#!/usr/bin/env node
import fs from 'node:fs';
import { observeDeploymentReadiness, dependencyRecheckState, collectWorkerDependencies,
  } from './release-readiness.mjs';
import { gsalesRuntimeCommand, parseGsalesRuntime } from './release-gsales.mjs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { migrationHead } from './release-migrations.mjs';
import { gsalesProvisionCommand } from './release-gsales-provision.mjs';
import { verifyReleaseBundle } from './release-bundle.mjs';
import { validateOciReceipt } from './release-oci.mjs';
import { buildV5ComponentImageInventory, deriveV3ProductionParent, verifyV5ProductionParent } from './release-component-bundle-v4.mjs';
import { createGitHubDeploymentsClient } from './github-deployments.mjs';
import { fetchOriginalDeployEvidence, fetchAuthorizationReconciliationEvidence, fetchInterruptedApplyRecoveryEvidence, safeReconciliationFailure } from './reconcile-deployment.mjs';
import { AuthorizationError, authorizationRecordPath, candidateScopeDiff,
  checkTaskAuthorizationState, validateTaskAuthorization } from './release-authorization.mjs';
import { expectedStaticParent, staticReceiptAndImages, verifyStaticLiveBaseline,
  v4NativeProofOnVPS, verifyV4SourceAtCheckout } from './release-static-v4.mjs';
import { applyStaticArtifactV4 } from './release-static-transfer-v4.mjs';

import { acquireReleaseLock } from './release-lock.mjs';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';
import { agataProductionMigrationCommand, applyCandidate, cleanupTemporaryWorktree, inspectProductionReadOnly, inspectAgataMigrationReadiness, inspectGsalesReadiness, planReleaseRetention, provisionAgataInternalConfigForBaseline, upload, runRemoteCommand, parseGuardedMigrationEvidence, validateAgataProviderMode, validateAgataProviderCompatibility } from './release-gpoint-vps-adapter.mjs';
import { initializeRunScopedState, stateFromLiveVps } from './deployment-run-state.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
let STATE_FILE = process.env.GPOINT_RELEASE_STATE_FILE || path.join(ROOT, 'docs/release/state/production.json');
let MANIFEST_DIR = process.env.GPOINT_RELEASE_MANIFEST_DIR || path.join(ROOT, 'docs/release/manifests');
// Keep the lock outside Actions checkout: checkout cleanup must never erase a
// stale release lock left by interruption, and all Mac checkouts share it.
const LOCK_FILE = path.join(os.homedir(), '.config/gpoint/release-production.lock');
const LEGACY_LOCK_FILE = path.join(ROOT, '.gpoint-release.lock');
const SHA_RE = /^[0-9a-f]{40}$/;
const GITHUB_SOURCE_URL = 'https://github.com/akhripunov/gpoint.git';
const REMOTE_VERIFY_TIMEOUT_MS = 15_000;
const GITHUB_REMOTE_TIMEOUT_MS = 20_000;
const NETWORK_RETRY_BUDGET_MS = 30 * 60 * 1000;
const NETWORK_BACKOFF_CAP_MS = 60_000;
const GIB = 1024 ** 3;
const LOCAL_DISK_HARD_MIN_BYTES = 10 * GIB;
const LOCAL_DISK_TARGET_BYTES = 15 * GIB;
const LOCAL_CACHE_CLEANUP_TIMEOUT_MS = 30_000;
function githubGitBaseEnvironment(baseEnv) {
  const env = {...baseEnv};
  return env;
}
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

/** Attach Actions credentials only to read-only refs for this private repository. */
export function githubSourceGitEnvironment(args, token, baseEnv = process.env) {
  const env = githubGitBaseEnvironment(baseEnv);
  delete env.GITHUB_TOKEN;
  delete env.GH_TOKEN;
  env.GIT_TERMINAL_PROMPT = '0';
  const canonicalRepo = args[0] === 'ls-remote' && args[1] === GITHUB_SOURCE_URL;
  const mainOnly = canonicalRepo && args.length === 3 && args[2] === 'refs/heads/main';
  if (!mainOnly) {
    const touchesSourceRefs = args.some(value => typeof value === 'string'
      && (value.includes('refs/heads/') || value.includes('refs/tags/')));
    if (touchesSourceRefs) throw new Error('GitHub source Git operation is not authorized');
    return env;
  }
  if (!token) throw new Error('GITHUB_TOKEN is required for private GitHub source verification');
  const offset = Number(env.GIT_CONFIG_COUNT ?? 0);
  if (!Number.isSafeInteger(offset) || offset < 0) throw new Error('Invalid inherited Git config count');
  env.GIT_CONFIG_COUNT = String(offset + 3);
  env[`GIT_CONFIG_KEY_${offset}`] = 'credential.helper';
  env[`GIT_CONFIG_VALUE_${offset}`] = '';
  env[`GIT_CONFIG_KEY_${offset + 1}`] = 'http.https://github.com/.extraheader';
  env[`GIT_CONFIG_VALUE_${offset + 1}`] = '';
  env[`GIT_CONFIG_KEY_${offset + 2}`] = 'http.https://github.com/.extraheader';
  env[`GIT_CONFIG_VALUE_${offset + 2}`] = `AUTHORIZATION: basic ${Buffer.from(`x-access-token:${token}`).toString('base64')}`;
  return env;
}

function recheckTaskAuthorizationBeforeMutation(candidateSha, boundary) {
  if (process.env.RELEASE_AUTHORIZATION_MODE !== 'TASK_SCOPED_AUTO') return;
  const authorizationId = process.env.RELEASE_AUTHORIZATION_ID;
  const token = process.env.GITHUB_TOKEN;
  if (!authorizationId || !token) die(`TASK_SCOPED_AUTO authorization is unavailable before ${boundary}`);
  const env = {...process.env};
  delete env.GITHUB_TOKEN;
  delete env.GH_TOKEN;
  const authorizationModule = new URL('./release-authorization.mjs', import.meta.url).href;
  const script = `import {checkTaskAuthorizationState} from ${JSON.stringify(authorizationModule)};
const token = await new Promise(resolve => { let value = ''; process.stdin.setEncoding('utf8'); process.stdin.on('data', chunk => value += chunk); process.stdin.on('end', () => resolve(value.trim())); });
try { await checkTaskAuthorizationState({project: 'gpoint', authorizationId: ${JSON.stringify(authorizationId)}, candidateSha: ${JSON.stringify(candidateSha)}, token}); process.stdout.write('TASK_AUTHORIZATION_MUTATION_GATE=PASS\\n'); }
catch { process.stderr.write('TASK_AUTHORIZATION_MUTATION_GATE=BLOCKED\\n'); process.exitCode = 1; }`;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {cwd: ROOT, env, input: token, encoding: 'utf8',
    timeout: 30_000, stdio: ['pipe', 'pipe', 'pipe']});
  if (result.error || result.status !== 0 || result.stdout.trim() !== 'TASK_AUTHORIZATION_MUTATION_GATE=PASS') {
    die(`TASK_SCOPED_AUTO authorization could not be revalidated before ${boundary}`);
  }
}
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
  return fs.readdirSync(MANIFEST_DIR)
    .filter(name => name.endsWith('.json') && name.startsWith('gpoint-release-'))
    .map(name => readJson(path.join(MANIFEST_DIR, name)))
    .filter(manifest => /^[0-9a-f]{40}$/.test(manifest.candidateSha ?? ''))
    .map(manifest => ({sha: manifest.candidateSha, status: manifest.status}));
}

function hasUnresolvedRecoveryForSha(sha) {
  return fs.readdirSync(MANIFEST_DIR).filter(name => name.endsWith('.json')).some(name => {
    const record = readJson(path.join(MANIFEST_DIR, name));
    return record.candidateSha === sha && record.status === 'RECOVERY_REQUIRED';
  });
}

export function sanitizeReleaseOutput(value) {
  return String(value ?? '')
    .replace(/((?:password|passwd|token|secret|api[_-]?key|private[_-]?key|database_url|dsn)\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, '$1[REDACTED]')
    .replace(/(postgres(?:ql)?(?:\+[a-z0-9]+)?:\/\/[^:\s/@]+:)[^@\s/]+@/gi, '$1[REDACTED]@')
    .replace(/(\bBearer\s+)[^\s"'`]+/gi, '$1[REDACTED]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g, '[REDACTED]')
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
  const script = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/runtime/current/run_codex_worker.sh');
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
    const file = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/status/worker-status.json');
    const stat = fs.lstatSync(file);
    if (stat.isSymbolicLink() || (stat.mode & 0o077)) throw new Error('unsafe status file');
    const target = readJson(file).targets?.GSALES;
    if (target) sales = { enabled: true, queue_connectivity: ['idle', 'running'].includes(target.state) ? 'reachable' : 'unavailable', last_poll_at: target.last_poll_at, last_heartbeat_at: target.last_heartbeat_at };
  } catch { /* Dependency observation remains NOT_VERIFIED. */ }
  return sales;
  }});
}

function verifyWorkerCompatibility(candidate, facts, mode) {
  if (!['preflight', 'apply'].includes(mode)) die('Worker compatibility mode is invalid');
  // Secrets are compared through stdin only, never arguments, logs or evidence.
  const script = `import json,pathlib\ne=dict(line.split('=',1) for line in pathlib.Path('/opt/gpoint/env/production.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)\nassert e['RELEASE_SHA']=='${facts.LIVE_RELEASE_ENV_SHA}'\nprint(json.dumps({key:e[key] for key in ('GPOINT_AGATA_WORKER_TOKEN','GSALES_WORKER_TOKEN')}))\n`;
  const credentials = runRemoteCommand(`set -e; printf '%s' '${Buffer.from(script).toString('base64')}' | base64 -d | python3 -`);
  const result = spawnSync('python3', [path.join(ROOT, 'scripts/verify-release-worker-compatibility.py'),
    '--candidate', candidate, '--mode', mode, '--gpoint-tenant', facts.LIVE_AGATA_TENANT_ID ?? '',
    '--gsales-tenant', facts.LIVE_GSALES_TENANT_ID ?? ''], { cwd: ROOT, input: credentials, encoding: 'utf8', timeout: 10_000 });
  if (result.status !== 0) {
    const reason = result.stderr.match(/WORKER_COMPATIBILITY_BLOCKED reason=([a-z0-9_]+)/)?.[1];
    die(`Worker ${mode} compatibility blocked${reason ? `: ${reason}` : ''}`);
  }
  const evidence = JSON.parse(result.stdout);
  if (evidence.compatibility !== 'PASS' || evidence.candidateSha !== candidate) die('Worker compatibility evidence invalid');
  if (mode === 'apply' && evidence.sourceStatus !== 'EXACT_MATCH') die('Installed worker source differs from candidate');
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
  if (!switched) return 'FAILED_PRE_APPLY';
  return runtimeVerified ? 'INSTALLATION_UNCONFIRMED' : 'INSTALLATION_UNCONFIRMED';
}

export function classifyReleaseFailure({ switchStarted = false, migrationStarted = false, agataStartStarted = false }) {
  const switched = switchStarted;
  const partiallyMutated = migrationStarted || agataStartStarted;
  return {
    switched,
    partiallyMutated,
    releaseState: switched || partiallyMutated
      ? 'INSTALLATION_UNCONFIRMED'
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

export function candidateAgataMigrationHead(candidate) {
  assertSha(candidate);
  const directory = 'services/agata/migrations/versions';
  const files = git(['ls-tree', '-r', '--name-only', candidate, directory]).split('\n').filter(name => name.endsWith('.py'));
  return migrationHead(files.map(name => git(['show', `${candidate}:${name}`]))).head;
}

export function candidatePrismaMigrationHead(candidate) {
  const files = git(['ls-tree', '-r', '--name-only', candidate, 'packages/db/prisma/migrations'])
    .split('\n').filter(name => name.endsWith('/migration.sql'));
  if (!files.length) return 'NONE';
  return files.map(name => name.split('/').at(-2)).sort().at(-1);
}

export function evaluateSafetyGates({ candidateSha: sha, remoteSha, clean = true, artifacts, config, backupReady = true, migrationOk = true, concurrent = false, recordedProductionSha, liveProductionSha }) {
  const failures = validateCandidate({ candidateSha: sha, remoteSha, clean });
  failures.push(...checkArtifactParity(sha, artifacts));
  failures.push(...checkEffectiveConfig(sha, config));
  if (!backupReady) failures.push('backup gate failed');
  if (!migrationOk) failures.push('migration gate failed');
  if (concurrent) failures.push('concurrent release lock is held');
  if (recordedProductionSha !== liveProductionSha) failures.push('live production SHA drift');
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
  const args = ['ls-remote', GITHUB_SOURCE_URL, 'refs/heads/main'];
  return runner('git', args, {
    cwd: ROOT,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: REMOTE_VERIFY_TIMEOUT_MS,
    maxBuffer: 8 * 1024 * 1024,
    env: githubSourceGitEnvironment(args, process.env.GITHUB_TOKEN),
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
  const main = refs.find(([, ref]) => ref === 'refs/heads/main');
  if (!main) die('origin/main could not be independently verified');
  if (main[0] !== candidate) die(`candidate SHA is not exact origin/main: candidate=${candidate} origin/main=${main[0]}`);
  return { mainSha: main[0], directRef: main[1], refs };
}

function runGitHubRemoteGit(args, { timeout = GITHUB_REMOTE_TIMEOUT_MS } = {}) {
  try {
    return runWithBoundedGitHubRetries(() => execFileSync('git', args, {
      cwd: ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout,
      maxBuffer: 8 * 1024 * 1024,
      env: githubSourceGitEnvironment(args, process.env.GITHUB_TOKEN),
    }).trim());
  } catch (error) {
    if (error?.code === 'NETWORK_UNAVAILABLE') die('NETWORK_UNAVAILABLE during GitHub remote operation');
    die(isTransientGitHubTransportFailure(error)
      ? 'GitHub remote operation stopped after a retryable transport error'
      : 'GitHub remote operation failed on a non-transient error; refusing retry');
  }
}

function readLiveFacts() {
  const output = inspectProductionReadOnly();
  const facts = {};
  for (const line of output.split(/\r?\n/)) {
    for (const match of line.matchAll(/(?:^|\s)([A-Z][A-Z0-9_]+)=([^\s]+)/g)) facts[match[1]] = match[2];
  }
  return facts;
}

function readGitHubReadToken() {
  const supplied = process.env.GITHUB_TOKEN || process.env.GH_TOKEN;
  if (supplied) return supplied;
  try {
    return execFileSync('gh', ['auth', 'token'], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10_000}).trim();
  } catch {
    die('GitHub Deployment read authorization is unavailable for V5 parent verification');
  }
}

export async function loadV5ParentDeployEvidence(verified, candidateSha, token,
  {fetchOriginal = fetchOriginalDeployEvidence, fetchReconciled = fetchAuthorizationReconciliationEvidence, fetchRecovered = fetchInterruptedApplyRecoveryEvidence} = {}) {
  const identity = verified.identity;
  const tuple = verified.authorizationReconciliation;
  const recovery = verified.recoveryAttestation;
  if (tuple && recovery) die('Conflicting completed Deployment attestation paths');
  // GHCR_READ_TOKEN is intentionally stripped from the dry-run child environment.
  // Reuse the existing job-scoped GitHub token only for reading the exact parent
  // OCI artifact, without passing an additional registry secret to subprocesses.
  const common = {candidateSha, deploymentId: verified.deployment.id,
    prepareRunId: identity.prepareRunId, prepareRunAttempt: identity.prepareRunAttempt,
    prepareReceiptDigest: identity.prepareReceiptDigest, token,
    registryUsername: process.env.GHCR_READ_USERNAME, registryToken: token};
  let evidence;
  try {
    evidence = recovery
      ? await fetchRecovered({...common,runId:recovery.runId,attempt:recovery.runAttempt,
          originalRunId:identity.runId,originalRunAttempt:identity.runAttempt,
          prepareReceiptSha256:identity.prepareReceiptSha256})
      : tuple
        ? await fetchReconciled({...common, runId: tuple.runId, attempt: tuple.runAttempt,
            originalRunId: identity.runId, originalRunAttempt: identity.runAttempt,
            prepareReceiptSha256: identity.prepareReceiptSha256})
        : await fetchOriginal({...common, runId: identity.runId, attempt: identity.runAttempt});
  } catch (error) {
    die('Exact Deploy OCI evidence, Actions provenance, or VPS verification evidence is unavailable ' +
      safeReconciliationFailure(error));
  }
  const completeEvidence = evidence?.status === 'COMPLETE' && evidence.releaseState === 'COMPLETE' &&
    (recovery ? evidence.deployRunConclusion === 'failure' && evidence.recoveryRunConclusion === 'success' &&
      evidence.recoveryAttestationVerified === true
      : tuple ? evidence.deployRunConclusion === 'failure' && evidence.reconciliationRunConclusion === 'success' &&
        evidence.authorizationReconciliationVerified === true : evidence.deployRunConclusion === 'success');
  const reconciliableEvidence = !tuple && evidence?.status === 'INSTALLATION_UNCONFIRMED' &&
    evidence.releaseState === 'INSTALLATION_UNCONFIRMED' && evidence.executionFailure?.stage === 'STATUS_UNCONFIRMED' &&
    evidence.applicationVerification === 'VPS_READBACK_VERIFIED' && evidence.deployRunConclusion === 'failure' &&
    verified.status.state === 'success';
  if (verified.status.state !== 'success' || identity.authorizationMode !== evidence?.authorizationMode ||
      identity.authorizationId !== evidence?.authorizationId || evidence?.candidateSha !== candidateSha ||
      !(completeEvidence || reconciliableEvidence) || evidence.applicationVerification !== 'VPS_READBACK_VERIFIED' ||
      evidence.recoveryRequired === true)
    die('Durable Deploy result does not prove the exact completed production release');
  return evidence;
}

async function verifiedV5ParentEvidence(current, facts, currentAttempt = null) {
  if (!SHA_RE.test(current?.currentProductionSha ?? '') ||
      facts.LIVE_RELEASE_ENV_SHA !== current.currentProductionSha ||
      hasUnresolvedRecoveryForSha(current.currentProductionSha))
    die('V5 production parent live SHA is mismatched or an unresolved recovery record exists');
  const api = createGitHubDeploymentsClient({token: readGitHubReadToken()});
  let verified;
  try { verified = await api.readSuccessfulProductionDeploymentForSha(current.currentProductionSha, 'FULL_RELEASE', currentAttempt); }
  catch (error) {
    if (/^No successful FULL_RELEASE Deployment exists for the exact SHA$/.test(error?.message ?? '')) return null;
    die('GitHub does not confirm one stable successful exact-SHA FULL_RELEASE Deployment');
  }
  const identity = verified.identity;
  const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || readGitHubReadToken();
  const evidence = await loadV5ParentDeployEvidence(verified, current.currentProductionSha, token);
  const receipt = evidence.ciReceipt;
  const componentImages = evidence.componentImages;
  if (!componentImages || !['app', 'agata', 'gsales'].every(role => componentImages[role]) ||
      receipt?.selectedTransport !== 'ghcr-oci' || receipt.ghcrVerified !== true || receipt.ghcrPostSwitchVerified !== true ||
      receipt.ghcrManifestDigest !== identity.prepareReceiptDigest ||
      receipt.prepareReceiptSha256 !== identity.prepareReceiptSha256 ||
      !/^sha256:[a-f0-9]{64}$/.test(receipt.ghcrManifestDigest ?? '') ||
      !/^[a-f0-9]{64}$/.test(receipt.prepareReceiptSha256 ?? ''))
    die('Durable Deploy result lacks the exact verified Prepare receipt and image inventory');
  if (typeof receipt.prepareReceiptJson !== 'string' ||
      crypto.createHash('sha256').update(receipt.prepareReceiptJson).digest('hex') !== receipt.prepareReceiptSha256)
    die('Durable Prepare receipt bytes do not match the Deployment receipt checksum');
  let exactReceipt;
  try {
    const parsed = JSON.parse(receipt.prepareReceiptJson);
    exactReceipt = validateOciReceipt(parsed, current.currentProductionSha,
      {runId: identity.prepareRunId, runAttempt: identity.prepareRunAttempt});
  } catch { die('Durable Prepare receipt is invalid or does not match its exact candidate and run'); }
  if (identity.candidateSha !== current.currentProductionSha ||
      identity.prepareReceiptDigest !== receipt.ghcrManifestDigest ||
      identity.prepareReceiptSha256 !== receipt.prepareReceiptSha256 ||
      String(identity.prepareRunId) !== String(receipt.runId) ||
      String(identity.prepareRunAttempt) !== String(receipt.runAttempt))
    die('GitHub Deployment SHA or Prepare provenance does not match the V5 receipt');
  if (identity.authorizationMode === 'TASK_SCOPED_AUTO') {
    try {
      const authFile = authorizationRecordPath(identity.authorizationId);
      const record = JSON.parse(git(['show', `${current.currentProductionSha}:${authFile}`]));
      const changedPaths = candidateScopeDiff(ROOT, identity.authorizationId,
        record.production_baseline_sha, current.currentProductionSha);
      const authorization = validateTaskAuthorization({record, project: 'gpoint', authorizationId: identity.authorizationId,
        candidateSha: current.currentProductionSha, currentMainSha: current.currentProductionSha,
        productionSha: record.production_baseline_sha, changedPaths});
      if (authorization.recordSha256 !== evidence.authorizationRecordSha256 ||
          evidence.authorizationConsumed !== true || evidence.authorizationScopeSha256 !== authorization.ownerTaskCommitmentSha256)
        throw new AuthorizationError('Durable authorization identity differs from Deploy evidence');
      await checkTaskAuthorizationState({project: 'gpoint', authorizationId: identity.authorizationId,
        candidateSha: current.currentProductionSha, consumedCandidateSha: current.currentProductionSha, token});
    } catch { die('TASK_SCOPED_AUTO owner record or exact consumed authorization state is not verified'); }
  }
  // The hash-bound Prepare receipt separately attests OCI configDigest and the
  // containerd nativeId established by the exact disposable deploy rehearsal.
  // Runtime Docker image .Id is nativeId, NOT the OCI config digest.
  const nativeInventory = Object.fromEntries(['app', 'agata', 'gsales'].map(role => {
    const image = exactReceipt.images?.[role];
    const rehearsal = exactReceipt.deployRehearsal?.images?.[role];
    if (!/^sha256:[a-f0-9]{64}$/.test(rehearsal?.nativeId ?? '') ||
        rehearsal.nativeId === image?.configDigest ||
        rehearsal.configDigest !== image?.configDigest ||
        rehearsal.digest !== image?.digest ||
        rehearsal.sourceSha !== current.currentProductionSha ||
        componentImages[role].imageId !== image?.configDigest ||
        componentImages[role].registryDigest !== image?.digest)
      die('Immutable V5 rehearsal-native-image identity mismatch ' + role);
    return [role, {...componentImages[role], nativeId: rehearsal.nativeId}];
  }));
  const native = v4NativeProofOnVPS(nativeInventory, runRemoteCommand);
  const nativeImages = Object.fromEntries(Object.entries(native).map(([role, value]) => [role, {
    imageId: value.nativeImageId, sourceSha: value.revisionLabel, platform: `${value.os}/${value.architecture}`,
  }]));
  const freshFacts = readLiveFacts();
  let freshState;
  try { freshState = stateFromLiveVps(freshFacts); }
  catch { die('Fresh VPS baseline became incomplete while verifying the durable V5 parent'); }
  const liveKeys = [...new Set([...Object.keys(facts), ...Object.keys(freshFacts)])]
    .filter(key => key.startsWith('LIVE_') && key !== 'LIVE_DISK_FREE_KB').sort();
  if (liveKeys.some(key => facts[key] !== freshFacts[key]) || Number(freshFacts.LIVE_DISK_FREE_KB) < 5242880 ||
      freshState.currentProductionSha !== current.currentProductionSha ||
      freshState.currentProductionDbHead !== exactReceipt.prismaHead ||
      freshState.agataAlembicHead !== exactReceipt.agataHead ||
      freshState.gsalesMigrationHead !== exactReceipt.gsalesMigrationHead ||
      freshState.gsalesMigrationSha256 !== exactReceipt.gsalesMigrationSha256)
    die('Fresh VPS health, image, migration, configuration, or rollback facts changed during V5 parent verification');
  const roleFactImages = {app: ['LIVE_API_IMAGE', 'LIVE_WORKER_IMAGE', 'LIVE_BOT_IMAGE'],
    agata: ['LIVE_AGATA_API_IMAGE_ID', 'LIVE_AGATA_WORKER_IMAGE_ID',
      'LIVE_GSALES_AGATA_API_IMAGE_ID', 'LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID'],
    gsales: ['LIVE_GSALES_API_IMAGE_ID']};
  for (const [role, keys] of Object.entries(roleFactImages)) {
    if (keys.some(key => freshFacts[key] !== nativeImages[role].imageId))
      die(`Fresh VPS runtime image identity differs from the exact V5 parent for ${role}`);
  }
  const unresolvedRecovery = hasUnresolvedRecoveryForSha(current.currentProductionSha) || evidence.recoveryRequired === true;
  const proof = {
    componentImages,
    deployment: {source: 'github-api', sha: verified.deployment.sha, status: verified.status.state,
      environment: verified.deployment.environment, task: verified.deployment.task,
      productionEnvironment: verified.deployment.production_environment,
      transientEnvironment: verified.deployment.transient_environment,
      runId: identity.runId, runAttempt: identity.runAttempt,
      prepareRunId: identity.prepareRunId, prepareRunAttempt: identity.prepareRunAttempt,
      authorizationMode: identity.authorizationMode, authorizationId: identity.authorizationId,
      receiptDigest: identity.prepareReceiptDigest, imageDigests: identity.imageDigests},
    prepare: {source: 'verified-ghcr-receipt', candidateSha: exactReceipt.candidateSha,
      runId: exactReceipt.runId, runAttempt: exactReceipt.runAttempt,
      receiptDigest: receipt.ghcrManifestDigest, receiptSha256: receipt.prepareReceiptSha256,
      prismaHead: exactReceipt.prismaHead, agataHead: exactReceipt.agataHead,
      gsalesMigrationHead: exactReceipt.gsalesMigrationHead,
      gsalesMigrationSha256: exactReceipt.gsalesMigrationSha256,
      images: Object.fromEntries(['app', 'agata', 'gsales'].map(role => {
        const image = exactReceipt.images?.[role];
        return [role, {manifestDigest: image?.digest, configDigest: image?.configDigest ?? image?.id,
          nativeId: exactReceipt.deployRehearsal?.images?.[role]?.nativeId,
          sourceSha: image?.sourceSha}];
      }))},
    live: {source: 'read-only-vps', runtimeSha: freshFacts.LIVE_RELEASE_ENV_SHA,
      miniappSha: freshFacts.LIVE_MINIAPP_SHA, prismaHead: freshFacts.LIVE_PRISMA_HEAD,
      agataHead: freshFacts.LIVE_AGATA_HEAD, gsalesHead: freshFacts.LIVE_GSALES_HEAD,
      gsalesMigrationSha256: freshFacts.LIVE_GSALES_MIGRATION_SHA256,
      apiHealth: freshFacts.LIVE_API_HEALTH, agataApiHealth: freshFacts.LIVE_AGATA_API_HEALTH,
      gsalesApiHealth: freshFacts.LIVE_GSALES_API_HEALTH,
      gsalesAgataApiHealth: freshFacts.LIVE_GSALES_AGATA_API_HEALTH,
      gsalesAgataWorkerHealth: freshFacts.LIVE_GSALES_AGATA_MEMORY_WORKER_HEALTH,
      workerRunning: freshFacts.LIVE_WORKER_RUNNING === 'true', botRunning: freshFacts.LIVE_BOT_RUNNING === 'true',
      agataWorkerRunning: freshFacts.LIVE_AGATA_WORKER_RUNNING === 'true',
      gsalesAgataWorkerRunning: freshFacts.LIVE_GSALES_AGATA_MEMORY_WORKER_RUNNING === 'true',
      nativeImages},
    unresolvedRecovery,
  };
  const verifiedCurrent = {...current, releaseOrigin: 'github-deployment', recoveryRequired: false,
    lastReleaseStatus: 'COMPLETE', serverDeployStatus: 'COMPLETE', activeRuntimeSha: current.currentProductionSha,
    agataRuntimeSha: current.currentProductionSha, miniappSourceSha: current.currentProductionSha,
    componentImages};
  try { verifyV5ProductionParent(verifiedCurrent, proof); }
  catch (error) { die(`V5 parent evidence verification failed: ${error.message}`); }
  return proof;
}

export function currentApplyDeploymentContext(candidate, bundle, env = process.env) {
  const raw = env.GPOINT_CURRENT_DEPLOYMENT_CONTEXT;
  if (!raw) return null;
  let context;
  try { context = JSON.parse(raw); }
  catch { die('Current Apply Deployment context is malformed'); }
  const identity = context?.identity;
  const expectedRoute = bundle?.staticOnly === true ? 'STATIC_UI_ONLY' : 'FULL_RELEASE';
  const expectedAuthorizationMode = env.TASK_AUTHORIZATION_ID ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA';
  const expectedAuthorizationId = expectedAuthorizationMode === 'TASK_SCOPED_AUTO'
    ? env.TASK_AUTHORIZATION_ID : null;
  const expectedLogUrl = `https://github.com/akhripunov/gpoint/actions/runs/${env.GITHUB_RUN_ID}/attempts/${env.GITHUB_RUN_ATTEMPT}`;
  if (!Number.isSafeInteger(context?.deploymentId) || context.deploymentId < 1 || !identity ||
      identity.candidateSha !== candidate || identity.runId !== String(env.GITHUB_RUN_ID) ||
      identity.runAttempt !== String(env.GITHUB_RUN_ATTEMPT) || identity.logUrl !== expectedLogUrl ||
      identity.prepareRunId !== String(env.PREPARE_RUN_ID) ||
      identity.prepareRunAttempt !== String(env.PREPARE_RUN_ATTEMPT) ||
      identity.prepareReceiptDigest !== env.GPOINT_PREPARE_RECEIPT_DIGEST ||
      identity.prepareReceiptSha256 !== env.GPOINT_PREPARE_RECEIPT_SHA256 ||
      identity.authorizationMode !== expectedAuthorizationMode ||
      (identity.authorizationId || null) !== expectedAuthorizationId ||
      identity.route !== expectedRoute ||
      (expectedRoute === 'STATIC_UI_ONLY' && (identity.staticArtifactDigest !== bundle.staticArtifactDigest ||
        identity.staticArtifactContentSha256 !== bundle.staticArtifactSha256)))
    die('Current Apply Deployment context does not match this exact workflow attempt and receipt');
  if (expectedRoute === 'FULL_RELEASE' && !['app', 'agata', 'gsales'].every(role =>
    identity.imageDigests?.[role] === bundle?.images?.[role]?.digest))
    die('Current Apply Deployment image identity does not match the exact Prepare receipt');
  return context;
}

function readVerifiedPrepareReceiptJson(candidate) {
  const directory = process.env.GPOINT_RELEASE_BUNDLE;
  const expectedSha256 = process.env.GPOINT_PREPARE_RECEIPT_SHA256;
  if (!directory || !/^[a-f0-9]{64}$/.test(expectedSha256 ?? ''))
    die('exact Prepare receipt bytes or checksum are unavailable after Apply');
  const root = fs.realpathSync(directory);
  const file = path.join(root, 'receipt.json');
  if (fs.realpathSync(file) !== file || !file.startsWith(`${root}${path.sep}`) ||
      fs.lstatSync(file).isSymbolicLink() || !fs.statSync(file).isFile())
    die('Prepare receipt path is not a regular in-bundle file');
  const raw = fs.readFileSync(file, 'utf8');
  if (crypto.createHash('sha256').update(raw).digest('hex') !== expectedSha256)
    die('Prepare receipt bytes do not match the immutable Deploy provenance checksum');
  let parsed;
  try { parsed = JSON.parse(raw); } catch { die('Prepare receipt bytes are invalid JSON'); }
  try { validateOciReceipt(parsed, candidate, {runId: parsed.runId, runAttempt: parsed.runAttempt}); }
  catch { die('Prepare receipt does not validate for the exact production candidate'); }
  return raw;
}

export function expectedV3LiveRevisions(current) {
  if (!current?.componentImages) {
    return { runtimeSha: current?.currentProductionSha,
             miniappSha: current?.currentProductionSha,
             agataSha: current?.currentProductionSha, mixed: false };
  }
  const candidate = current.currentProductionSha;
  if (!SHA_RE.test(candidate ?? '') || current.miniappSourceSha !== candidate ||
      current.lastReleaseStatus !== 'COMPLETE' ||
      current.serverDeployStatus !== 'COMPLETE' || current.recoveryRequired !== false)
    die('V4 component baseline not immutable COMPLETE');
  const roles = current.componentImages;
  const source = role => roles?.[role]?.sourceSha;
  if (!['app','agata','gsales'].every(role => SHA_RE.test(source(role) ?? '') &&
      /^sha256:[0-9a-f]{64}$/.test(roles?.[role]?.imageId ?? '') &&
      roles?.[role]?.platform === 'linux/amd64'))
    die('V4 production component source inventory incomplete');
  if (current.activeRuntimeSha !== source('app') || current.agataRuntimeSha !== source('agata'))
    die('V4 production component/runtime ledger drift');
  return { runtimeSha: source('app'), miniappSha: candidate,
           agataSha: source('agata'), mixed: source('app') !== candidate };
}

function assertLiveBaseline(current, facts, { requireAgataConfig = true, parentEvidence = null } = {}) {
  let baselineState = current;
  if (parentEvidence) {
    try {
      const verifiedCurrent = {...current, releaseOrigin: 'github-deployment', recoveryRequired: false,
        lastReleaseStatus: 'COMPLETE', serverDeployStatus: 'COMPLETE', activeRuntimeSha: current.currentProductionSha,
        agataRuntimeSha: current.currentProductionSha, miniappSourceSha: current.currentProductionSha,
        componentImages: parentEvidence.componentImages};
      const parent = verifyV5ProductionParent(verifiedCurrent, parentEvidence);
      baselineState = {...current, ...parent, componentImages: parent.images,
        miniappSourceSha: parent.currentProductionSha,
        activeRuntimeSha: parent.images.app.sourceSha, agataRuntimeSha: parent.images.agata.sourceSha};
    }
    catch (error) { die(`V5 production parent is not independently verified: ${error.message}`); }
  }
  const liveSha = facts.LIVE_RELEASE_ENV_SHA;
  const expected = expectedV3LiveRevisions(baselineState);
  if (!SHA_RE.test(liveSha ?? '') || liveSha !== expected.runtimeSha) die(`live production SHA drift: recorded=${current.currentProductionSha} live=${liveSha ?? 'unavailable'}`);
  if (!facts.LIVE_DOCKER_VERSION || !facts.LIVE_COMPOSE_VERSION || !facts.LIVE_BUILDX_VERSION || facts.LIVE_BUILDKIT !== 'ready') die('production Docker/Compose/Buildx/BuildKit preflight failed');
  if (Number(facts.LIVE_DISK_FREE_KB) < 5242880) die('production disk preflight requires at least 5 GiB free');
  if (facts.LIVE_ROLLBACK_IMAGE_AVAILABLE !== 'YES') die('current production rollback image is unavailable');
  for (const role of ['API', 'WORKER', 'BOT']) {
    if (facts[`LIVE_${role}_SHA`] !== expected.runtimeSha || facts[`LIVE_${role}_RUNNING`] !== 'true') die(`live ${role.toLowerCase()} runtime does not match RELEASE_SHA`);
    if (facts[`LIVE_${role}_HEALTH`] !== 'healthy' && role === 'API') die('live API health is not healthy');
  }
  if (facts.LIVE_MINIAPP_SHA !== expected.miniappSha) die('live Mini App SHA does not match RELEASE_SHA');
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
  if (expected.mixed) {
    for (const role of ['API','WORKER']) {
      if (facts['LIVE_AGATA_' + role + '_SHA'] !== expected.agataSha)
        die('inherited AGATA runtime source SHA drift');
    }
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

async function requireV4StaticPreflight(candidate, bundle, current) {
  if (!bundle?.staticOnly || bundle.candidateSha !== candidate)
    die('V4 static release requires exact immutable typed receipt');
  const proof = remoteProof(candidate);
  if (proof.mainSha !== candidate) die('V4 candidate no longer matches remote main');
  if (current.currentProductionSha !== bundle.productionBaselineSha)
    die('V4 candidate production baseline differs from immutable receipt');
  const facts = readLiveFacts();
  const parentEvidence = await verifiedV5ParentEvidence(current, facts);
  const parent = expectedStaticParent(current, parentEvidence);
  verifyV4SourceAtCheckout({ root: ROOT, candidate, baseline: current.currentProductionSha,
                            impact: bundle.impactV4 });
  const expectedPrisma = candidatePrismaMigrationHead(candidate);
  const expectedAgata = candidateAgataMigrationHead(candidate);
  const expectedGsalesChecksum = crypto.createHash('sha256').update(
    fs.readFileSync(path.join(ROOT, 'services/gsales/migrations/001_gsales.sql'))).digest('hex');
  if (bundle.prismaHead !== expectedPrisma || bundle.agataHead !== expectedAgata ||
      bundle.gsalesMigrationHead !== '001_gsales' ||
      bundle.gsalesMigrationSha256 !== expectedGsalesChecksum ||
      current.currentProductionDbHead !== expectedPrisma)
    die('V4 static release migration source/baseline drift');
  if (facts.LIVE_DISK_FREE_KB == null || Number(facts.LIVE_DISK_FREE_KB) < 1048576 ||
      facts.LIVE_DOCKER_VERSION == null || facts.LIVE_AGATA_ENABLED !== 'true' ||
      facts.LIVE_GSALES_API_HEALTH !== 'healthy' ||
      facts.LIVE_GSALES_AGATA_API_HEALTH !== 'healthy' ||
      facts.LIVE_GSALES_AGATA_MEMORY_WORKER_HEALTH !== 'healthy')
    die('V4 static release cannot prove live baseline readiness');
  const providerFailures = validateAgataProviderCompatibility(providerConfigFromFacts(facts));
  if (providerFailures.length) die('V4 static provider incompatibility: ' + providerFailures.join('; '));
  const observed = v4NativeProofOnVPS(parent.images, runRemoteCommand);
  const componentManifest = verifyStaticLiveBaseline(current, bundle, facts, observed, parentEvidence);
  // Current Docker Compose files must support distinct component source SHA.
  // Otherwise a future restart after RELEASE_SHA advances would pull missing images.
  const ready = runRemoteCommand(
    "set -eu; test -d /opt/gpoint/app/miniapp; test ! -L /opt/gpoint/app/miniapp; " +
    "grep -Fq 'GPOINT_APP_IMAGE_SHA' /opt/gpoint/compose/docker-compose.yml; " +
    "grep -Fq 'GPOINT_AGATA_IMAGE_SHA' /opt/gpoint/compose/docker-compose.yml; " +
    "grep -Fq 'GPOINT_GSALES_IMAGE_SHA' /opt/gpoint/compose/docker-compose.yml; " +
    "test ! -e /opt/gpoint/app/static-v4-" + candidate + "; " +
    "python3 -c 'import pathlib,json; p=pathlib.Path(\"/opt/gpoint/app\"); " +
    "assert all(json.loads(f.read_text()).get(\"phase\") not in (\"ARMED\",\"SWITCHED\") for f in p.glob(\".static-v4-*.json\"))'; " +
    "echo V4_STATIC_PREFLIGHT=PASS");
  if (!ready.includes('V4_STATIC_PREFLIGHT=PASS')) die('V4 static filesystem readiness missing');
  // The canonical source baseline may have advanced through one or more
  // static-only releases. GSales config remains bound to the actual immutable
  // running runtime RELEASE_SHA, not the latest UI commit.
  inspectGsalesReadiness(candidate, parent.images.app.sourceSha);
  const workerCompatibility = verifyWorkerCompatibility(candidate, facts, 'preflight');
  return { parent, componentManifest, facts, workerCompatibility, remoteMainSha: proof.mainSha };
}

async function runStaticDryRun(candidate, bundle) {
  const startedAt = isoNow();
  const releaseId = `gpoint-dry-run-${startedAt.replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const current = state();
    const checked = await requireV4StaticPreflight(candidate, bundle, current);
    const manifest = {
      releaseId, candidateSha: candidate,
      previousProductionSha: current.currentProductionSha,
      productionShaAfter: null, status: 'PLANNED', dryRun: true,
      productionChanged: false, route: 'STATIC_UI_ONLY',
      remoteMainSha: checked.remoteMainSha,
      artifactIdentities: { miniapp: { sourceSha: candidate,
        staticArtifactSha256: bundle.staticArtifactSha256 } },
      componentImages: checked.parent.images,
      workerCompatibility: checked.workerCompatibility,
      dbHeadBefore: current.currentProductionDbHead,
      dbHeadAfter: null, agataDbHeadBefore: checked.facts.LIVE_AGATA_HEAD,
      agataDbHeadAfter: checked.facts.LIVE_AGATA_HEAD,
      gsalesDbHeadBefore: checked.facts.LIVE_GSALES_HEAD,
      startedAt, finishedAt: isoNow(),
      failureStage: null, failureReason: null,
      rollbackSha: current.currentProductionSha,
      ciReceipt: { ...bundle, archive: undefined },
    };
    writeManifest(manifest);
    console.log(JSON.stringify({ status: 'PLANNED', candidateSha: candidate,
      previousProductionSha: current.currentProductionSha,
      route: 'STATIC_UI_ONLY', dryRun: true, productionChanged: false,
      releaseId, rollbackSha: current.currentProductionSha }));
  } finally { unlock(); }
}

async function runDryRun(candidate) {
  const staticBundle = process.env.GPOINT_RELEASE_BUNDLE
    ? verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate) : null;
  if (staticBundle?.staticOnly) return runStaticDryRun(candidate, staticBundle);
  const startedAt = isoNow();
  const releaseId = `gpoint-dry-run-${startedAt.replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const localHead = git(['rev-parse', candidate]);
    if (localHead !== candidate) die('candidate does not exist locally');
    const proof = remoteProof(candidate);
    const current = state();
    const facts = readLiveFacts();
    const parentEvidence = await verifiedV5ParentEvidence(current, facts);
    const baseline = assertLiveBaseline(current, facts, {parentEvidence});
    const { liveSha, liveDbHead } = baseline;
    const gsalesReadiness = inspectGsalesReadiness(candidate, liveSha);
    const workerCompatibility = verifyWorkerCompatibility(candidate, baseline.facts, 'preflight');
    const agataMigrationReadiness = inspectAgataMigrationReadiness(candidate, baseline.agataDbHead, candidateAgataMigrationHead(candidate));
    const build = isolatedBuild(candidate, process.env.GPOINT_RELEASE_SKIP_BUILD !== '1');
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: build.miniappBuildSha ?? candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    Object.assign(artifacts, gsalesArtifacts(candidate));
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: baseline.provider, ...providerConfigFromFacts(baseline.facts, baseline.workerStatus), enabled: true };
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha,
      liveProductionSha: expectedV3LiveRevisions(current).mixed ? current.currentProductionSha : liveSha,
      productionRefTarget: candidate });
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

async function runStaticApply(candidate, bundle, currentAttempt = null) {
  if (bundle.staticArtifactRoute !== 'V4_FAST_STATIC' || !bundle.staticOnly ||
      !bundle.archive || !/^sha256:[a-f0-9]{64}$/.test(bundle.staticArtifactDigest ?? '') ||
      !/^[a-f0-9]{64}$/.test(bundle.staticArtifactSha256 ?? ''))
    die('V4 static Apply requires the exact GHCR-attested V4_FAST_STATIC artifact');
  const current = state();
  const releaseId = `gpoint-static-release-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  let switchStarted = false;
  let checked = null;
  const startedAt = isoNow();
  try {
    const initialFacts = readLiveFacts();
    const parentEvidence = await verifiedV5ParentEvidence(current, initialFacts, currentAttempt);
    const initial = assertLiveBaseline(current, initialFacts, {parentEvidence});
    if (initial.liveSha !== bundle.productionBaselineSha)
      die('V4 static baseline changed before Apply');
    checked = await requireV4StaticPreflight(candidate, bundle, current);
    recheckTaskAuthorizationBeforeMutation(candidate, 'V4_STATIC_STAGE');
    const outcome = applyStaticArtifactV4({
      candidate, baseline: current.currentProductionSha, bundle,
      images: checked.parent.images, sourceRoot: ROOT, upload,
      runRemote: runRemoteCommand,
      healthCheck: expectedSha => {
        const page = execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/'], {encoding: 'utf8'});
        if (!page.includes(`gpoint-build-sha" content="${expectedSha}`))
          throw new Error('external Mini App SHA does not match the static transaction');
        execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/api/health/live'], {stdio: 'ignore'});
      },
      nativeProof: images => {
        const live = v4NativeProofOnVPS(images, runRemoteCommand);
        if (Object.keys(live).length !== 3) throw new Error('V4 inherited native image proof is incomplete');
      },
      onSwitchStart: () => {
        recheckTaskAuthorizationBeforeMutation(candidate, 'V4_STATIC_SWITCH');
        switchStarted = true;
      },
    });
    const afterFacts = readLiveFacts();
    if (afterFacts.LIVE_RELEASE_ENV_SHA !== checked.parent.images.app.sourceSha ||
        afterFacts.LIVE_MINIAPP_SHA !== candidate ||
        afterFacts.LIVE_PRISMA_HEAD !== current.currentProductionDbHead ||
        afterFacts.LIVE_AGATA_HEAD !== checked.facts.LIVE_AGATA_HEAD ||
        afterFacts.LIVE_GSALES_HEAD !== checked.facts.LIVE_GSALES_HEAD ||
        afterFacts.LIVE_GSALES_MIGRATION_SHA256 !== checked.facts.LIVE_GSALES_MIGRATION_SHA256)
      throw new Error('V4 static post-Apply VPS readback differs from the attested UI and unchanged production baseline');
    const finishedAt = isoNow();
    const manifest = {
      releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha,
      productionShaAfter: current.currentProductionSha, miniappShaAfter: candidate,
      status: 'COMPLETE', route: 'STATIC_UI_ONLY', startedAt, finishedAt,
      productionChanged: true, runtimeSwitchOccurred: false, databaseMutationOccurred: false,
      containersRestarted: false, staticTransaction: outcome,
      staticArtifactDigest: bundle.staticArtifactDigest,
      staticArtifactSha256: bundle.staticArtifactSha256,
      prepareReceiptDigest: bundle.ghcrManifestDigest,
      componentImages: checked.parent.images,
      dbHeadBefore: current.currentProductionDbHead, dbHeadAfter: afterFacts.LIVE_PRISMA_HEAD,
      agataDbHeadBefore: checked.facts.LIVE_AGATA_HEAD, agataDbHeadAfter: afterFacts.LIVE_AGATA_HEAD,
      gsalesDbHeadBefore: checked.facts.LIVE_GSALES_HEAD, gsalesDbHeadAfter: afterFacts.LIVE_GSALES_HEAD,
      workerCompatibility: checked.workerCompatibility,
      rollbackSha: current.currentProductionSha, rollbackProven: false,
      authorizationMode: process.env.RELEASE_AUTHORIZATION_MODE || 'EXACT_SHA',
      authorizationId: process.env.RELEASE_AUTHORIZATION_ID || null,
      ciReceipt: {...bundle, archive: undefined},
    };
    writeManifest(manifest);
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({...current,
      miniappSourceSha: candidate, lastReleaseId: releaseId,
      lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: finishedAt,
      serverDeployStatus: 'COMPLETE', releaseOrigin: 'durable-production-release',
      recoveryRequired: false}, null, 2)}\n`);
    console.log(JSON.stringify({status: 'COMPLETE', releaseId, candidateSha: candidate,
      previousProductionSha: current.currentProductionSha, miniappSourceSha: candidate,
      route: 'STATIC_UI_ONLY', databaseMutationOccurred: false, containersRestarted: false}));
  } catch (error) {
    const finishedAt = isoNow();
    const failed = {releaseId, candidateSha: candidate,
      previousProductionSha: current.currentProductionSha,
      productionShaAfter: current.currentProductionSha, miniappShaAfter: null,
      status: switchStarted ? (error.rollbackProven ? 'ROLLED_BACK' : 'RECOVERY_REQUIRED') : 'FAILED',
      route: 'STATIC_UI_ONLY', startedAt, finishedAt,
      failureStage: switchStarted ? 'STATIC_SWITCH_OR_VERIFY' : 'STATIC_PREFLIGHT_OR_STAGE',
      failureReason: error?.name ?? 'Error', rollbackSha: current.currentProductionSha,
      rollbackProven: error.rollbackProven === true,
      staticArtifactDigest: bundle.staticArtifactDigest,
      staticArtifactSha256: bundle.staticArtifactSha256,
      prepareReceiptDigest: bundle.ghcrManifestDigest,
      databaseMutationOccurred: false, runtimeSwitchOccurred: false,
      ciReceipt: {...bundle, archive: undefined}};
    writeManifest(failed);
    if (switchStarted && error.rollbackProven !== true)
      fs.writeFileSync(STATE_FILE, `${JSON.stringify({...current, lastReleaseId: releaseId,
        lastReleaseStatus: 'RECOVERY_REQUIRED', recoveryRequired: true}, null, 2)}\n`);
    throw error;
  } finally { unlock(); }
}

function status() { console.log(JSON.stringify({ state: state(), activeLock: fs.existsSync(LOCK_FILE) ? readJson(LOCK_FILE) : null, manifests: fs.readdirSync(MANIFEST_DIR).filter(x => x.endsWith('.json')).sort() }, null, 2)); }

async function provisionAgataConfig(candidate) {
  const localHead = git(['rev-parse', candidate]);
  if (localHead !== candidate) die('candidate does not exist locally');
  if (git(['status', '--porcelain'])) die('AGATA config provisioning requires a clean committed checkout');
  const proof = remoteProof(candidate);
  if (proof.mainSha !== candidate) die('candidate must exactly match GitHub origin/main before AGATA config provisioning');
  const current = state();
  const facts = readLiveFacts();
  const parentEvidence = await verifiedV5ParentEvidence(current, facts);
  assertLiveBaseline(current, facts, { requireAgataConfig: false, parentEvidence });
  if (!SHA_RE.test(current.currentProductionSha ?? '')) die('recorded current production SHA is unavailable');
  const releaseId = `gpoint-agata-config-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    const output = provisionAgataInternalConfigForBaseline(current.currentProductionSha);
    console.log(JSON.stringify({ status: 'PROVISIONED', candidateSha: candidate, baselineSha: current.currentProductionSha, provider: output.match(/AGATA_LLM_PROVIDER=(disabled|mac_worker)/)?.[1] ?? 'unknown', apiKeyProvisioned: false, output }, null, 2));
  } finally { unlock(); }
}

async function provisionGsalesConfig(candidate, approvedSha) {
  if (candidate !== approvedSha) die('GSales provisioning requires --owner-approved-sha equal to exact candidate');
  const releaseId = `gpoint-gsales-config-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    if (git(['rev-parse', 'HEAD']) !== candidate || git(['status', '--porcelain'])) die('GSales provisioning requires exact clean candidate checkout');
    if (remoteProof(candidate).mainSha !== candidate) die('candidate must exactly match GitHub origin/main');
    const bundle = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate);
    const current = state();
    const facts = readLiveFacts();
    const parentEvidence = await verifiedV5ParentEvidence(current, facts);
    const baseline = assertLiveBaseline(current, facts, {parentEvidence});
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

async function configureGsalesWorker(candidate, approvedSha) {
  if (candidate !== approvedSha) die('Mac lane configuration requires exact-SHA OWNER approval');
  const releaseId = `gpoint-gsales-worker-${isoNow().replace(/[-:.]/g, '')}`;
  const unlock = acquireLock(releaseId);
  try {
    if (git(['rev-parse', 'HEAD']) !== candidate || git(['status', '--porcelain'])) die('Mac configuration requires a clean exact candidate');
    if (remoteProof(candidate).mainSha !== candidate) die('candidate must match GitHub current main');
    verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate);
    const current = state();
    const facts = readLiveFacts();
    const parentEvidence = await verifiedV5ParentEvidence(current, facts);
    const baseline = assertLiveBaseline(current, facts, {parentEvidence});
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

async function runApply(candidate) {
  const staticBundle = process.env.GPOINT_RELEASE_BUNDLE
    ? verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, candidate) : null;
  const currentAttempt = currentApplyDeploymentContext(candidate, staticBundle);
  if (staticBundle?.staticOnly) return runStaticApply(candidate, staticBundle, currentAttempt);
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
    const facts = readLiveFacts();
    const parentEvidence = await verifiedV5ParentEvidence(current, facts, currentAttempt);
    const baseline = assertLiveBaseline(current, facts, {parentEvidence});
    validatedProvider = baseline.provider;
    const { liveSha, liveDbHead } = baseline;
    const gsalesReadiness = inspectGsalesReadiness(candidate, liveSha);
    const workerCompatibility = verifyWorkerCompatibility(candidate, baseline.facts, 'apply');
    const agataMigrationReadiness = inspectAgataMigrationReadiness(candidate, baseline.agataDbHead, candidateAgataMigrationHead(candidate));
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: baseline.provider, ...providerConfigFromFacts(baseline.facts, baseline.workerStatus), enabled: true };
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    Object.assign(artifacts, gsalesArtifacts(candidate));
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha,
      liveProductionSha: expectedV3LiveRevisions(current).mixed ? current.currentProductionSha : liveSha,
      productionRefTarget: candidate });
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
    const ghcrReadToken = process.env.GHCR_READ_TOKEN || '';
    delete process.env.GHCR_READ_TOKEN;
    const adapter = applyCandidate(candidate, {
      retentionPlan,
      provider: baseline.provider,
      imageTransport: process.env.IMAGE_TRANSPORT || 'prepared',
      ghcrReadToken,
      onBeforeMutation: boundary => recheckTaskAuthorizationBeforeMutation(candidate, boundary),
      onMigrationStart: () => { migrationStarted = true; },
      onAgataStart: () => { agataStartStarted = true; },
      onSwitchStart: () => { switchStarted = true; },
    });
    if (adapter.postSwitch?.state !== 'VERIFIED') {
      const error = new Error(`post-switch reconciliation required: ${adapter.postSwitch?.state ?? 'UNKNOWN'}`);
      error.code = adapter.postSwitch?.state ?? 'SWITCHED_UNVERIFIED';
      throw error;
    }
    if (adapter.ciReceipt?.selectedTransport === 'ghcr-oci' &&
        (adapter.ciReceipt.ghcrVerified !== true || adapter.ciReceipt.ghcrPostSwitchVerified !== true)) {
      throw new Error('GHCR digest identity was not verified both before Apply and after runtime switch');
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
    Object.assign(stageTimings, adapter.timings ?? {}, { TOTAL_RELEASE: Number(((Date.now() - totalStarted) / 1000).toFixed(1)) });
    console.log(`RELEASE_STAGE_TIMING=${JSON.stringify(stageTimings)}`);
    const artifactIdentities = { api: { sourceSha: candidate, adapter: adapter.imageIdentity }, worker: { sourceSha: candidate, adapter: adapter.imageIdentity }, bot: { sourceSha: candidate, adapter: adapter.imageIdentity }, miniapp: { sourceSha: candidate, adapter: adapter.imageIdentity }, agataApi: { sourceSha: candidate, adapter: adapter.agataIdentity }, agataWorker: { sourceSha: candidate, adapter: adapter.agataIdentity } };
    for (const role of ['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker']) artifactIdentities[role] = { sourceSha: candidate, adapter: adapter.gsalesIdentity };
    // Preserve the exact GHCR manifest digests alongside Docker config IDs.
    // The resulting inventory is not itself a production attestation; later
    // selective paths must re-read the successful GitHub Deployment and VPS.
    let componentImages = null;
    try {
      componentImages = buildV5ComponentImageInventory(adapter.ciReceipt, {
        app: adapter.imageIdentity, agata: adapter.agataIdentity, gsales: adapter.gsalesIdentity,
      }, candidate);
    } catch {
      console.error('V5_SELECTIVE_PARENT_IMAGE_INVENTORY=UNAVAILABLE (selective V3/V4 remains fail-closed)');
    }
    const readiness = observeDeploymentReadiness('COMPLETE', readWorkerDependencies, { gsalesInferenceReady: adapter.gsalesIdentity.inferenceReady });
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: candidate, dbHeadBefore: liveDbHead, dbHeadAfter: deployedDbHead, agataDbHeadBefore: baseline.agataDbHead, agataDbHeadAfter: adapter.agataIdentity.head, agataRuntimeSha: candidate, agataProvider: adapter.agataIdentity.provider, artifactIdentities, startedAt, finishedAt: isoNow(), status: 'COMPLETE', failureStage: null, failureReason: null, rollbackSha: current.currentProductionSha, rollbackDbCompatibility: 'REQUIRES_FORWARD_FIX_ASSESSMENT', productionRef: null, remoteProof: { mainSha: proof.mainSha }, migrationOutput: adapter.migrationOutput, migrationProof: adapter.migrationProof, detectorSourceHash: adapter.detectorHash, stageTimings, releaseOrigin: 'github-deployment', agataMigrationReadiness };
    Object.assign(manifest, readiness);
    manifest.workerCompatibility = workerCompatibility;
    manifest.ciReceipt = adapter.ciReceipt ? {
      ...adapter.ciReceipt,
      ghcrManifestDigest: process.env.GPOINT_PREPARE_RECEIPT_DIGEST,
      prepareReceiptSha256: process.env.GPOINT_PREPARE_RECEIPT_SHA256,
      prepareReceiptJson: readVerifiedPrepareReceiptJson(candidate),
    } : null;
    // Additive V4 component identity, not an authorization to reuse images.
    // Any future inherited image must still pass source/CI/native VPS proofs.
    if (componentImages) manifest.componentImages = componentImages;
    manifest.miniappSourceSha = candidate;
    Object.assign(manifest, { workerCompatibility, gsalesReadiness, gsalesDbHeadBefore: baseline.facts.LIVE_GSALES_HEAD,
      gsalesDbHeadAfter: adapter.gsalesIdentity.migrationHead, gsalesRuntime: adapter.gsalesIdentity,
      effectiveConfigSha256: adapter.effectiveConfigSha256,
      authorizationMode: process.env.RELEASE_AUTHORIZATION_MODE || 'EXACT_SHA',
      authorizationId: process.env.RELEASE_AUTHORIZATION_ID || null,
      authorizationSourceInstructionId: process.env.RELEASE_AUTHORIZATION_SOURCE_INSTRUCTION_ID || null,
      authorizationScopeSha256: process.env.RELEASE_AUTHORIZATION_SCOPE_SHA256 || null,
      authorizationRecordSha256: process.env.RELEASE_AUTHORIZATION_RECORD_SHA256 || null });
    const file = writeManifest(manifest);
    const verifiedState = verifiedRuntimeState(current, { candidate, dbHead: deployedDbHead });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...verifiedState, currentProductionRef: null, previousProductionSha: current.currentProductionSha, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: manifest.finishedAt, ...readiness, gsalesDbHead: adapter.gsalesIdentity.migrationHead, gsalesMigrationSha256: adapter.gsalesIdentity.migrationSha256, releaseOrigin: 'github-deployment', agataRuntimeSha: candidate, agataAlembicHead: adapter.agataIdentity.head, agataProvider: adapter.agataIdentity.provider, artifactIdentities: manifest.artifactIdentities, ...(componentImages ? { componentImages } : {}), miniappSourceSha: candidate }, null, 2)}\n`);
    console.log(JSON.stringify({ status: 'COMPLETE', ...readiness, releaseId, candidateSha: candidate, productionRef: null, manifest: path.relative(ROOT, file) }, null, 2));
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
      status: releaseState, serverDeployStatus: releaseState === 'INSTALLATION_UNCONFIRMED' ? 'INSTALLATION_UNCONFIRMED' : 'FAILED', workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED',
      agataProvider: validatedProvider,
      ...failureEvidence(error, { stage: error.releaseStage, fallbackStage: failure.failureStage }),
      rollbackSha: current.currentProductionSha,
      productionRef: null,
      releaseOrigin: 'github-deployment'
    };
    const manifestFile = writeManifest(failureManifest);
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...current, ...(validatedProvider ? { agataProvider: validatedProvider } : {}), lastReleaseId: releaseId, lastReleaseStatus: releaseState, serverDeployStatus: failureManifest.serverDeployStatus, workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED', dependencyCheckedAt: null, lastReleaseFailureManifest: manifestReference(manifestFile), lastReleaseFailureAt: failureManifest.finishedAt }, null, 2)}\n`);
    console.error(JSON.stringify({ status: releaseState, serverDeployStatus: failureManifest.serverDeployStatus, workerOperationalStatus: 'NOT_VERIFIED', overallServiceReadiness: 'DEGRADED', releaseId, candidateSha: candidate, failureStage: failureManifest.failureStage, failureReason: error.message }));
    throw error;
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

async function main() {
  if (Boolean(process.env.GPOINT_RELEASE_STATE_FILE) !== Boolean(process.env.GPOINT_RELEASE_MANIFEST_DIR)) die('release evidence paths must be supplied together');
  if (!process.env.GPOINT_RELEASE_STATE_FILE && !process.env.GPOINT_RELEASE_MANIFEST_DIR) {
    const runDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-local-deploy-'));
    const state = initializeRunScopedState({directory: runDirectory});
    STATE_FILE = state.stateFile;
    MANIFEST_DIR = state.manifestDir;
    process.once('exit', () => fs.rmSync(runDirectory, {recursive: true, force: true}));
  }
  const args = process.argv.slice(2);
  if (args[0] === 'status' || args.includes('--status')) return status();
  if (!args.includes('--recheck-worker')) runLocalDiskPreflight();
  if (args.includes('--provision-agata-config')) return await provisionAgataConfig(candidateSha(args));
  if (args.includes('--provision-gsales-config')) return await provisionGsalesConfig(candidateSha(args), args[args.indexOf('--owner-approved-sha') + 1]);
  if (args.includes('--configure-gsales-worker')) return await configureGsalesWorker(candidateSha(args), args[args.indexOf('--owner-approved-sha') + 1]);
  if (args.includes('--recheck-worker')) return recheckWorkerDependency(candidateSha(args));
  const isDryRun = args.includes('--dry-run');
  const isApply = args.includes('--apply');
  if (!isDryRun && !isApply) die('choose --dry-run or --apply');
  if (isApply && !args.includes('--confirm-production-apply')) die('apply requires --confirm-production-apply');
  if (isApply) return await runApply(candidateSha(args));
  await runDryRun(candidateSha(args));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
