#!/usr/bin/env node
import fs from 'node:fs';
import { observeDeploymentReadiness, dependencyRecheckState, collectWorkerDependencies } from './release-readiness.mjs';
import { gsalesRuntimeCommand, parseGsalesRuntime } from './release-gsales.mjs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { loadReleaseBookkeeping, persistReleaseBookkeeping } from './release-bookkeeping.mjs';
import { migrationHead } from './release-migrations.mjs';
import { gsalesProvisionCommand } from './release-gsales-provision.mjs';
import { verifyReleaseBundle } from './release-bundle.mjs';
import { acquireReleaseLock } from './release-lock.mjs';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';
import { applyCandidate, cleanupTemporaryWorktree, inspectProductionReadOnly, inspectAgataMigrationReadiness, inspectGsalesReadiness, planReleaseRetention, provisionAgataInternalConfigForBaseline, runRemoteCommand, validateAgataProviderMode, validateAgataProviderCompatibility } from './release-gpoint-vps-adapter.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
let STATE_FILE = process.env.GPOINT_RELEASE_STATE_FILE || path.join(ROOT, 'docs/release/state/production.json');
let MANIFEST_DIR = process.env.GPOINT_RELEASE_MANIFEST_DIR || path.join(ROOT, 'docs/release/manifests');
// Keep the lock outside Actions checkout: checkout cleanup must never erase a
// stale release lock left by interruption, and all Mac checkouts share it.
const LOCK_FILE = path.join(os.homedir(), '.config/gpoint/release-production.lock');
const LEGACY_LOCK_FILE = path.join(ROOT, '.gpoint-release.lock');
const SHA_RE = /^[0-9a-f]{40}$/;
const REMOTE_VERIFY_TIMEOUT_MS = 15_000;
const GITHUB_REMOTE_TIMEOUT_MS = 20_000;
const NETWORK_RETRY_BUDGET_MS = 30 * 60 * 1000;
const NETWORK_BACKOFF_CAP_MS = 60_000;
const TAG_RECONCILIATION_RETRY_DELAYS_MS = [1_000, 3_000];
const GIB = 1024 ** 3;
const LOCAL_DISK_HARD_MIN_BYTES = 10 * GIB;
const LOCAL_DISK_TARGET_BYTES = 15 * GIB;
const LOCAL_CACHE_CLEANUP_TIMEOUT_MS = 30_000;
function githubSshCommand() {
  return [
    'ssh',
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=8',
    '-o', 'ConnectionAttempts=1',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', 'IdentitiesOnly=yes',
    '-o', 'HostName=ssh.github.com',
    '-o', 'HostKeyAlias=github.com',
    '-o', 'ServerAliveInterval=5',
    '-o', 'ServerAliveCountMax=2',
    ...(process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE ? ['-i', `"${process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE.replaceAll('"', '\\"')}"`] : []),
    '-p', '443',
  ].join(' ');
}

function die(message) { throw new Error(`BLOCKED: ${message}`); }
function readJson(file) { return JSON.parse(fs.readFileSync(file, 'utf8')); }
function isoNow() { return new Date().toISOString(); }
function measured(timings, name, action) { const started = Date.now(); try { return action(); } finally { timings[name] = Number(((Date.now() - started) / 1000).toFixed(1)); } }
function git(args, cwd = ROOT) { return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim(); }
const ledgerGit = (args, input) => execFileSync('git', args, { cwd: ROOT, input, encoding: 'utf8', timeout: 30_000 }).trim();
function manifestReference(file) { return `docs/release/manifests/${path.basename(file)}`; }
function command(args, cwd) { const r = spawnSync(args[0], args.slice(1), { cwd, encoding: 'utf8', stdio: 'inherit' }); if (r.status !== 0) die(`${args.join(' ')} exited with ${r.status}`); }
export function localDiskDecision(freeBytes) {
  if (!Number.isFinite(freeBytes) || freeBytes < 0) throw new Error('local disk free-space value is invalid');
  if (freeBytes < LOCAL_DISK_HARD_MIN_BYTES) return { gate: 'FAIL', freeGiB: freeBytes / GIB };
  if (freeBytes < LOCAL_DISK_TARGET_BYTES) return { gate: 'WARNING', freeGiB: freeBytes / GIB };
  return { gate: 'PASS', freeGiB: freeBytes / GIB };
}

export function cleanupLocalPackageCaches(runCommand = (binary, args, options) => spawnSync(binary, args, options)) {
  const attempts = [
    ['pnpm', ['store', 'prune']],
    ['npm', ['cache', 'clean', '--force']],
  ];
  return attempts.map(([binary, args]) => {
    const result = runCommand(binary, args, { encoding: 'utf8', stdio: 'ignore', timeout: LOCAL_CACHE_CLEANUP_TIMEOUT_MS });
    return { command: binary, ok: result.status === 0 && !result.error };
  });
}

export function runLocalDiskPreflight({
  freeBytes = () => { const stats = fs.statfsSync(ROOT); return stats.bavail * stats.bsize; },
  cleanup = cleanupLocalPackageCaches,
  warn = message => console.error(message),
} = {}) {
  let bytes = freeBytes();
  let cleanupAttempted = false;
  if (bytes < LOCAL_DISK_TARGET_BYTES) {
    cleanupAttempted = true;
    const results = cleanup();
    for (const result of results) if (!result.ok) warn(`LOCAL_DISK_CACHE_CLEANUP=${result.command}:FAILED`);
    bytes = freeBytes();
  }
  const decision = localDiskDecision(bytes);
  if (decision.gate === 'FAIL') throw new Error(`BLOCKED: local disk has ${decision.freeGiB.toFixed(2)} GiB free; release requires at least 10 GiB`);
  if (decision.gate === 'WARNING') warn(`LOCAL_DISK_GATE=WARNING free=${decision.freeGiB.toFixed(2)} GiB target=15 GiB`);
  else console.error(`LOCAL_DISK_GATE=PASS free=${decision.freeGiB.toFixed(2)} GiB`);
  return { ...decision, cleanupAttempted };
}
function assertSha(value, name = 'candidate SHA') { if (!SHA_RE.test(value ?? '')) die(`${name} must be an exact 40-character commit SHA`); }
function candidateSha(args) { const i = args.indexOf('--candidate'); const value = i >= 0 ? args[i + 1] : process.env.GPOINT_CANDIDATE_SHA; assertSha(value); return value; }
function state() { return readJson(STATE_FILE); }
function writeManifest(manifest) { fs.mkdirSync(MANIFEST_DIR, { recursive: true }); const file = path.join(MANIFEST_DIR, `${manifest.releaseId}.json`); if (fs.existsSync(file)) die(`manifest already exists: ${file}`); fs.writeFileSync(file, `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx' }); return file; }

function releaseRetentionCandidates() {
  const reconciliations = new Map();
  for (const name of fs.readdirSync(MANIFEST_DIR).filter(name => name.endsWith('.json'))) {
    const record = readJson(path.join(MANIFEST_DIR, name));
    if (!record.failedReleaseId || record.runtimeSwitchOccurred !== false || record.databaseApplyOccurred !== false) continue;
    if (record.recordedProductionSha && record.liveProductionSha && record.recordedProductionSha !== record.liveProductionSha) continue;
    const state = record.failureClassification?.reconciled ?? record.correctedFailureStage;
    if (state === 'FAILED_PRE_SWITCH' || state === 'FAILED_MIGRATION' || record.eventType === 'FAILED_RELEASE_PRE_SWITCH_RECONCILIATION' || (record.eventType === 'FAILED_RELEASE_STATE_CORRECTION' && state === 'PRE_SWITCH')) {
      reconciliations.set(record.failedReleaseId, state === 'FAILED_MIGRATION' ? 'FAILED_MIGRATION' : 'FAILED_PRE_SWITCH');
    }
  }
  return fs.readdirSync(MANIFEST_DIR)
    .filter(name => name.endsWith('.json') && name.startsWith('gpoint-release-'))
    .map(name => readJson(path.join(MANIFEST_DIR, name)))
    .filter(manifest => /^[0-9a-f]{40}$/.test(manifest.candidateSha ?? ''))
    .map(manifest => {
      const reconciledStatus = reconciliations.get(manifest.releaseId);
      return {
        sha: manifest.candidateSha,
        status: reconciledStatus ?? manifest.status,
        recoveryRequired: reconciledStatus ? false : manifest.status === 'RECOVERY_REQUIRED',
      };
    });
}

export function sanitizeReleaseOutput(value) {
  return String(value ?? '')
    .replace(/((?:password|passwd|token|secret|api[_-]?key|private[_-]?key|database_url|dsn)\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, '$1[REDACTED]')
    .replace(/(postgres(?:ql)?(?:\+[a-z0-9]+)?:\/\/[^:\s/@]+:)[^@\s/]+@/gi, '$1[REDACTED]@')
    .replace(/(\bBearer\s+)[^\s"'`]+/gi, '$1[REDACTED]')
    .slice(-4000);
}

export function failureEvidence(error, { stage, fallbackStage } = {}) {
  const stdout = String(error?.stdout ?? '');
  const stderr = String(error?.stderr || error?.output || stdout);
  return {
    failureStage: stage || fallbackStage || 'UNKNOWN',
    failureReason: sanitizeReleaseOutput(error?.message),
    failureCommand: stage || error?.command || null,
    failureCommandSha256: error?.commandHash ?? null,
    failureExitCode: Number.isInteger(error?.exitCode) ? error.exitCode : Number.isInteger(error?.status) ? error.status : null,
    failureStdout: sanitizeReleaseOutput(stdout),
    failureStderr: sanitizeReleaseOutput(stderr),
  };
}

export function validateCandidate({ candidateSha: sha, remoteSha, clean = true }) {
  const failures = [];
  try { assertSha(sha); } catch (e) { failures.push(e.message); }
  if (remoteSha !== sha) failures.push('remote candidate SHA does not match candidate');
  if (!clean) failures.push('build checkout is dirty');
  return failures;
}

export function checkArtifactParity(candidate, artifacts) {
  const failures = [];
  for (const role of ['api', 'worker', 'bot', 'miniapp']) {
    if (!artifacts?.[role] || artifacts[role].sourceSha !== candidate) failures.push(`${role} artifact SHA mismatch`);
  }
  return failures;
}

export function checkEffectiveConfig(candidate, config) {
  const failures = [];
  for (const role of ['api', 'worker', 'bot']) if (config?.[role] !== candidate) failures.push(`${role} effective image revision mismatch`);
  if (config?.miniapp !== candidate) failures.push('miniapp effective artifact revision mismatch');
  return failures;
}

export function checkAgataArtifactParity(candidate, artifacts) {
  return ['agataApi', 'agataWorker'].flatMap(role =>
    artifacts?.[role]?.sourceSha === candidate ? [] : [`${role} artifact SHA mismatch`],
  );
}

export function checkGsalesArtifactParity(candidate, artifacts) {
  return ['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker'].flatMap(role =>
    artifacts?.[role]?.sourceSha === candidate ? [] : [`${role} artifact SHA mismatch`]);
}

function gsalesArtifacts(candidate) {
  return Object.fromEntries(['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker'].map(role => [role, { sourceSha: candidate }]));
}

export function checkAgataEffectiveConfig(config) {
  if (config?.enabled !== true) return ['AGATA is not enabled'];
  return validateAgataProviderCompatibility(config);
}

export function validateMacWorkerStatus(status, { nowMs = Date.now(), maxAgeMs = 90_000, futureClockSkewMs = 5_000 } = {}) {
  return validateAgataProviderMode({
    provider: 'mac_worker',
    enabled: 'true',
    localEnabled: 'true',
    workerTokenPresent: true,
    agataApiHealthy: true,
    workerStatus: status,
  }, { nowMs, maxWorkerAgeMs: maxAgeMs, futureClockSkewMs });
}

function readMacWorkerStatus() {
  const script = path.join(os.homedir(), 'Downloads/Codex/Workers/AGATA Workers/runtime/current/run_codex_worker.sh');
  try {
    const output = execFileSync(script, ['status'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 25_000, maxBuffer: 64 * 1024 });
    const status = JSON.parse(output);
    return status && typeof status === 'object' && !Array.isArray(status) ? status : null;
  } catch (error) {
    if (error.code === 'ETIMEDOUT' || error.killed) throw error;
    return null;
  }
}

function readWorkerDependencies() {
  return collectWorkerDependencies({GPOINT: readMacWorkerStatus, GSALES: () => {
  let sales = null;
  try {
    const file = path.join(os.homedir(), 'Downloads/Codex/Workers/AGATA Workers/status/worker-status.json');
    const stat = fs.lstatSync(file);
    if (stat.isSymbolicLink() || (stat.mode & 0o077)) throw new Error('unsafe status file');
    const target = readJson(file).targets?.GSALES;
    if (target) sales = { enabled: true, queue_connectivity: ['idle', 'running'].includes(target.state) ? 'reachable' : 'unavailable', last_poll_at: target.last_poll_at, last_heartbeat_at: target.last_heartbeat_at };
  } catch { /* Dependency observation remains NOT_VERIFIED. */ }
  return sales;
  }});
}

function verifyWorkerCompatibility(candidate, facts) {
  // Secrets are compared through stdin only, never arguments, logs or evidence.
  const script = `import json,pathlib\ne=dict(line.split('=',1) for line in pathlib.Path('/opt/gpoint/env/production.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)\nassert e['RELEASE_SHA']=='${facts.LIVE_RELEASE_ENV_SHA}'\nprint(json.dumps({key:e[key] for key in ('GPOINT_AGATA_WORKER_TOKEN','GSALES_WORKER_TOKEN')}))\n`;
  const credentials = runRemoteCommand(`set -e; printf '%s' '${Buffer.from(script).toString('base64')}' | base64 -d | python3 -`);
  const result = spawnSync('python3', [path.join(ROOT, 'scripts/verify-release-worker-compatibility.py'),
    '--candidate', candidate, '--gpoint-tenant', facts.LIVE_AGATA_TENANT_ID ?? '',
    '--gsales-tenant', facts.LIVE_GSALES_TENANT_ID ?? ''], { cwd: ROOT, input: credentials, encoding: 'utf8', timeout: 10_000 });
  if (result.status !== 0) die('Worker source/protocol/tenant/lane compatibility is unproven');
  const evidence = JSON.parse(result.stdout);
  if (evidence.compatibility !== 'PASS' || evidence.candidateSha !== candidate) die('Worker compatibility evidence invalid');
  return evidence;
}

function providerConfigFromFacts(facts, workerStatus = undefined) {
  return {
    provider: facts.LIVE_AGATA_PROVIDER,
    enabled: facts.LIVE_AGATA_ENABLED,
    localEnabled: facts.LIVE_AGATA_CODEX_LOCAL_ENABLED,
    workerTokenPresent: facts.LIVE_CONFIG_GPOINT_AGATA_WORKER_TOKEN === 'YES',
    apiKeyPresent: facts.LIVE_CONFIG_AGATA_LLM_API_KEY === 'YES',
    agataApiHealthy: facts.LIVE_AGATA_API_HEALTH === 'healthy',
    workerStatus,
  };
}

function assertProviderCompatibility(facts) {
  const provider = facts.LIVE_AGATA_PROVIDER;
  if (facts.LIVE_AGATA_ENABLED !== 'true') die('AGATA is not enabled');
  const failures = validateAgataProviderCompatibility(providerConfigFromFacts(facts));
  if (failures.length) die(failures.join('; '));
  return { provider };
}

export function releaseStateAfterError({ switched, runtimeVerified = false }) {
  if (!switched) return 'FAILED_PRE_SWITCH';
  return runtimeVerified ? 'RECOVERY_REQUIRED' : 'SWITCHED_UNVERIFIED';
}

export function classifyReleaseFailure({ switchStarted = false, migrationStarted = false, agataStartStarted = false }) {
  const switched = switchStarted;
  const partiallyMutated = migrationStarted || agataStartStarted;
  return {
    switched,
    partiallyMutated,
    releaseState: switched || partiallyMutated
      ? 'RECOVERY_REQUIRED'
      : releaseStateAfterError({ switched, runtimeVerified: false }),
    failureStage: switched ? 'POST_SWITCH' : partiallyMutated ? 'PRE_SWITCH_DB_OR_AGATA' : 'PRE_SWITCH',
  };
}

export function verifiedRuntimeState(current, { candidate, dbHead }) {
  return {
    ...current,
    currentProductionSha: candidate,
    activeRuntimeSha: candidate,
    currentProductionDbHead: dbHead,
    activeRuntimeDbHead: dbHead,
  };
}

export function canCreateProductionRef({ releaseState, candidate, actualSha, mixedRuntime = false, health = true, dbVerified = true }) {
  return releaseState === 'VERIFIED' && candidate === actualSha && !mixedRuntime && health && dbVerified;
}

export function shouldFinalizeRecovery({ releaseState, candidate, actualSha, gatesPass }) {
  return releaseState === 'RECOVERY_REQUIRED' && candidate === actualSha && gatesPass === true;
}

export function candidateAgataMigrationHead(candidate) {
  assertSha(candidate);
  const directory = 'services/agata/migrations/versions';
  const files = git(['ls-tree', '-r', '--name-only', candidate, directory]).split('\n').filter(name => name.endsWith('.py'));
  return migrationHead(files.map(name => git(['show', `${candidate}:${name}`]))).head;
}

export function recoveryRuntimeFailures(candidate, facts, expectedDbHead, workerStatus = undefined, expectedAgataHead = 'agata0008') {
  const failures = [];
  if (facts.LIVE_RELEASE_ENV_SHA !== candidate) failures.push('release env SHA mismatch');
  for (const role of ['API', 'WORKER', 'BOT']) {
    if (facts[`LIVE_${role}_SHA`] !== candidate) failures.push(`${role.toLowerCase()} build SHA mismatch`);
    if (facts[`LIVE_${role}_RUNNING`] !== 'true') failures.push(`${role.toLowerCase()} is not running`);
  }
  if (facts.LIVE_API_HEALTH !== 'healthy') failures.push('API is not healthy');
  if (facts.LIVE_MINIAPP_SHA !== candidate) failures.push('Mini App SHA mismatch');
  if (facts.LIVE_PRISMA_HEAD !== expectedDbHead) failures.push('Prisma head mismatch');
  if (facts.LIVE_AGATA_HEAD !== expectedAgataHead) failures.push('AGATA Alembic head mismatch');
  if (facts.LIVE_CONFIG_AGATA_CODEX_LOCAL_ENABLED !== 'YES') failures.push('AGATA_CODEX_LOCAL_ENABLED is missing');
  failures.push(...validateAgataProviderCompatibility(providerConfigFromFacts(facts)));
  if (facts.LIVE_AGATA_ENABLED !== 'true') failures.push('AGATA is not enabled');
  if (facts.LIVE_AGATA_PROVIDER === 'mac_worker' && facts.LIVE_CONFIG_GPOINT_AGATA_WORKER_TOKEN !== 'YES') failures.push('AGATA worker token/config is missing');
  if (facts.LIVE_AGATA_PROVIDER === 'disabled' && facts.LIVE_CONFIG_AGATA_LLM_API_KEY !== 'NO') failures.push('AGATA LLM API key must remain absent while provider is disabled');
  for (const role of ['API', 'WORKER']) {
    if (facts[`LIVE_AGATA_${role}_SHA`] !== candidate) failures.push(`AGATA ${role.toLowerCase()} build SHA mismatch`);
    if (facts[`LIVE_AGATA_${role}_RUNNING`] !== 'true') failures.push(`AGATA ${role.toLowerCase()} is not running`);
    if (facts[`LIVE_AGATA_${role}_HEALTH`] !== 'healthy') failures.push(`AGATA ${role.toLowerCase()} is not healthy`);
  }
  if (!facts.LIVE_AGATA_API_IMAGE_ID || facts.LIVE_AGATA_API_IMAGE_ID !== facts.LIVE_AGATA_WORKER_IMAGE_ID) failures.push('AGATA API/worker image identity mismatch');
  return failures;
}

export function recoveryArtifactIdentities(candidate, facts) {
  const runtimeImageId = facts.LIVE_API_IMAGE;
  return {
    api: { sourceSha: candidate, runtimeImageId },
    worker: { sourceSha: candidate, runtimeImageId: facts.LIVE_WORKER_IMAGE },
    bot: { sourceSha: candidate, runtimeImageId: facts.LIVE_BOT_IMAGE },
    miniapp: { sourceSha: candidate, servedBuildSha: facts.LIVE_MINIAPP_SHA },
    agataApi: { sourceSha: candidate, runtimeImageId: facts.LIVE_AGATA_API_IMAGE_ID },
    agataWorker: { sourceSha: candidate, runtimeImageId: facts.LIVE_AGATA_WORKER_IMAGE_ID },
  };
}

export function evaluateSafetyGates({ candidateSha: sha, remoteSha, clean = true, artifacts, config, backupReady = true, migrationOk = true, concurrent = false, recordedProductionSha, liveProductionSha, productionRefTarget }) {
  const failures = validateCandidate({ candidateSha: sha, remoteSha, clean });
  failures.push(...checkArtifactParity(sha, artifacts));
  failures.push(...checkEffectiveConfig(sha, config));
  if (!backupReady) failures.push('backup gate failed');
  if (!migrationOk) failures.push('migration gate failed');
  if (concurrent) failures.push('concurrent release lock is held');
  if (recordedProductionSha !== liveProductionSha) failures.push('live production SHA drift');
  if (productionRefTarget !== undefined && productionRefTarget !== sha) failures.push('production ref target mismatch');
  return [...new Set(failures)];
}

function acquireLock(releaseId) {
  return acquireReleaseLock(LOCK_FILE, releaseId, LEGACY_LOCK_FILE);
}

function errorText(error) {
  const stderr = Buffer.isBuffer(error?.stderr) ? error.stderr.toString('utf8') : error?.stderr ?? '';
  const message = Buffer.isBuffer(error?.message) ? error.message.toString('utf8') : error?.message ?? '';
  return `${error?.code ?? ''} ${message} ${stderr}`;
}

export function isTransientGitHubTransportFailure(error) {
  const text = errorText(error);
  if (/permission denied|publickey|host key verification failed|remote mismatch|non-fast-forward|rejected|authentication failed|sha mismatch|candidate sha is not exact|certificate verify failed|certificate verification failed|self-signed certificate|unable to get local issuer/i.test(text)) return false;
  const transient = error?.code === 'ETIMEDOUT'
    || error?.killed === true
    || /could not resolve (?:hostname|host)|nodename nor servname|temporary failure in name resolution|name or service not known|connection timed out|operation timed out|connection reset|connection closed|closed by .* port|banner exchange|connection refused|network is unreachable|network unreachable|eai_again|enotfound|broken pipe|ssl_error_syscall|ssl read error|tls.*(?:timeout|temporar|unexpected eof|connection reset)|unexpected eof while reading/i.test(text);
  if (transient) return true;
  if (/could not read from remote repository/i.test(text)) return false;
  return false;
}

export function runWithBoundedGitHubRetries(operation, {
  sleep = milliseconds => execFileSync('sleep', [String(milliseconds / 1_000)]),
  now = () => performance.now(), jitter = (low, high) => low + Math.random() * (high - low),
  budgetMs = NETWORK_RETRY_BUDGET_MS, emit = message => console.error(message),
} = {}) {
  const started = now();
  const statePath = process.env.NETWORK_RETRY_BUDGET_STATE;
  let spentMs = 0;
  if (statePath && fs.existsSync(statePath)) {
    const raw = fs.readFileSync(statePath, 'utf8').trim();
    if (!/^\d+(?:\.\d+)?$/.test(raw)) throw new Error('Invalid shared network retry budget state');
    spentMs = Number(raw) * 1000;
  }
  const remainingBudgetMs = Math.max(0, budgetMs - spentMs);
  const deadline = started + remainingBudgetMs;
  let retries = 0;
  const unavailable = () => {
    emit(`NETWORK_UNAVAILABLE stage=github_read elapsed_s=${((now() - started) / 1000).toFixed(1)} budget_s=${budgetMs / 1000}`);
    const error = new Error('NETWORK_UNAVAILABLE stage=github_read');
    error.code = 'NETWORK_UNAVAILABLE';
    return error;
  };
  try { while (true) {
    if (now() >= deadline) throw unavailable();
    try {
      const result = operation(retries + 1);
      if (now() >= deadline) throw unavailable();
      if (retries) emit(`NETWORK_RESTORED stage=github_read elapsed_s=${((now() - started) / 1000).toFixed(1)}`);
      emit('RELEASE_CONTINUING stage=github_read');
      return result;
    } catch (error) {
      if (!isTransientGitHubTransportFailure(error)) throw error;
      const remaining = deadline - now();
      if (remaining <= 0) throw unavailable();
      const cap = Math.min(NETWORK_BACKOFF_CAP_MS, 1_000 * 2 ** Math.min(retries, 6));
      const delay = Math.min(Math.max(0, jitter(0, cap)), remaining);
      emit(`NETWORK_RETRYING stage=github_read retry_in_s=${(delay / 1000).toFixed(1)} elapsed_s=${((now() - started) / 1000).toFixed(1)}`);
      retries += 1;
      sleep(delay);
    }
  } } finally {
    if (statePath && retries) {
      fs.mkdirSync(path.dirname(statePath), { recursive: true });
      const temporary = `${statePath}.${process.pid}.tmp`;
      fs.writeFileSync(temporary, `${((spentMs + Math.max(0, now() - started)) / 1000).toFixed(6)}\n`, { mode: 0o600 });
      fs.renameSync(temporary, statePath);
    }
  }
}

function waitForGitHubRetry(attempt, sleep = milliseconds => execFileSync('sleep', [String(milliseconds / 1_000)])) {
  const delay = TAG_RECONCILIATION_RETRY_DELAYS_MS[attempt - 1];
  if (delay) sleep(delay);
}

function githubTransportEnv() {
  return {
    ...process.env,
    GIT_SSH_COMMAND: process.env.GIT_SSH_COMMAND || githubSshCommand(),
  };
}

export function runRemoteShaList({ runner = execFileSync } = {}) {
  return runner('git', ['ls-remote', 'origin', 'refs/heads/main', 'refs/tags/*'], {
    cwd: ROOT,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: REMOTE_VERIFY_TIMEOUT_MS,
    maxBuffer: 8 * 1024 * 1024,
    env: githubTransportEnv(),
  }).trim();
}

export function remoteProof(candidate, { runRemote = () => runRemoteShaList(), sleep, now, jitter, budgetMs, emit } = {}) {
  let lines;
  try { lines = runWithBoundedGitHubRetries(() => runRemote({ timeoutMs: REMOTE_VERIFY_TIMEOUT_MS }), { sleep, now, jitter, budgetMs, emit }); }
  catch (error) {
    if (error?.code === 'NETWORK_UNAVAILABLE') die('NETWORK_UNAVAILABLE during GitHub remote verification');
    if (isTransientGitHubTransportFailure(error)) die('GitHub remote verification stopped after a retryable transport error');
    die('GitHub remote verification failed on a non-transient error; refusing retry');
  }
  const refs = lines.split('\n').filter(Boolean).map(line => line.split(/\s+/));
  const direct = refs.find(([sha]) => sha === candidate);
  const main = refs.find(([, ref]) => ref === 'refs/heads/main');
  if (!main) die('origin/main could not be independently verified');
  if (main[0] !== candidate) die(`candidate SHA is not exact origin/main: candidate=${candidate} origin/main=${main[0]}`);
  return { mainSha: main[0], directRef: direct?.[1] ?? null, refs }; 
}

function runGitHubRemoteGit(args, { timeout = GITHUB_REMOTE_TIMEOUT_MS } = {}) {
  try {
    return runWithBoundedGitHubRetries(() => execFileSync('git', args, {
      cwd: ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout,
      maxBuffer: 8 * 1024 * 1024,
      env: githubTransportEnv(),
    }).trim());
  } catch (error) {
    if (error?.code === 'NETWORK_UNAVAILABLE') die('NETWORK_UNAVAILABLE during GitHub remote operation');
    die(isTransientGitHubTransportFailure(error)
      ? 'GitHub remote operation stopped after a retryable transport error'
      : 'GitHub remote operation failed on a non-transient error; refusing retry');
  }
}

function remoteTagTarget(tag) {
  const output = runGitHubRemoteGit(['ls-remote', 'origin', `refs/tags/${tag}`, `refs/tags/${tag}^{}`]);
  const refs = new Map(output.split('\n').filter(Boolean).map(line => {
    const [objectSha, ref] = line.split(/\s+/);
    return [ref, objectSha];
  }));
  return refs.get(`refs/tags/${tag}^{}`) ?? refs.get(`refs/tags/${tag}`) ?? null;
}

export function pushProductionTag(tag, candidate, { push = () => execFileSync('git', ['push', 'origin', `refs/tags/${tag}`], {
  cwd: ROOT,
  encoding: 'utf8',
  stdio: ['ignore', 'pipe', 'pipe'],
  timeout: GITHUB_REMOTE_TIMEOUT_MS,
  maxBuffer: 8 * 1024 * 1024,
  env: githubTransportEnv(),
}), lookup = remoteTagTarget, sleep } = {}) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      push(attempt);
      const target = lookup(tag);
      if (target !== candidate) die(`remote production tag target mismatch: ${tag}`);
      return target;
    } catch (error) {
      if (!isTransientGitHubTransportFailure(error)) {
        die(/permission denied|publickey|authentication failed/i.test(errorText(error))
          ? 'GitHub production tag push authentication failed; refusing retry'
          : /host key verification failed/i.test(errorText(error))
            ? 'GitHub production tag push host-key verification failed; refusing retry'
            : 'GitHub production tag push failed on a non-transient error; refusing retry');
      }
      // A failed push can have reached GitHub; always reconcile before any retry.
      let existing;
      try { existing = lookup(tag); }
      catch { die('production tag push outcome is ambiguous; remote tag could not be verified'); }
      if (existing === candidate) return existing;
      if (existing !== null) die(`production tag already exists with a different target: ${tag}`);
      if (attempt === 3) die('GitHub production tag push failed after three bounded transport attempts');
      waitForGitHubRetry(attempt, sleep);
    }
  }
  throw new Error('unreachable production tag push retry state');
}

function readLiveFacts() {
  const output = inspectProductionReadOnly();
  const facts = {};
  for (const line of output.split(/\r?\n/)) {
    for (const match of line.matchAll(/(?:^|\s)([A-Z][A-Z0-9_]+)=([^\s]+)/g)) facts[match[1]] = match[2];
  }
  return facts;
}

function assertLiveBaseline(current, facts, { requireAgataConfig = true } = {}) {
  const liveSha = facts.LIVE_RELEASE_ENV_SHA;
  if (!SHA_RE.test(liveSha ?? '') || liveSha !== current.currentProductionSha) die(`live production SHA drift: recorded=${current.currentProductionSha} live=${liveSha ?? 'unavailable'}`);
  if (!facts.LIVE_DOCKER_VERSION || !facts.LIVE_COMPOSE_VERSION || !facts.LIVE_BUILDX_VERSION || facts.LIVE_BUILDKIT !== 'ready') die('production Docker/Compose/Buildx/BuildKit preflight failed');
  if (Number(facts.LIVE_DISK_FREE_KB) < 5242880) die('production disk preflight requires at least 5 GiB free');
  if (facts.LIVE_ROLLBACK_IMAGE_AVAILABLE !== 'YES') die('current production rollback image is unavailable');
  for (const role of ['API', 'WORKER', 'BOT']) {
    if (facts[`LIVE_${role}_SHA`] !== liveSha || facts[`LIVE_${role}_RUNNING`] !== 'true') die(`live ${role.toLowerCase()} runtime does not match RELEASE_SHA`);
    if (facts[`LIVE_${role}_HEALTH`] !== 'healthy' && role === 'API') die('live API health is not healthy');
  }
  if (facts.LIVE_MINIAPP_SHA !== liveSha) die('live Mini App SHA does not match RELEASE_SHA');
  if (facts.LIVE_PRISMA_HEAD !== current.currentProductionDbHead) die(`live Prisma head drift: recorded=${current.currentProductionDbHead} live=${facts.LIVE_PRISMA_HEAD ?? 'unavailable'}`);
  let providerReadiness = { provider: facts.LIVE_AGATA_PROVIDER };
  if (requireAgataConfig) {
    const requiredConfig = ['AGATA_ENABLED','AGATA_LLM_PROVIDER','AGATA_DATABASE_URL','AGATA_TENANT_ID','AGATA_INTERNAL_API_TOKEN','AGATA_MESSAGE_AEAD_KEY','AGATA_REQUEST_FINGERPRINT_HMAC_KEY','AGATA_RETRIEVAL_QUERY_HMAC_KEY','AGATA_RESPONSE_FINGERPRINT_HMAC_KEY','AGATA_SHADOW_CANDIDATE_HMAC_KEY'];
    for (const key of requiredConfig) if (facts[`LIVE_CONFIG_${key}`] !== 'YES') die(`AGATA production config is missing: ${key}`);
    if (facts.LIVE_CONFIG_AGATA_CODEX_LOCAL_ENABLED !== 'YES') die('AGATA_CODEX_LOCAL_ENABLED is missing');
    providerReadiness = assertProviderCompatibility(facts);
    if (providerReadiness.provider === 'mac_worker' && facts.LIVE_CONFIG_GPOINT_AGATA_WORKER_TOKEN !== 'YES') die('AGATA worker token/config is missing');
    if (providerReadiness.provider === 'disabled' && facts.LIVE_CONFIG_AGATA_LLM_API_KEY !== 'NO') die('AGATA LLM API key must remain absent while provider is disabled');
  }
  if (!facts.LIVE_AGATA_HEAD || facts.LIVE_AGATA_HEAD === 'READ_ONLY_DB_INSPECTION_FAILED') die('live AGATA migration head could not be verified');
  return { liveSha, liveDbHead: facts.LIVE_PRISMA_HEAD, agataDbHead: facts.LIVE_AGATA_HEAD, facts, ...providerReadiness };
}

function isolatedBuild(candidate, shouldBuild) {
  const worktree = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-release-'));
  try {
    command(['git', 'worktree', 'add', '--detach', worktree, candidate], ROOT);
    const checkoutSha = git(['rev-parse', 'HEAD'], worktree);
    if (checkoutSha !== candidate) die('isolated build checkout SHA mismatch');
    if (git(['status', '--porcelain'], worktree)) die('isolated build checkout is dirty');
    if (shouldBuild) { const pnpm = process.env.GPOINT_PNPM_BIN ?? 'pnpm'; command([pnpm, 'install', '--frozen-lockfile'], worktree); command([pnpm, '--filter', '@gpoint/db', 'exec', 'prisma', 'generate'], worktree); command([pnpm, 'build'], worktree); }
    const index = path.join(worktree, 'apps/miniapp/dist/index.html');
    const html = fs.existsSync(index) ? fs.readFileSync(index, 'utf8') : '';
    const match = html.match(/gpoint-build-sha" content="([0-9a-f]{40})/);
    if (shouldBuild && match?.[1] !== candidate) die('Mini App build SHA does not match candidate');
    return { worktree, checkoutSha, clean: true, miniappBuildSha: match?.[1] ?? null };
  } finally { cleanupTemporaryWorktree(worktree); }
}

function runDryRun(candidate) {
  const startedAt = isoNow();
  const releaseId = `gpoint-dry-run-${startedAt.replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const localHead = git(['rev-parse', candidate]);
    if (localHead !== candidate) die('candidate does not exist locally');
    const proof = remoteProof(candidate);
    const current = state();
    const baseline = assertLiveBaseline(current, readLiveFacts());
    const { liveSha, liveDbHead } = baseline;
    const gsalesReadiness = inspectGsalesReadiness(candidate, liveSha);
    const workerCompatibility = verifyWorkerCompatibility(candidate, baseline.facts);
    const agataMigrationReadiness = inspectAgataMigrationReadiness(candidate, baseline.agataDbHead, candidateAgataMigrationHead(candidate));
    const build = isolatedBuild(candidate, process.env.GPOINT_RELEASE_SKIP_BUILD !== '1');
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: build.miniappBuildSha ?? candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    Object.assign(artifacts, gsalesArtifacts(candidate));
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: baseline.provider, ...providerConfigFromFacts(baseline.facts, baseline.workerStatus), enabled: true };
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha, liveProductionSha: liveSha, productionRefTarget: candidate });
    gates.push(...checkAgataArtifactParity(candidate, artifacts), ...checkAgataEffectiveConfig(effective));
    gates.push(...checkGsalesArtifactParity(candidate, artifacts));
    if (gates.length) die(gates.join('; '));
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: null, dbHeadBefore: liveDbHead, dbHeadAfter: null, agataDbHeadBefore: baseline.agataDbHead, agataDbHeadAfter: candidateAgataMigrationHead(candidate), artifactIdentities: artifacts, provider: baseline.provider, agataProvider: baseline.provider, startedAt, finishedAt: isoNow(), status: 'PLANNED', failureStage: null, failureReason: null, rollbackSha: current.currentProductionSha, rollbackDbCompatibility: 'REQUIRES_FORWARD_FIX_ASSESSMENT', productionRef: null, remoteProof: { mainSha: proof.mainSha, directRef: proof.directRef }, liveFacts: baseline.facts, agataMigrationReadiness, dryRun: true };
    Object.assign(manifest, { workerCompatibility, gsalesReadiness, gsalesDbHeadBefore: baseline.facts.LIVE_GSALES_HEAD,
      gsalesMigrationSha256Before: baseline.facts.LIVE_GSALES_MIGRATION_SHA256 });
    const file = writeManifest(manifest);
    console.log(JSON.stringify({ status: 'PLANNED', releaseId, candidateSha: candidate, remoteMainSha: proof.mainSha, buildCheckoutSha: build.checkoutSha, buildCheckoutClean: build.clean, dbHead: liveDbHead, rollbackSha: current.currentProductionSha, manifest: path.relative(ROOT, file) }, null, 2));
  } finally { unlock(); }
}

function status() { console.log(JSON.stringify({ state: state(), activeLock: fs.existsSync(LOCK_FILE) ? readJson(LOCK_FILE) : null, manifests: fs.readdirSync(MANIFEST_DIR).filter(x => x.endsWith('.json')).sort() }, null, 2)); }

function provisionAgataConfig(candidate) {
  const localHead = git(['rev-parse', candidate]);
  if (localHead !== candidate) die('candidate does not exist locally');
  if (git(['status', '--porcelain'])) die('AGATA config provisioning requires a clean committed checkout');
  const proof = remoteProof(candidate);
  if (proof.mainSha !== candidate) die('candidate must exactly match GitHub origin/main before AGATA config provisioning');
  const current = state();
  assertLiveBaseline(current, readLiveFacts(), { requireAgataConfig: false });
  if (!SHA_RE.test(current.currentProductionSha ?? '')) die('recorded current production SHA is unavailable');
  const releaseId = `gpoint-agata-config-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const output = provisionAgataInternalConfigForBaseline(current.currentProductionSha);
    console.log(JSON.stringify({ status: 'PROVISIONED', candidateSha: candidate, baselineSha: current.currentProductionSha, provider: output.match(/AGATA_LLM_PROVIDER=(disabled|mac_worker)/)?.[1] ?? 'unknown', apiKeyProvisioned: false, output }, null, 2));
  } finally { unlock(); }
}

function provisionGsalesConfig(candidate, approvedSha) {
  if (candidate !== approvedSha) die('GSales provisioning requires --owner-approved-sha equal to exact candidate');
  const releaseId = `gpoint-gsales-config-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    if (git(['rev-parse', 'HEAD']) !== candidate || git(['status', '--porcelain'])) die('GSales provisioning requires exact clean candidate checkout');
    if (remoteProof(candidate).mainSha !== candidate) die('candidate must exactly match GitHub origin/main');
    const bundle = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate);
    const current = state();
    const baseline = assertLiveBaseline(current, readLiveFacts());
    writeManifest({ releaseId: `${releaseId}-baseline`, candidateSha: candidate, previousProductionSha: baseline.liveSha,
      status: 'CONFIG_PROVISION_BASELINE_RECORDED', dbHeadBefore: baseline.liveDbHead,
      agataDbHeadBefore: baseline.agataDbHead, gsalesDbHeadBefore: baseline.facts.LIVE_GSALES_HEAD,
      authorizationMode: 'EXACT_SHA', ownerApprovedSha: approvedSha,
      ciReceipt: { ...bundle, archive: undefined }, recordedAt: isoNow(), runtimeSwitchOccurred: false });
    const output = runRemoteCommand(gsalesProvisionCommand(candidate, approvedSha, baseline.liveSha));
    const evidence = JSON.parse(output.trim().split('\n').at(-1));
    if (evidence.status !== 'PROVISIONED' || evidence.candidateSha !== candidate || evidence.baselineSha !== baseline.liveSha || evidence.backup?.status !== 'COMPLETE_PROTECTED') die('GSales provisioning evidence missing');
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: baseline.liveSha,
      status: 'GSales_CONFIG_PROVISIONED', authorizationMode: 'EXACT_SHA', ownerApprovedSha: approvedSha,
      dbHeadBefore: baseline.liveDbHead, agataDbHeadBefore: baseline.agataDbHead,
      startedAt: isoNow(), finishedAt: isoNow(), runtimeSwitchOccurred: false, evidence };
    writeManifest(manifest);
    console.log(JSON.stringify(manifest));
  } finally { unlock(); }
}

function configureGsalesWorker(candidate, approvedSha) {
  if (candidate !== approvedSha) die('Mac lane configuration requires exact-SHA OWNER approval');
  const releaseId = `gpoint-gsales-worker-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    if (git(['rev-parse', 'HEAD']) !== candidate || git(['status', '--porcelain'])) die('Mac configuration requires a clean exact candidate');
    if (remoteProof(candidate).mainSha !== candidate) die('candidate must match GitHub current main');
    verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate);
    const baseline = assertLiveBaseline(state(), readLiveFacts());
    inspectGsalesReadiness(candidate, baseline.liveSha);
    const source = `import json,pathlib\ne=dict(line.split('=',1) for line in pathlib.Path('/opt/gpoint/env/production.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)\nassert e['RELEASE_SHA']=='${baseline.liveSha}'\nprint(json.dumps({key:e[key] for key in ('GSALES_WORKER_TOKEN','GSALES_TENANT_ID')}))\n`;
    const credentials = runRemoteCommand(`set -e; printf '%s' '${Buffer.from(source).toString('base64')}' | base64 -d | python3 -`);
    const result = spawnSync('python3', [path.join(ROOT, 'scripts/configure-gsales-mac-lane.py'),
      '--candidate', candidate, '--owner-approved-sha', approvedSha, '--apply'],
      { cwd: ROOT, input: credentials, encoding: 'utf8', timeout: 120_000 });
    if (result.status !== 0) die(`Mac configuration blocked: ${sanitizeReleaseOutput(result.stderr)}`);
    const evidence = JSON.parse(result.stdout);
    if (!['CONFIGURED_GPOINT_AUTHENTICATED_GSALES_PENDING', 'ALREADY_CONFIGURED'].includes(evidence.status) || evidence.candidateSha !== candidate || evidence.generationInvoked !== false) die('Mac configuration evidence unproven');
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: baseline.liveSha,
      status: 'WORKER_CONFIGURED', authorizationMode: 'EXACT_SHA', ownerApprovedSha: approvedSha,
      runtimeSwitchOccurred: false, databaseApplyOccurred: false, finishedAt: isoNow(), evidence };
    writeManifest(manifest); console.log(JSON.stringify(manifest));
  } finally { unlock(); }
}

function runApply(candidate) {
  const startedAt = isoNow();
  const totalStarted = Date.now();
  const releaseId = `gpoint-release-${startedAt.replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  const current = state();
  let switchStarted = false;
  let migrationStarted = false;
  let agataStartStarted = false;
  let validatedProvider = null;
  const stageTimings = {};
  try {
    const proof = remoteProof(candidate);
    const baseline = assertLiveBaseline(current, readLiveFacts());
    validatedProvider = baseline.provider;
    const { liveSha, liveDbHead } = baseline;
    const gsalesReadiness = inspectGsalesReadiness(candidate, liveSha);
    const workerCompatibility = verifyWorkerCompatibility(candidate, baseline.facts);
    const agataMigrationReadiness = inspectAgataMigrationReadiness(candidate, baseline.agataDbHead, candidateAgataMigrationHead(candidate));
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: baseline.provider, ...providerConfigFromFacts(baseline.facts, baseline.workerStatus), enabled: true };
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    Object.assign(artifacts, gsalesArtifacts(candidate));
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha, liveProductionSha: liveSha, productionRefTarget: candidate });
    gates.push(...checkAgataArtifactParity(candidate, artifacts), ...checkAgataEffectiveConfig(effective));
    gates.push(...checkGsalesArtifactParity(candidate, artifacts));
    if (gates.length) die(gates.join('; '));
    const retentionCandidates = releaseRetentionCandidates();
    const retentionPlan = planReleaseRetention({
      currentSha: candidate,
      rollbackSha: liveSha,
      candidates: retentionCandidates,
      imageTags: retentionCandidates.flatMap(({ sha }) => [`gpoint-runtime:${sha}`, `gpoint-agata-runtime:${sha}`]),
    });
    const adapter = applyCandidate(candidate, {
      retentionPlan,
      provider: baseline.provider,
      onMigrationStart: () => { migrationStarted = true; },
      onAgataStart: () => { agataStartStarted = true; },
      onSwitchStart: () => { switchStarted = true; },
    });
    if (adapter.postSwitch?.state !== 'VERIFIED') {
      const error = new Error(`post-switch reconciliation required: ${adapter.postSwitch?.state ?? 'UNKNOWN'}`);
      error.code = adapter.postSwitch?.state ?? 'SWITCHED_UNVERIFIED';
      throw error;
    }
    if (adapter.agataIdentity.provider !== baseline.provider) die('post-switch AGATA provider does not match validated baseline');
    if (baseline.provider === 'mac_worker') assertProviderCompatibility(baseline.facts);
    const root = measured(stageTimings, 'API_HEALTH', () => execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/'], { encoding: 'utf8' }));
    if (!root.includes(`gpoint-build-sha" content="${candidate}`)) die('external Mini App build SHA mismatch after apply');
    measured(stageTimings, 'WORKER_HEALTH', () => execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/api/health/live'], { stdio: 'ignore' }));
    const afterFacts = readLiveFacts();
    const deployedDbHead = afterFacts.LIVE_PRISMA_HEAD;
    if (!deployedDbHead || deployedDbHead === 'NONE') die('post-release Prisma head is missing');
    const prismaPaths = git(['ls-tree', '-r', '--name-only', candidate, 'packages/db/prisma/migrations']).split('\n').filter(name => name.endsWith('/migration.sql'));
    const candidateDbHead = prismaPaths.map(name => name.split('/').at(-2)).sort().at(-1);
    if (deployedDbHead !== candidateDbHead || (adapter.ciReceipt && deployedDbHead !== adapter.ciReceipt.prismaHead)) die('post-release Prisma head does not match candidate');
    if (afterFacts.LIVE_AGATA_HEAD !== adapter.agataIdentity.head) die('post-release Alembic head mismatch');
    if (!adapter.gsalesIdentity || afterFacts.LIVE_GSALES_HEAD !== adapter.gsalesIdentity.migrationHead ||
        afterFacts.LIVE_GSALES_MIGRATION_SHA256 !== adapter.gsalesIdentity.migrationSha256 ||
        adapter.gsalesIdentity.buildSha !== candidate || adapter.gsalesIdentity.ready !== true) die('post-release GSales head/checksum/readiness mismatch');
    const ref = `gpoint-production-${startedAt.replace(/[-:.]/g, '')}`;
    measured(stageTimings, 'PRODUCTION_REF_CREATE', () => execFileSync('git', ['tag', '-a', ref, candidate, '-m', `GPoint production release\nproductionSha=${candidate}\ndbHead=${deployedDbHead}\nreleaseId=${releaseId}`], { cwd: ROOT, stdio: 'inherit' }));
    const remoteRef = measured(stageTimings, 'PRODUCTION_REF_PUSH_AND_VERIFY', () => pushProductionTag(ref, candidate));
    Object.assign(stageTimings, adapter.timings ?? {}, { TOTAL_RELEASE: Number(((Date.now() - totalStarted) / 1000).toFixed(1)) });
    console.log(`RELEASE_STAGE_TIMING=${JSON.stringify(stageTimings)}`);
    const artifactIdentities = { api: { sourceSha: candidate, adapter: adapter.imageIdentity }, worker: { sourceSha: candidate, adapter: adapter.imageIdentity }, bot: { sourceSha: candidate, adapter: adapter.imageIdentity }, miniapp: { sourceSha: candidate, adapter: adapter.imageIdentity }, agataApi: { sourceSha: candidate, adapter: adapter.agataIdentity }, agataWorker: { sourceSha: candidate, adapter: adapter.agataIdentity } };
    for (const role of ['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker']) artifactIdentities[role] = { sourceSha: candidate, adapter: adapter.gsalesIdentity };
    const readiness = observeDeploymentReadiness('COMPLETE', readWorkerDependencies, { gsalesInferenceReady: adapter.gsalesIdentity.inferenceReady });
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: candidate, dbHeadBefore: liveDbHead, dbHeadAfter: deployedDbHead, agataDbHeadBefore: baseline.agataDbHead, agataDbHeadAfter: adapter.agataIdentity.head, agataRuntimeSha: candidate, agataProvider: adapter.agataIdentity.provider, artifactIdentities, startedAt, finishedAt: isoNow(), status: 'COMPLETE', failureStage: null, failureReason: null, rollbackSha: current.currentProductionSha, rollbackDbCompatibility: 'REQUIRES_FORWARD_FIX_ASSESSMENT', productionRef: ref, remoteProof: { mainSha: proof.mainSha }, migrationOutput: adapter.migrationOutput, migrationProof: adapter.migrationProof, detectorSourceHash: adapter.detectorHash, stageTimings, releaseOrigin: 'durable-production-release', agataMigrationReadiness };
    Object.assign(manifest, readiness);
    manifest.workerCompatibility = workerCompatibility;
    manifest.ciReceipt = adapter.ciReceipt ?? null;
    Object.assign(manifest, { workerCompatibility, gsalesReadiness, gsalesDbHeadBefore: baseline.facts.LIVE_GSALES_HEAD,
      gsalesDbHeadAfter: adapter.gsalesIdentity.migrationHead, gsalesRuntime: adapter.gsalesIdentity,
      effectiveConfigSha256: adapter.effectiveConfigSha256, authorizationMode: 'EXACT_SHA' });
    const file = writeManifest(manifest);
    const verifiedState = verifiedRuntimeState(current, { candidate, dbHead: deployedDbHead });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...verifiedState, currentProductionRef: ref, previousProductionSha: current.currentProductionSha, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: manifest.finishedAt, ...readiness, gsalesDbHead: adapter.gsalesIdentity.migrationHead, gsalesMigrationSha256: adapter.gsalesIdentity.migrationSha256, releaseOrigin: 'durable-production-release', agataRuntimeSha: candidate, agataAlembicHead: adapter.agataIdentity.head, agataProvider: adapter.agataIdentity.provider, artifactIdentities: manifest.artifactIdentities }, null, 2)}\n`);
    console.log(JSON.stringify({ status: 'COMPLETE', ...readiness, releaseId, candidateSha: candidate, productionRef: ref, manifest: path.relative(ROOT, file) }, null, 2));
  } catch (error) {
    const failure = classifyReleaseFailure({ switchStarted, migrationStarted, agataStartStarted });
    const { switched, partiallyMutated, releaseState } = failure;
    const failureManifest = {
      releaseId,
      candidateSha: candidate,
      previousProductionSha: current.currentProductionSha,
      productionShaAfter: null,
      dbHeadBefore: current.currentProductionDbHead,
      dbHeadAfter: null,
      startedAt,
      finishedAt: isoNow(),
      status: releaseState, serverDeployStatus: releaseState === 'RECOVERY_REQUIRED' ? 'RECOVERY_REQUIRED' : 'FAILED', workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED',
      agataProvider: validatedProvider,
      ...failureEvidence(error, { stage: error.releaseStage, fallbackStage: failure.failureStage }),
      rollbackSha: current.currentProductionSha,
      productionRef: null,
      releaseOrigin: 'durable-production-release'
    };
    const manifestFile = writeManifest(failureManifest);
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...current, ...(validatedProvider ? { agataProvider: validatedProvider } : {}), lastReleaseId: releaseId, lastReleaseStatus: releaseState, serverDeployStatus: failureManifest.serverDeployStatus, workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED', dependencyCheckedAt: null, recoveryRequired: switched || partiallyMutated, lastReleaseFailureManifest: manifestReference(manifestFile), lastReleaseFailureAt: failureManifest.finishedAt }, null, 2)}\n`);
    console.error(JSON.stringify({ status: releaseState, serverDeployStatus: failureManifest.serverDeployStatus, workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED', releaseId, candidateSha: candidate, failureStage: failureManifest.failureStage, failureReason: error.message }));
    throw error;
  } finally { unlock(); }
}

function finalizeRecovery(candidate) {
  const startedAt = isoNow();
  const releaseId = `gpoint-recovery-${startedAt.replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const current = state();
    if (current.recoveryRequired !== true || current.lastReleaseStatus !== 'RECOVERY_REQUIRED') die('no unresolved RECOVERY_REQUIRED release is recorded');
    const failedReference = current.lastReleaseFailureManifest ?? '';
    if (!/^docs\/release\/manifests\/gpoint-[a-zA-Z0-9_.-]+\.json$/.test(failedReference)) die('invalid recovery manifest reference');
    const failedManifestPath = path.join(MANIFEST_DIR, path.basename(failedReference));
    if (!failedManifestPath.startsWith(`${MANIFEST_DIR}${path.sep}`) || !fs.existsSync(failedManifestPath)) die('recovery failure manifest is missing or outside the release manifest directory');
    const failed = readJson(failedManifestPath);
    if (failed.status !== 'RECOVERY_REQUIRED' || failed.candidateSha !== candidate || failed.releaseId !== current.lastReleaseId) die('candidate does not match the unresolved release attempt');
    assertSha(candidate);
    if (git(['rev-parse', candidate]) !== candidate) die('recovery candidate is not present locally');

    const remoteMain = runGitHubRemoteGit(['ls-remote', 'origin', 'refs/heads/main']).split(/\s+/)[0];
    if (!SHA_RE.test(remoteMain ?? '') || remoteMain !== git(['rev-parse', 'origin/main'])) die('origin/main exact SHA could not be independently reconciled');
    try { execFileSync('git', ['merge-base', '--is-ancestor', candidate, 'origin/main'], { cwd: ROOT, stdio: 'ignore' }); }
    catch { die('recovery candidate is not contained in verified origin/main history'); }

    const facts = readLiveFacts();
    const prismaFiles = git(['ls-tree', '-r', '--name-only', candidate, 'packages/db/prisma/migrations']).split('\n').filter(name => name.endsWith('/migration.sql'));
    const expectedPrismaHead = prismaFiles.map(name => name.split('/').at(-2)).sort().at(-1);
    const runtimeFailures = recoveryRuntimeFailures(candidate, facts, expectedPrismaHead, undefined, candidateAgataMigrationHead(candidate));
    if (runtimeFailures.length) die(`live runtime is not safe to finalize: ${runtimeFailures.join('; ')}`);
    const root = measured({}, 'RECOVERY_ROOT_HEALTH', () => execFileSync('curl', ['-fsS', '--connect-timeout', '3', '--max-time', '10', 'https://gpoint.amiora.one/'], { encoding: 'utf8' }));
    if (!root.includes(`gpoint-build-sha" content="${candidate}`)) die('external Mini App root does not serve the recovery candidate');
    measured({}, 'RECOVERY_API_HEALTH', () => execFileSync('curl', ['-fsS', '--connect-timeout', '3', '--max-time', '10', 'https://gpoint.amiora.one/api/health/live'], { stdio: 'ignore' }));
    const messagesStatus = execFileSync('curl', ['-sS', '-o', os.devNull, '-w', '%{http_code}', '--connect-timeout', '3', '--max-time', '10', 'https://gpoint.amiora.one/api/messages/state'], { encoding: 'utf8' }).trim();
    const ageStatus = execFileSync('curl', ['-sS', '-o', os.devNull, '-w', '%{http_code}', '--connect-timeout', '3', '--max-time', '10', 'https://gpoint.amiora.one/api/age-assurance/state'], { encoding: 'utf8' }).trim();
    if (messagesStatus !== '401' || ageStatus !== '401') die(`Messages/Age routes did not return the expected unauthenticated guard: messages=${messagesStatus} age=${ageStatus}`);
    for (const route of ['/messages', '/model/models_hub']) {
      const html = execFileSync('curl', ['-fsS', '--connect-timeout', '3', '--max-time', '10', `https://gpoint.amiora.one${route}`], { encoding: 'utf8' });
      if (!html.includes(`gpoint-build-sha" content="${candidate}`)) die(`Mini App route shell mismatch at ${route}`);
    }
    const gsalesIdentity = { migrationHead: facts.LIVE_GSALES_HEAD, migrationSha256: facts.LIVE_GSALES_MIGRATION_SHA256 };
    // Derive expected migration checksum from exact candidate, not the live response.
    const expectedSalesChecksum = crypto.createHash('sha256').update(execFileSync('git', ['show', `${candidate}:services/gsales/migrations/001_gsales.sql`], { cwd: ROOT })).digest('hex');
    if (gsalesIdentity.migrationHead !== '001_gsales' || gsalesIdentity.migrationSha256 !== expectedSalesChecksum) die('recovery GSales migration identity mismatch');
    const gsalesRuntime = parseGsalesRuntime(runRemoteCommand(gsalesRuntimeCommand(candidate, gsalesIdentity)), candidate, gsalesIdentity);
    const provider = facts.LIVE_AGATA_PROVIDER;
    const agataSmoke = runRemoteCommand(`set -e; docker exec compose-agata-api-1 python -c "from app.config import get_settings; assert get_settings().llm_provider == '${provider}'; print('AGATA_PROVIDER_VERIFIED=${provider}')"; docker run --rm --network gpoint_app --env-file /opt/gpoint/env/production.env --entrypoint node gpoint-runtime:${candidate} -e 'fetch("http://agata-api:8000/internal/v1/users/gpoint-release-readiness/age-assurance", {headers:{authorization:"Bearer "+process.env.AGATA_INTERNAL_API_TOKEN}}).then(async r=>{if(!r.ok) throw Error("age readiness HTTP "+r.status); await r.arrayBuffer(); console.log("AGE_ASSURANCE_INTERNAL_READINESS=PASS")}).catch(e=>{console.error(e.message);process.exit(1)})'; echo AGATA_RECOVERY_SMOKE=PASS`);
    if (!agataSmoke.includes(`AGATA_PROVIDER_VERIFIED=${provider}`) || !agataSmoke.includes('AGE_ASSURANCE_INTERNAL_READINESS=PASS')) die('AGATA/age-assurance recovery smoke failed');

    const ref = `gpoint-production-${startedAt.replace(/[-:.]/g, '')}`;
    const localTag = spawnSync('git', ['show-ref', '--verify', '--quiet', `refs/tags/${ref}`], { cwd: ROOT, stdio: 'ignore' });
    if (localTag.status === 0) die(`production ref already exists locally: ${ref}`);
    execFileSync('git', ['tag', '-a', ref, candidate, '-m', `GPoint production recovery finalization\nproductionSha=${candidate}\ndbHead=${facts.LIVE_PRISMA_HEAD}\nreleaseId=${failed.releaseId}`], { cwd: ROOT, stdio: 'inherit' });
    const remoteRef = pushProductionTag(ref, candidate);

    const readiness = observeDeploymentReadiness('COMPLETE', readWorkerDependencies, { gsalesInferenceReady: gsalesRuntime.inferenceReady });
    const finishedAt = isoNow();
    const recoveryManifest = {
      releaseId, eventType: 'RECOVERY_FINALIZATION', failedReleaseId: failed.releaseId,
      candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: candidate,
      dbHeadBefore: current.currentProductionDbHead, dbHeadAfter: facts.LIVE_PRISMA_HEAD,
      agataDbHeadAfter: facts.LIVE_AGATA_HEAD, agataRuntimeSha: candidate, agataProvider: provider,
      artifactIdentities: recoveryArtifactIdentities(candidate, facts), gsalesRuntime, gsalesDbHeadAfter: gsalesIdentity.migrationHead,
      startedAt, finishedAt, status: 'COMPLETE', ...readiness, failureStage: null, failureReason: null,
      productionRef: ref, remoteProductionRefTarget: remoteRef, remoteMainSha: remoteMain,
      runtimeFacts: Object.fromEntries(Object.entries(facts).filter(([key]) => key.startsWith('LIVE_'))),
      smoke: { root: 'PASS', apiHealth: 'PASS', messagesUnauthenticatedRoute: 'PASS', ageUnauthenticatedRoute: 'PASS', messagesRouteShell: 'PASS', modelRouteShell: 'PASS', ageInternalReadiness: 'PASS', provider },
      recoveryBasis: 'Live runtime, API/Worker/Bot/AGATA image parity, Mini App build SHA, Prisma/Alembic heads, and health/smoke checks were independently verified after the original release apply switched runtime but failed AGATA identity parsing.'
    };
    const manifestFile = writeManifest(recoveryManifest);
    const verifiedState = verifiedRuntimeState(current, { candidate, dbHead: facts.LIVE_PRISMA_HEAD });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...verifiedState, currentProductionRef: ref, previousProductionSha: current.currentProductionSha, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: finishedAt, ...readiness, gsalesDbHead: gsalesIdentity.migrationHead, gsalesMigrationSha256: gsalesIdentity.migrationSha256, releaseOrigin: 'durable-production-release', agataRuntimeSha: candidate, agataAlembicHead: facts.LIVE_AGATA_HEAD, agataProvider: provider, artifactIdentities: recoveryManifest.artifactIdentities, recoveryRequired: false, lastReleaseFailureManifest: manifestReference(failedManifestPath), recoveryFinalizationManifest: manifestReference(manifestFile) }, null, 2)}\n`);
    console.log(JSON.stringify({ status: 'COMPLETE', candidateSha: candidate, productionRef: ref, recoveryManifest: manifestReference(manifestFile), failedAttemptPreserved: manifestReference(failedManifestPath) }, null, 2));
  } finally { unlock(); }
}

function recheckWorkerDependency(candidate) {
  assertSha(candidate);
  const releaseId = `gpoint-dependency-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const current = state();
    // Validate scope before any observation. Preserve the completed server identity.
    dependencyRecheckState(current, candidate, { serverDeployStatus: 'COMPLETE' });
    if (current.gsalesDbHead !== '001_gsales' || !/^[a-f0-9]{64}$/.test(current.gsalesMigrationSha256 ?? '')) die('recorded GSales migration proof is missing');
    let salesInferenceReady;
    try {
      const identity = { migrationHead: current.gsalesDbHead, migrationSha256: current.gsalesMigrationSha256 };
      const runtime = parseGsalesRuntime(runRemoteCommand(gsalesRuntimeCommand(candidate, identity, { attempts: 1 })), candidate, identity);
      salesInferenceReady = runtime.inferenceReady;
    } catch { salesInferenceReady = false; }
    const readiness = observeDeploymentReadiness('COMPLETE', readWorkerDependencies, { gsalesInferenceReady: salesInferenceReady });
    const updated = dependencyRecheckState(current, candidate, readiness);
    const manifest = { releaseId, eventType: 'DEPENDENCY_RECHECK', serverReleaseId: current.lastReleaseId,
      candidateSha: candidate, status: 'COMPLETE', ...readiness, inferenceAcceptance: current.inferenceAcceptance ?? 'NOT_VERIFIED',
      productionRef: current.currentProductionRef, runtimeSwitchOccurred: false, databaseApplyOccurred: false,
      finishedAt: isoNow() };
    writeManifest(manifest);
    fs.writeFileSync(STATE_FILE, `${JSON.stringify(updated, null, 2)}\n`);
    console.log(JSON.stringify(manifest));
  } finally { unlock(); }
}

function main() {
  let standaloneLedger;
  if (Boolean(process.env.GPOINT_RELEASE_STATE_FILE) !== Boolean(process.env.GPOINT_RELEASE_MANIFEST_DIR)) die('release evidence paths must be supplied together');
  if (!process.env.GPOINT_RELEASE_STATE_FILE && !process.env.GPOINT_RELEASE_MANIFEST_DIR) {
    const ledger = loadReleaseBookkeeping({ root: ROOT, git: ledgerGit });
    standaloneLedger = ledger;
    STATE_FILE = ledger.stateFile; MANIFEST_DIR = ledger.manifestDir;
  }
  const args = process.argv.slice(2);
  if (args[0] === 'status' || args.includes('--status')) return status();
  if (!args.includes('--recheck-worker')) runLocalDiskPreflight();
  if (args.includes('--provision-agata-config')) return provisionAgataConfig(candidateSha(args));
  function persistAction(action) {
    const previous = new Set(fs.readdirSync(MANIFEST_DIR));
    try { return action(); } finally {
      if (standaloneLedger) {
        const records = fs.readdirSync(MANIFEST_DIR).filter(name => !previous.has(name)).map(name => ({ name, data: readJson(path.join(MANIFEST_DIR, name)) }));
        const evidence = records.filter(r => r.data.status !== 'PLANNED').at(-1)?.data;
        if (evidence) persistReleaseBookkeeping({ mode: 'production', records, evidence, git: ledgerGit, ...standaloneLedger });
      }
    }
  }
  if (args.includes('--provision-gsales-config')) return persistAction(() => provisionGsalesConfig(candidateSha(args), args[args.indexOf('--owner-approved-sha') + 1]));
  if (args.includes('--configure-gsales-worker')) return persistAction(() => configureGsalesWorker(candidateSha(args), args[args.indexOf('--owner-approved-sha') + 1]));
  if (args.includes('--recheck-worker')) return persistAction(() => recheckWorkerDependency(candidateSha(args)));
  if (args.includes('--finalize-recovery')) return persistAction(() => finalizeRecovery(candidateSha(args)));
  const isDryRun = args.includes('--dry-run');
  const isApply = args.includes('--apply');
  if (!isDryRun && !isApply) die('choose --dry-run or --apply');
  if (isApply && !args.includes('--confirm-production-apply')) die('apply requires --confirm-production-apply');
  if (isApply) return persistAction(() => runApply(candidateSha(args)));
  runDryRun(candidateSha(args));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) { try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; } }
