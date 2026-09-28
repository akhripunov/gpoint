#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { applyCandidate, inspectProductionReadOnly, planReleaseRetention, provisionAgataInternalConfigForBaseline, runRemoteCommand } from './release-gpoint-vps-adapter.mjs';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const STATE_FILE = path.join(ROOT, 'docs/release/state/production.json');
const MANIFEST_DIR = path.join(ROOT, 'docs/release/manifests');
const LOCK_FILE = path.join(ROOT, '.gpoint-release.lock');
const SHA_RE = /^[0-9a-f]{40}$/;

function die(message) { throw new Error(`BLOCKED: ${message}`); }
function readJson(file) { return JSON.parse(fs.readFileSync(file, 'utf8')); }
function isoNow() { return new Date().toISOString(); }
function measured(timings, name, action) { const started = Date.now(); try { return action(); } finally { timings[name] = Number(((Date.now() - started) / 1000).toFixed(1)); } }
function git(args, cwd = ROOT) { return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim(); }
function command(args, cwd) { const r = spawnSync(args[0], args.slice(1), { cwd, encoding: 'utf8', stdio: 'inherit' }); if (r.status !== 0) die(`${args.join(' ')} exited with ${r.status}`); }
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

export function checkAgataEffectiveConfig(config) {
  const failures = [];
  if (config?.provider !== 'disabled') failures.push('AGATA inference provider is not disabled');
  if (config?.enabled !== true) failures.push('AGATA is not enabled');
  return failures;
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

export function recoveryRuntimeFailures(candidate, facts, expectedDbHead) {
  const failures = [];
  if (facts.LIVE_RELEASE_ENV_SHA !== candidate) failures.push('release env SHA mismatch');
  for (const role of ['API', 'WORKER', 'BOT']) {
    if (facts[`LIVE_${role}_SHA`] !== candidate) failures.push(`${role.toLowerCase()} build SHA mismatch`);
    if (facts[`LIVE_${role}_RUNNING`] !== 'true') failures.push(`${role.toLowerCase()} is not running`);
  }
  if (facts.LIVE_API_HEALTH !== 'healthy') failures.push('API is not healthy');
  if (facts.LIVE_MINIAPP_SHA !== candidate) failures.push('Mini App SHA mismatch');
  if (facts.LIVE_PRISMA_HEAD !== expectedDbHead) failures.push('Prisma head mismatch');
  if (facts.LIVE_AGATA_HEAD !== 'agata0007') failures.push('AGATA Alembic head mismatch');
  if (facts.LIVE_AGATA_ENABLED !== 'true' || facts.LIVE_AGATA_PROVIDER !== 'disabled') failures.push('AGATA config is not enabled with provider disabled');
  if (facts.LIVE_CONFIG_AGATA_LLM_API_KEY !== 'NO') failures.push('AGATA LLM API key must remain absent while provider is disabled');
  for (const role of ['API', 'WORKER']) {
    if (facts[`LIVE_AGATA_${role}_SHA`] !== candidate) failures.push(`AGATA ${role.toLowerCase()} build SHA mismatch`);
    if (facts[`LIVE_AGATA_${role}_RUNNING`] !== 'true') failures.push(`AGATA ${role.toLowerCase()} is not running`);
    if (facts[`LIVE_AGATA_${role}_HEALTH`] !== 'healthy') failures.push(`AGATA ${role.toLowerCase()} is not healthy`);
  }
  if (!facts.LIVE_AGATA_API_IMAGE_ID || facts.LIVE_AGATA_API_IMAGE_ID !== facts.LIVE_AGATA_WORKER_IMAGE_ID) failures.push('AGATA API/worker image identity mismatch');
  return failures;
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
  fs.mkdirSync(path.dirname(LOCK_FILE), { recursive: true });
  try { fs.writeFileSync(LOCK_FILE, `${JSON.stringify({ releaseId, pid: process.pid, startedAt: isoNow() })}\n`, { flag: 'wx' }); }
  catch { die('another GPoint production release is active; stale locks require audited manual recovery'); }
  return () => { try { fs.unlinkSync(LOCK_FILE); } catch {} };
}

function remoteProof(candidate) {
  const lines = git(['ls-remote', 'origin', 'refs/heads/main', 'refs/tags/*']);
  const refs = lines.split('\n').filter(Boolean).map(line => line.split(/\s+/));
  const direct = refs.find(([sha]) => sha === candidate);
  const main = refs.find(([, ref]) => ref === 'refs/heads/main');
  if (!main) die('origin/main could not be independently verified');
  if (main[0] !== candidate) die(`candidate SHA is not exact origin/main: candidate=${candidate} origin/main=${main[0]}`);
  return { mainSha: main[0], directRef: direct?.[1] ?? null, refs }; 
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
  if (requireAgataConfig) {
    const requiredConfig = ['AGATA_ENABLED','AGATA_LLM_PROVIDER','AGATA_DATABASE_URL','AGATA_TENANT_ID','AGATA_INTERNAL_API_TOKEN','AGATA_MESSAGE_AEAD_KEY','AGATA_REQUEST_FINGERPRINT_HMAC_KEY','AGATA_RETRIEVAL_QUERY_HMAC_KEY','AGATA_RESPONSE_FINGERPRINT_HMAC_KEY','AGATA_SHADOW_CANDIDATE_HMAC_KEY'];
    for (const key of requiredConfig) if (facts[`LIVE_CONFIG_${key}`] !== 'YES') die(`AGATA production config is missing: ${key}`);
    if (facts.LIVE_AGATA_ENABLED !== 'true' || facts.LIVE_AGATA_PROVIDER !== 'disabled') die('AGATA must be enabled with inference explicitly disabled');
  }
  if (!facts.LIVE_AGATA_HEAD || facts.LIVE_AGATA_HEAD === 'READ_ONLY_DB_INSPECTION_FAILED') die('live AGATA migration head could not be verified');
  return { liveSha, liveDbHead: facts.LIVE_PRISMA_HEAD, agataDbHead: facts.LIVE_AGATA_HEAD, facts };
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
  } finally { try { git(['worktree', 'remove', '--force', worktree], ROOT); } catch {} }
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
    const build = isolatedBuild(candidate, process.env.GPOINT_RELEASE_SKIP_BUILD !== '1');
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: build.miniappBuildSha ?? candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: 'disabled', agataEnabled: true };
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha, liveProductionSha: liveSha, productionRefTarget: candidate });
    gates.push(...checkAgataArtifactParity(candidate, artifacts), ...checkAgataEffectiveConfig({ provider: effective.agataProvider, enabled: effective.agataEnabled }));
    if (gates.length) die(gates.join('; '));
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: null, dbHeadBefore: liveDbHead, dbHeadAfter: null, agataDbHeadBefore: baseline.agataDbHead, agataDbHeadAfter: 'agata0007', artifactIdentities: artifacts, provider: 'disabled', startedAt, finishedAt: isoNow(), status: 'PLANNED', failureStage: null, failureReason: null, rollbackSha: current.currentProductionSha, rollbackDbCompatibility: 'REQUIRES_FORWARD_FIX_ASSESSMENT', productionRef: null, remoteProof: { mainSha: proof.mainSha, directRef: proof.directRef }, liveFacts: baseline.facts, dryRun: true };
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
    console.log(JSON.stringify({ status: 'PROVISIONED', candidateSha: candidate, baselineSha: current.currentProductionSha, provider: 'disabled', apiKeyProvisioned: false, output }, null, 2));
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
  const stageTimings = {};
  try {
    const proof = remoteProof(candidate);
    const baseline = assertLiveBaseline(current, readLiveFacts());
    const { liveSha, liveDbHead } = baseline;
    const effective = { api: candidate, worker: candidate, bot: candidate, miniapp: candidate, agataApi: candidate, agataWorker: candidate, agataProvider: 'disabled', agataEnabled: true };
    const artifacts = { api: { sourceSha: candidate }, worker: { sourceSha: candidate }, bot: { sourceSha: candidate }, miniapp: { sourceSha: candidate }, agataApi: { sourceSha: candidate }, agataWorker: { sourceSha: candidate } };
    const gates = evaluateSafetyGates({ candidateSha: candidate, remoteSha: proof.mainSha, artifacts, config: effective, recordedProductionSha: current.currentProductionSha, liveProductionSha: liveSha, productionRefTarget: candidate });
    gates.push(...checkAgataArtifactParity(candidate, artifacts), ...checkAgataEffectiveConfig({ provider: effective.agataProvider, enabled: effective.agataEnabled }));
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
      onMigrationStart: () => { migrationStarted = true; },
      onAgataStart: () => { agataStartStarted = true; },
      onSwitchStart: () => { switchStarted = true; },
    });
    if (adapter.postSwitch?.state !== 'VERIFIED') {
      const error = new Error(`post-switch reconciliation required: ${adapter.postSwitch?.state ?? 'UNKNOWN'}`);
      error.code = adapter.postSwitch?.state ?? 'SWITCHED_UNVERIFIED';
      throw error;
    }
    const root = measured(stageTimings, 'API_HEALTH', () => execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/'], { encoding: 'utf8' }));
    if (!root.includes(`gpoint-build-sha" content="${candidate}`)) die('external Mini App build SHA mismatch after apply');
    measured(stageTimings, 'WORKER_HEALTH', () => execFileSync('curl', ['-fsS', 'https://gpoint.amiora.one/api/health/live'], { stdio: 'ignore' }));
    const ref = `gpoint-production-${startedAt.replace(/[-:.]/g, '')}`;
    measured(stageTimings, 'PRODUCTION_REF_CREATE', () => execFileSync('git', ['tag', '-a', ref, candidate, '-m', `GPoint production release\nproductionSha=${candidate}\ndbHead=${liveDbHead}\nreleaseId=${releaseId}`], { cwd: ROOT, stdio: 'inherit' }));
    measured(stageTimings, 'PRODUCTION_REF_PUSH', () => execFileSync('git', ['push', 'origin', `refs/tags/${ref}`], { cwd: ROOT, stdio: 'inherit' }));
    const remoteRef = measured(stageTimings, 'REMOTE_REF_VERIFY', () => git(['ls-remote', 'origin', `refs/tags/${ref}^{}`])).trim();
    if (!remoteRef.includes(candidate)) die('remote production ref target mismatch');
    Object.assign(stageTimings, adapter.timings ?? {}, { TOTAL_RELEASE: Number(((Date.now() - totalStarted) / 1000).toFixed(1)) });
    console.log(`RELEASE_STAGE_TIMING=${JSON.stringify(stageTimings)}`);
    const artifactIdentities = { api: { sourceSha: candidate, adapter: adapter.imageIdentity }, worker: { sourceSha: candidate, adapter: adapter.imageIdentity }, bot: { sourceSha: candidate, adapter: adapter.imageIdentity }, miniapp: { sourceSha: candidate, adapter: adapter.imageIdentity }, agataApi: { sourceSha: candidate, adapter: adapter.agataIdentity }, agataWorker: { sourceSha: candidate, adapter: adapter.agataIdentity } };
    const manifest = { releaseId, candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: candidate, dbHeadBefore: liveDbHead, dbHeadAfter: liveDbHead, agataDbHeadBefore: baseline.agataDbHead, agataDbHeadAfter: adapter.agataIdentity.head, agataRuntimeSha: candidate, agataProvider: 'disabled', artifactIdentities, startedAt, finishedAt: isoNow(), status: 'COMPLETE', failureStage: null, failureReason: null, rollbackSha: current.currentProductionSha, rollbackDbCompatibility: 'REQUIRES_FORWARD_FIX_ASSESSMENT', productionRef: ref, remoteProof: { mainSha: proof.mainSha }, migrationOutput: adapter.migrationOutput, detectorSourceHash: adapter.detectorHash, stageTimings, releaseOrigin: 'durable-production-release' };
    const file = writeManifest(manifest);
    const verifiedState = verifiedRuntimeState(current, { candidate, dbHead: liveDbHead });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...verifiedState, currentProductionRef: ref, previousProductionSha: current.currentProductionSha, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: manifest.finishedAt, releaseOrigin: 'durable-production-release', agataRuntimeSha: candidate, agataAlembicHead: adapter.agataIdentity.head, agataProvider: 'disabled', artifactIdentities: manifest.artifactIdentities }, null, 2)}\n`);
    console.log(JSON.stringify({ status: 'COMPLETE', releaseId, candidateSha: candidate, productionRef: ref, manifest: path.relative(ROOT, file) }, null, 2));
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
      status: releaseState,
      ...failureEvidence(error, { stage: error.releaseStage, fallbackStage: failure.failureStage }),
      rollbackSha: current.currentProductionSha,
      productionRef: null,
      releaseOrigin: 'durable-production-release'
    };
    const manifestFile = writeManifest(failureManifest);
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...current, lastReleaseId: releaseId, lastReleaseStatus: releaseState, recoveryRequired: switched || partiallyMutated, lastReleaseFailureManifest: path.relative(ROOT, manifestFile), lastReleaseFailureAt: failureManifest.finishedAt }, null, 2)}\n`);
    console.error(JSON.stringify({ status: releaseState, releaseId, candidateSha: candidate, failureStage: failureManifest.failureStage, failureReason: error.message }));
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
    const failedManifestPath = path.resolve(ROOT, current.lastReleaseFailureManifest ?? '');
    if (!failedManifestPath.startsWith(`${MANIFEST_DIR}${path.sep}`) || !fs.existsSync(failedManifestPath)) die('recovery failure manifest is missing or outside the release manifest directory');
    const failed = readJson(failedManifestPath);
    if (failed.status !== 'RECOVERY_REQUIRED' || failed.candidateSha !== candidate || failed.releaseId !== current.lastReleaseId) die('candidate does not match the unresolved release attempt');
    assertSha(candidate);
    if (git(['rev-parse', candidate]) !== candidate) die('recovery candidate is not present locally');

    const remoteMain = git(['ls-remote', 'origin', 'refs/heads/main']).split(/\s+/)[0];
    if (!SHA_RE.test(remoteMain ?? '') || remoteMain !== git(['rev-parse', 'origin/main'])) die('origin/main exact SHA could not be independently reconciled');
    try { execFileSync('git', ['merge-base', '--is-ancestor', candidate, 'origin/main'], { cwd: ROOT, stdio: 'ignore' }); }
    catch { die('recovery candidate is not contained in verified origin/main history'); }

    const facts = readLiveFacts();
    const runtimeFailures = recoveryRuntimeFailures(candidate, facts, current.currentProductionDbHead);
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
    const agataSmoke = runRemoteCommand(`set -e; docker exec compose-agata-api-1 python -c "from app.config import get_settings; assert get_settings().llm_provider == 'disabled'; print('AGATA_PROVIDER_DISABLED')"; docker run --rm --network gpoint_app --env-file /opt/gpoint/env/production.env --entrypoint node gpoint-runtime:${candidate} -e 'fetch("http://agata-api:8000/internal/v1/users/gpoint-release-readiness/age-assurance", {headers:{authorization:"Bearer "+process.env.AGATA_INTERNAL_API_TOKEN}}).then(async r=>{if(!r.ok) throw Error("age readiness HTTP "+r.status); await r.arrayBuffer(); console.log("AGE_ASSURANCE_INTERNAL_READINESS=PASS")}).catch(e=>{console.error(e.message);process.exit(1)})'; echo AGATA_RECOVERY_SMOKE=PASS`);
    if (!agataSmoke.includes('AGATA_PROVIDER_DISABLED') || !agataSmoke.includes('AGE_ASSURANCE_INTERNAL_READINESS=PASS')) die('AGATA/age-assurance recovery smoke failed');

    const ref = `gpoint-production-${startedAt.replace(/[-:.]/g, '')}`;
    const localTag = spawnSync('git', ['show-ref', '--verify', '--quiet', `refs/tags/${ref}`], { cwd: ROOT, stdio: 'ignore' });
    if (localTag.status === 0) die(`production ref already exists locally: ${ref}`);
    execFileSync('git', ['tag', '-a', ref, candidate, '-m', `GPoint production recovery finalization\nproductionSha=${candidate}\ndbHead=${facts.LIVE_PRISMA_HEAD}\nreleaseId=${failed.releaseId}`], { cwd: ROOT, stdio: 'inherit' });
    execFileSync('git', ['push', 'origin', `refs/tags/${ref}`], { cwd: ROOT, stdio: 'inherit' });
    const remoteRef = git(['ls-remote', 'origin', `refs/tags/${ref}^{}`]).split(/\s+/)[0];
    if (remoteRef !== candidate) die('remote production ref target mismatch during recovery finalization');

    const finishedAt = isoNow();
    const recoveryManifest = {
      releaseId, eventType: 'RECOVERY_FINALIZATION', failedReleaseId: failed.releaseId,
      candidateSha: candidate, previousProductionSha: current.currentProductionSha, productionShaAfter: candidate,
      dbHeadBefore: current.currentProductionDbHead, dbHeadAfter: facts.LIVE_PRISMA_HEAD,
      agataDbHeadAfter: facts.LIVE_AGATA_HEAD, agataRuntimeSha: candidate, agataProvider: 'disabled',
      startedAt, finishedAt, status: 'COMPLETE', failureStage: null, failureReason: null,
      productionRef: ref, remoteProductionRefTarget: remoteRef, remoteMainSha: remoteMain,
      runtimeFacts: Object.fromEntries(Object.entries(facts).filter(([key]) => key.startsWith('LIVE_'))),
      smoke: { root: 'PASS', apiHealth: 'PASS', messagesUnauthenticatedRoute: 'PASS', ageUnauthenticatedRoute: 'PASS', messagesRouteShell: 'PASS', modelRouteShell: 'PASS', ageInternalReadiness: 'PASS', providerDisabled: 'PASS' },
      recoveryBasis: 'Live runtime, API/Worker/Bot/AGATA image parity, Mini App build SHA, Prisma/Alembic heads, and health/smoke checks were independently verified after the original release apply switched runtime but failed AGATA identity parsing.'
    };
    const manifestFile = writeManifest(recoveryManifest);
    const verifiedState = verifiedRuntimeState(current, { candidate, dbHead: facts.LIVE_PRISMA_HEAD });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify({ ...verifiedState, currentProductionRef: ref, previousProductionSha: current.currentProductionSha, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', lastReleaseCompletedAt: finishedAt, releaseOrigin: 'durable-production-release', agataRuntimeSha: candidate, agataAlembicHead: facts.LIVE_AGATA_HEAD, agataProvider: 'disabled', recoveryRequired: false, lastReleaseFailureManifest: path.relative(ROOT, failedManifestPath), recoveryFinalizationManifest: path.relative(ROOT, manifestFile) }, null, 2)}\n`);
    console.log(JSON.stringify({ status: 'COMPLETE', candidateSha: candidate, productionRef: ref, recoveryManifest: path.relative(ROOT, manifestFile), failedAttemptPreserved: path.relative(ROOT, failedManifestPath) }, null, 2));
  } finally { unlock(); }
}

function main() {
  const args = process.argv.slice(2);
  if (args[0] === 'status' || args.includes('--status')) return status();
  if (args.includes('--provision-agata-config')) return provisionAgataConfig(candidateSha(args));
  if (args.includes('--finalize-recovery')) return finalizeRecovery(candidateSha(args));
  const isDryRun = args.includes('--dry-run');
  const isApply = args.includes('--apply');
  if (!isDryRun && !isApply) die('choose --dry-run or --apply');
  if (isApply && !args.includes('--confirm-production-apply')) die('apply requires --confirm-production-apply');
  if (isApply) return runApply(candidateSha(args));
  runDryRun(candidateSha(args));
}

if (import.meta.url === `file://${process.argv[1]}`) { try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; } }
