#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { createRetryingGit } from './release-network-retry.mjs';
import { verifyReleaseBundle } from './release-bundle.mjs';
import { persistReleaseBookkeeping, loadReleaseBookkeeping } from './release-bookkeeping.mjs';
import { fileURLToPath } from 'node:url';
import { inspectProductionReadOnly } from './release-gpoint-vps-adapter.mjs';
import { candidateAgataMigrationHead, candidatePrismaMigrationHead } from './release-gpoint.mjs';
import { assertInstalledWorkerMatches } from './worker-runtime-guard.mjs';
import { withPreflightEvidence } from './release-evidence.mjs';
import { AuthorizationError, authorizationRecordPath, candidateScopeDiff,
  checkTaskAuthorizationState, consumeTaskAuthorization, validateTaskAuthorization,
  assertNoTaskScopedMigrations } from './release-authorization.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const sha = process.env.CANDIDATE_SHA;
const mode = process.env.RELEASE_MODE;
if (!['preflight', 'production', 'task-scoped-auto'].includes(mode)) throw new Error('Explicit release mode is required');
if (mode === 'production' && process.env.APPROVE_PRODUCTION !== 'true') throw new Error('Explicit production approval is required');
if (mode === 'task-scoped-auto' && (process.env.APPROVE_PRODUCTION !== 'false' || !process.env.TASK_AUTHORIZATION_ID)) throw new Error('TASK_SCOPED_AUTO requires its durable authorization ID and no exact-SHA approval input');
if (!['preflight', 'task-scoped-auto'].includes(mode) && process.env.TASK_AUTHORIZATION_ID) throw new Error('Task authorization ID is only valid in linked preflight or TASK_SCOPED_AUTO mode');
const proof = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, sha);
if (String(proof.runId) !== process.env.PREPARE_RUN_ID) throw new Error('Receipt does not belong to the approved GitHub run');
const runGit = (args, input, timeout) => execFileSync('git', args, { cwd: root, encoding: 'utf8', input, timeout }).trim();
const git = createRetryingGit(runGit);
if (git(['rev-parse', 'HEAD']) !== sha || git(['status', '--porcelain'])) throw new Error('Release checkout must be clean and exact');
const checksum = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
async function github(route) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new AuthorizationError('GitHub token is required for TASK_SCOPED_AUTO revalidation');
  const response = await fetch(`https://api.github.com/repos/akhripunov/gpoint/${route}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
  });
  if (!response.ok) throw new AuthorizationError(`GitHub task authorization check failed (HTTP ${response.status})`);
  return response.json();
}
let taskAuthorization = null;
let authorizationConsumed = false;
const workerRoot = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/runtime/current');
const workerAssets = ['gpoint_mac_worker.py', 'agata_worker_pool.py', 'lab_queue.py', 'lab_fixture_manifest.json', 'requirements.txt', 'run_codex_worker.sh'];
assertInstalledWorkerMatches(mode, workerAssets.map(asset => {
  const source = path.join(root, 'services/agata/worker', asset);
  const installed = path.join(workerRoot, asset);
  const sourceExists = fs.existsSync(source);
  const installedExists = fs.existsSync(installed);
  return { sourceExists, installedExists,
    matches: sourceExists && installedExists && checksum(source) === checksum(installed) };
}));
if (!process.env.RELEASE_RESULT || !process.env.GITHUB_RUN_ID) throw new Error('Release evidence paths are required');
const ledger = loadReleaseBookkeeping({ root, git, destination: process.env.GPOINT_RELEASE_EVIDENCE });
process.env.GPOINT_RELEASE_STATE_FILE = ledger.stateFile;
process.env.GPOINT_RELEASE_MANIFEST_DIR = ledger.manifestDir;
const previous = new Set(fs.readdirSync(ledger.manifestDir));
let releaseCode = 1;
let preflightStatus = 'BLOCKED';
try {
  // First GSales release includes an explicitly approved protected setup.
  // Read-only preflight never provisions identities or changes the Mac worker.
  if (mode === 'production') {
    const facts = inspectProductionReadOnly();
    if (/^LIVE_GSALES_HEAD=ABSENT$/m.test(facts)) {
      const setup = spawnSync('pnpm', ['release:gpoint', '--provision-gsales-config', '--candidate', sha, '--owner-approved-sha', sha],
        { cwd: root, stdio: 'inherit', timeout: 300_000 });
      if (setup.status !== 0) throw new Error('Approved GSales provisioning blocked the release');
      const worker = spawnSync('pnpm', ['release:gpoint', '--configure-gsales-worker', '--candidate', sha, '--owner-approved-sha', sha],
      { cwd: root, stdio: 'inherit', timeout: 150_000 });
      if (worker.status !== 0) throw new Error('Approved Mac lane configuration blocked the release');
    }
  }
  // Docker artifacts already passed CI. Canonical dry-run still independently
  // checks the live baseline, config, GitHub SHA and isolated source tree.
  const preflight = spawnSync('pnpm', ['release:gpoint', '--dry-run', '--candidate', sha], {
    cwd: root, env: { ...process.env, GPOINT_RELEASE_SKIP_BUILD: '1' }, stdio: 'inherit', timeout: 2_100_000,
  });
  preflightStatus = preflight.status === 0 ? 'PLANNED' : 'FAILED';
  if (preflight.status !== 0) throw new Error('Canonical preflight blocked the release');
  const linkedAuthorizationId = process.env.TASK_AUTHORIZATION_ID;
  if (mode === 'task-scoped-auto' || (mode === 'preflight' && linkedAuthorizationId)) {
    const authorizationId = linkedAuthorizationId;
    const currentMainSha = (await github('git/ref/heads/main')).object?.sha;
    const planned = fs.readdirSync(ledger.manifestDir).filter(name => !previous.has(name))
      .map(name => JSON.parse(fs.readFileSync(path.join(ledger.manifestDir, name), 'utf8')))
      .find(item => item.candidateSha === sha && item.status === 'PLANNED');
    const productionBaselineSha = planned?.previousProductionSha;
    if (!/^[a-f0-9]{40}$/.test(productionBaselineSha ?? '')) throw new AuthorizationError('Canonical preflight did not provide the exact production baseline');
    const gsalesMigration = path.join(root, 'services/gsales/migrations/001_gsales.sql');
    const record = JSON.parse(fs.readFileSync(path.join(root, authorizationRecordPath(authorizationId)), 'utf8'));
    const changedPaths = candidateScopeDiff(root, authorizationId, record.production_baseline_sha, sha);
    taskAuthorization = validateTaskAuthorization({record, project: 'gpoint', authorizationId,
      candidateSha: sha, currentMainSha, productionSha: productionBaselineSha, changedPaths});
    assertNoTaskScopedMigrations({manifest: planned, prismaHead: candidatePrismaMigrationHead(sha),
      agataHead: candidateAgataMigrationHead(sha), gsalesMigrationSha256: checksum(gsalesMigration),
      record, changedPaths});
    const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId,
      ...(mode === 'task-scoped-auto' ? {candidateSha: sha} : {}), token: process.env.GITHUB_TOKEN});
    if (mode === 'preflight' && state.claimed) throw new AuthorizationError('Task authorization already has a production claim');
    if (mode === 'task-scoped-auto') {
      process.env.RELEASE_AUTHORIZATION_MODE = 'TASK_SCOPED_AUTO';
      process.env.RELEASE_AUTHORIZATION_ID = authorizationId;
      process.env.RELEASE_AUTHORIZATION_SOURCE = taskAuthorization.sourceReference;
      process.env.RELEASE_AUTHORIZATION_RECORD_SHA256 = taskAuthorization.recordSha256;
    }
  }
  if (mode === 'preflight') {
    releaseCode = 0;
  } else {
    const result = spawnSync('pnpm', ['release:gpoint', '--apply', '--candidate', sha, '--confirm-production-apply'], {
      cwd: root, stdio: 'inherit', timeout: 1_800_000,
    });
    releaseCode = result.status ?? 1;
    if (mode === 'task-scoped-auto' && releaseCode === 0) {
      try {
        await consumeTaskAuthorization({project: 'gpoint', authorizationId: process.env.TASK_AUTHORIZATION_ID,
          candidateSha: sha, token: process.env.GITHUB_TOKEN});
        authorizationConsumed = true;
        process.env.RELEASE_AUTHORIZATION_CONSUMED = 'true';
      } catch {
        releaseCode = 1;
        process.env.RELEASE_AUTHORIZATION_CONSUMED = 'pending';
        console.error('TASK_AUTHORIZATION_CONSUMPTION=RECOVERY_REQUIRED');
      }
    }
  }
} finally {
  const directory = ledger.manifestDir;
  const records = fs.readdirSync(directory).filter(name => !previous.has(name)).map(name => ({
    name, data: JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8')),
  })).filter(record => record.data.candidateSha === sha);
  const setupStates = new Set(['CONFIG_PROVISION_BASELINE_RECORDED', 'GSales_CONFIG_PROVISIONED', 'WORKER_CONFIGURED']);
  const attempt = (mode === 'preflight' ? records : records.filter(r => r.data.status !== 'PLANNED' && !setupStates.has(r.data.status))).at(-1);
  const evidence = attempt?.data ?? { candidateSha: sha, status: 'BLOCKED', productionRef: null };
  const status = mode === 'task-scoped-auto' && evidence.status === 'COMPLETE' && !authorizationConsumed
    ? 'RECOVERY_REQUIRED' : evidence.status;
  fs.writeFileSync(process.env.RELEASE_RESULT, JSON.stringify({ ...withPreflightEvidence(evidence, preflightStatus, mode),
    mode,
    status,
    releaseState: mode === 'preflight' && evidence.status === 'PLANNED' ? 'READY_FOR_OWNER' :
      ['production', 'task-scoped-auto'].includes(mode) && evidence.status === 'COMPLETE' ?
        (mode === 'task-scoped-auto' && !authorizationConsumed ? 'RECOVERY_REQUIRED' : 'COMPLETE') : evidence.status,
    authorizationMode: mode === 'task-scoped-auto' ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA',
    authorizationId: process.env.TASK_AUTHORIZATION_ID ?? null,
    authorizationSourceReference: taskAuthorization?.sourceReference ?? null,
    authorizationRecordSha256: taskAuthorization?.recordSha256 ?? null,
    authorizationChangedPaths: taskAuthorization?.changedPaths ?? null,
    authorizationConsumed,
    authorizationSource: process.env.GITHUB_ACTOR ?? '',
    cachePreventedDownload: process.env.CACHE_PREVENTED_DOWNLOAD ?? 'NO',
    bundleDownloadSeconds: process.env.BUNDLE_DOWNLOAD_SECONDS ?? '0',
    productionBaselineSha: evidence.previousProductionSha ?? null,
    prepareRunId: process.env.PREPARE_RUN_ID,
    prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
    protectedSetupEvidence: records.filter(r => setupStates.has(r.data.status)).map(r => r.data),
    operator: process.env.GITHUB_ACTOR, automationRunId: process.env.GITHUB_RUN_ID, automationRunAttempt: process.env.GITHUB_RUN_ATTEMPT,
    applicationVerification: mode === 'preflight' ? 'NOT_DEPLOYED' : 'OWNER_SMOKE_PENDING',
  }, null, 2) + '\n');
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `preflight=${preflightStatus}\nstatus=${evidence.status}\n`);
  }
  // Persist canonical evidence to the dedicated ledger, never application main.
  const bookkeeping = persistReleaseBookkeeping({ mode, records, evidence, git, ...ledger });
  console.log(`RELEASE_BOOKKEEPING=${bookkeeping}`);
}
process.exitCode = releaseCode;
