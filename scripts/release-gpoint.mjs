#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { applyCandidate, inspectProductionReadOnly, provisionAgataInternalConfigForBaseline } from './release-gpoint-vps-adapter.mjs';

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
    const adapter = applyCandidate(candidate, {
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
      failureStage: failure.failureStage,
      failureReason: error.message,
      failureCommand: error.command ?? null,
      failureExitCode: error.exitCode ?? null,
      failureStderr: error.stderr ? String(error.stderr).slice(-4000) : null,
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

function main() {
  const args = process.argv.slice(2);
  if (args[0] === 'status' || args.includes('--status')) return status();
  if (args.includes('--provision-agata-config')) return provisionAgataConfig(candidateSha(args));
  const isDryRun = args.includes('--dry-run');
  const isApply = args.includes('--apply');
  if (!isDryRun && !isApply) die('choose --dry-run or --apply');
  if (isApply && !args.includes('--confirm-production-apply')) die('apply requires --confirm-production-apply');
  if (isApply) return runApply(candidateSha(args));
  runDryRun(candidateSha(args));
}

if (import.meta.url === `file://${process.argv[1]}`) { try { main(); } catch (error) { console.error(error.message); process.exitCode = 1; } }
