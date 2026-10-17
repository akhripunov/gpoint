#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { createRetryingGit } from './release-network-retry.mjs';
import { verifyReleaseBundle, selectImageTransport } from './release-bundle.mjs';
import { persistReleaseBookkeeping, loadReleaseBookkeeping } from './release-bookkeeping.mjs';
import { fileURLToPath } from 'node:url';
import { candidateAgataMigrationHead, candidatePrismaMigrationHead, githubLedgerGitEnvironment } from './release-gpoint.mjs';
import { assertInstalledWorkerMatches } from './worker-runtime-guard.mjs';
import { verifyOciReadAccess } from './release-oci.mjs';
import { recoveryEvidenceFromReadback, withPreflightEvidence } from './release-evidence.mjs';
import { AuthorizationError, authorizationRecordPath, candidateScopeDiff,
  checkTaskAuthorizationState, consumeTaskAuthorization, validateTaskAuthorization,
  assertNoTaskScopedMigrations } from './release-authorization.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const sha = process.env.CANDIDATE_SHA;
const mode = process.env.RELEASE_MODE;
if (!['preflight', 'production', 'task-scoped-auto', 'recovery-diagnostics', 'recovery-finalization'].includes(mode)) throw new Error('Explicit release mode is required');
if (['production', 'recovery-finalization'].includes(mode) && process.env.APPROVE_PRODUCTION !== 'true') throw new Error('Explicit production approval is required');
if (mode === 'task-scoped-auto' && (process.env.APPROVE_PRODUCTION !== 'false' || !process.env.TASK_AUTHORIZATION_ID)) throw new Error('TASK_SCOPED_AUTO requires its durable authorization ID and no exact-SHA approval input');
if (!['preflight', 'task-scoped-auto'].includes(mode) && process.env.TASK_AUTHORIZATION_ID) throw new Error('Task authorization ID is only valid in linked preflight or TASK_SCOPED_AUTO mode');
if (mode === 'recovery-diagnostics' && (process.env.APPROVE_PRODUCTION !== 'false' || process.env.TASK_AUTHORIZATION_ID))
  throw new Error('Read-only recovery diagnostics forbid production approval and task authorization');
if (mode === 'recovery-finalization') {
  if (process.env.GPOINT_RECOVERY_DB_RISK_ACCEPTED !== 'true'
      || process.env.GPOINT_RECOVERY_DB_RISK_CANDIDATE_SHA !== process.env.CANDIDATE_SHA
      || !/^[a-f0-9]{40}$/.test(process.env.GPOINT_RECOVERY_DB_RISK_BASELINE_SHA ?? '')
      || !process.env.GPOINT_RECOVERY_DB_RISK_DECISION_ID
      || process.env.GPOINT_RECOVERY_CREATE_BACKUP !== 'true'
      || !process.env.GPOINT_RECOVERY_FAILURE_EVIDENCE) {
    throw new Error('Recovery finalization requires exact candidate, baseline DB-risk, current-backup, and original failure evidence approvals');
  }
}
const proof = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, sha);
if (String(proof.runId) !== process.env.PREPARE_RUN_ID) throw new Error('Receipt does not belong to the approved GitHub run');
const selectedImageTransport = selectImageTransport(proof, process.env.IMAGE_TRANSPORT || 'prepared');
if (selectedImageTransport === 'ghcr-oci') {
  await verifyOciReadAccess(proof, process.env.GHCR_READ_USERNAME, process.env.GHCR_READ_TOKEN);
}
const runGit = (args, input, timeout) => execFileSync('git', args, { cwd: root, encoding: 'utf8', input, timeout,
  env: githubLedgerGitEnvironment(args, process.env.GITHUB_TOKEN) }).trim();
const git = createRetryingGit(runGit);
const executorSha = ['recovery-diagnostics', 'recovery-finalization'].includes(mode) ? process.env.GITHUB_SHA : sha;
if (!/^[a-f0-9]{40}$/.test(executorSha ?? '') || git(['rev-parse', 'HEAD']) !== executorSha
    || git(['status', '--porcelain'])) throw new Error('Release checkout must be clean at its verified executor SHA');
if (['recovery-diagnostics', 'recovery-finalization'].includes(mode)) {
  const ancestry = spawnSync('git', ['merge-base', '--is-ancestor', sha, executorSha], {cwd: root, stdio: 'ignore'});
  if (ancestry.status !== 0) throw new Error('Recovery candidate is not an ancestor of its verified executor SHA');
}
const checksum = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
function resultStatusFromManifest(directory, candidateSha, previousNames) {
  const records = fs.readdirSync(directory).filter(name => !previousNames.has(name) && name.endsWith('.json'));
  const record = records.map(name => JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8')))
    .find(value => value.candidateSha === candidateSha && value.eventType === 'RECOVERY_READ_ONLY_DIAGNOSTICS');
  if (!record || !record.recoverySnapshot) return 1;
  return record.recoverySnapshot.status === 'FACTS_COMPLETE' ? 0 : 1;
}
async function github(route) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new AuthorizationError('Scoped GitHub read token is required for release evidence checks');
  const response = await fetch(`https://api.github.com/repos/akhripunov/gpoint/${route}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
  });
  if (!response.ok) throw new AuthorizationError(`GitHub task authorization check failed (HTTP ${response.status})`);
  return response.json();
}
let taskAuthorization = null;
let authorizationConsumed = false;
let recoveryActiveDeploys = null;
if (mode === 'recovery-diagnostics') {
  if (!process.env.GPOINT_RECOVERY_FAILURE_EVIDENCE) throw new Error('Verified immutable incident evidence is required for recovery diagnostics');
  const runs = [];
  let totalCount = null;
  for (let page = 1; page <= 20; page += 1) {
    const inventory = await github(`actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100&page=${page}`);
    if (!Array.isArray(inventory.workflow_runs) || !Number.isInteger(inventory.total_count)
        || (totalCount !== null && inventory.total_count !== totalCount))
      throw new Error('GitHub active Deploy inventory is incomplete or changed during read');
    totalCount = inventory.total_count;
    runs.push(...inventory.workflow_runs);
    if (runs.length >= totalCount) break;
  }
  if (totalCount === null || runs.length !== totalCount) throw new Error('GitHub active Deploy inventory is incomplete');
  recoveryActiveDeploys = {workflow_runs: runs, total_count: totalCount};
  process.env.GPOINT_RECOVERY_ACTIVE_DEPLOYS = JSON.stringify(recoveryActiveDeploys);
}
const workerRoot = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/runtime/current');
const workerAssets = ['gpoint_mac_worker.py', 'agata_worker_pool.py', 'lab_queue.py', 'lab_fixture_manifest.json', 'requirements.txt', 'run_codex_worker.sh'];
if (mode !== 'recovery-diagnostics') assertInstalledWorkerMatches(mode, workerAssets.map(asset => {
  const sourceBytes = execFileSync('git', ['show', `${sha}:services/agata/worker/${asset}`], {
    cwd: root, encoding: 'buffer', stdio: ['ignore', 'pipe', 'ignore'],
  });
  const installed = path.join(workerRoot, asset);
  const installedExists = fs.existsSync(installed);
  return { sourceExists: true, installedExists,
    matches: installedExists && crypto.createHash('sha256').update(sourceBytes).digest('hex') === checksum(installed) };
}));
if (!process.env.RELEASE_RESULT || !process.env.GITHUB_RUN_ID) throw new Error('Release evidence paths are required');
const ledger = loadReleaseBookkeeping({ root, git, destination: process.env.GPOINT_RELEASE_EVIDENCE });
process.env.GPOINT_RELEASE_STATE_FILE = ledger.stateFile;
process.env.GPOINT_RELEASE_MANIFEST_DIR = ledger.manifestDir;
process.env.GPOINT_RELEASE_LEDGER_SHA = ledger.ledgerSha ?? '';
const previous = new Set(fs.readdirSync(ledger.manifestDir));
let releaseCode = 1;
let preflightStatus = 'BLOCKED';
try {
  if (mode === 'recovery-diagnostics') {
    // This branch has no Apply, migration, rollback, tag, or ledger writer.
    const result = spawnSync('pnpm', ['release:gpoint', '--recovery-diagnostics', '--candidate', sha], {
      cwd: root, env: {...process.env, GPOINT_RELEASE_SKIP_BUILD: '1'}, stdio: 'inherit', timeout: 1_800_000,
    });
    releaseCode = result.status ?? 1;
    preflightStatus = 'NOT_APPLICABLE';
  } else if (mode === 'recovery-finalization') {
    // Recovery has its own complete read-only gates inside the finalizer; it
    // must never enter ordinary Apply or migration code.
    const result = spawnSync('pnpm', ['release:gpoint', '--finalize-recovery', '--candidate', sha], {
      cwd: root, stdio: 'inherit', timeout: 1_800_000,
    });
    releaseCode = result.status ?? 1;
    preflightStatus = releaseCode === 0 ? 'PASS' : 'FAILED';
  } else {
    // The first ordinary production operation is the canonical read-only preflight.
    const preflightEnv = { ...process.env, IMAGE_TRANSPORT: selectedImageTransport,
      GPOINT_RELEASE_SKIP_BUILD: '1' };
    delete preflightEnv.GHCR_READ_TOKEN;
    const preflight = spawnSync('pnpm', ['release:gpoint', '--dry-run', '--candidate', sha], {
      cwd: root, env: preflightEnv, stdio: 'inherit', timeout: 2_100_000,
    });
    preflightStatus = preflight.status === 0 ? 'PLANNED' : 'FAILED';
    if (preflight.status !== 0) throw new Error('Canonical preflight blocked the release');
  }
  const linkedAuthorizationId = process.env.TASK_AUTHORIZATION_ID;
  if (mode !== 'recovery-finalization' && (mode === 'task-scoped-auto' || (mode === 'preflight' && linkedAuthorizationId))) {
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
      process.env.RELEASE_AUTHORIZATION_SOURCE_INSTRUCTION_ID = taskAuthorization.sourceInstructionIdentifier;
      process.env.RELEASE_AUTHORIZATION_RECORD_SHA256 = taskAuthorization.recordSha256;
      process.env.RELEASE_AUTHORIZATION_SCOPE_SHA256 = taskAuthorization.ownerTaskCommitmentSha256;
    }
  }
  if (mode === 'recovery-diagnostics') {
    // The diagnostics command only writes a workflow artifact; it has no Apply path.
    if (releaseCode === 0) releaseCode = resultStatusFromManifest(ledger.manifestDir, sha, previous);
  } else if (mode === 'recovery-finalization') {
    // release-gpoint persists every recovery ledger checkpoint with CAS and
    // remote read-back; this wrapper intentionally performs no second write.
  } else if (mode === 'preflight') {
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
  let evidence = attempt?.data ?? { candidateSha: sha, status: 'BLOCKED', productionRef: null };
  let recoveryReadbackState = null;
  if (mode === 'recovery-finalization') {
    let destination;
    try {
      destination = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-recovery-result-read-'));
      const check = loadReleaseBookkeeping({root, git, destination});
      recoveryReadbackState = JSON.parse(fs.readFileSync(check.stateFile, 'utf8'));
      if (recoveryReadbackState.recoveryRequired === true || recoveryReadbackState.lastReleaseStatus === 'COMPLETE')
        evidence = recoveryEvidenceFromReadback(recoveryReadbackState, check.manifestDir, sha);
    } catch { recoveryReadbackState = null; }
    finally { if (destination) fs.rmSync(destination, {recursive: true, force: true}); }
  }
  const status = mode === 'recovery-diagnostics'
    ? (evidence.recoverySnapshot?.status ?? 'INCOMPLETE')
    : mode === 'recovery-finalization'
    ? (releaseCode === 0 && recoveryReadbackState?.currentProductionSha === sha
      && recoveryReadbackState.lastReleaseStatus === 'COMPLETE' && recoveryReadbackState.recoveryRequired === false
      ? 'COMPLETE' : recoveryReadbackState?.recoveryRequired === true
        ? 'RECOVERY_REQUIRED' : 'BLOCKED')
    : mode === 'task-scoped-auto' && evidence.status === 'COMPLETE' && !authorizationConsumed
      ? 'RECOVERY_REQUIRED' : evidence.status;
  fs.writeFileSync(process.env.RELEASE_RESULT, JSON.stringify({ ...withPreflightEvidence(evidence, preflightStatus, mode),
    mode,
    status,
    releaseState: mode === 'recovery-diagnostics' ? 'RECOVERY_DIAGNOSTICS_ONLY'
      : mode === 'recovery-finalization' ? status : mode === 'preflight' && evidence.status === 'PLANNED' ? 'READY_FOR_OWNER' :
      ['production', 'task-scoped-auto'].includes(mode) && evidence.status === 'COMPLETE' ?
        (mode === 'task-scoped-auto' && !authorizationConsumed ? 'RECOVERY_REQUIRED' : 'COMPLETE') : evidence.status,
    authorizationMode: mode === 'recovery-diagnostics' ? 'READ_ONLY_RECOVERY' : mode === 'task-scoped-auto' ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA',
    authorizationId: process.env.TASK_AUTHORIZATION_ID ?? null,
    authorizationSourceInstructionId: taskAuthorization?.sourceInstructionIdentifier ?? null,
    authorizationScopeSha256: taskAuthorization?.ownerTaskCommitmentSha256 ?? null,
    authorizationRecordSha256: taskAuthorization?.recordSha256 ?? null,
    authorizationChangedPaths: taskAuthorization?.changedPaths ?? null,
    authorizationConsumed,
    authorizationSource: process.env.GITHUB_ACTOR ?? '',
    cachePreventedDownload: process.env.CACHE_PREVENTED_DOWNLOAD ?? 'NO',
    bundleDownloadSeconds: process.env.BUNDLE_DOWNLOAD_SECONDS ?? '0',
    productionBaselineSha: evidence.previousProductionSha ?? null,
    recoveryReadbackState: mode === 'recovery-finalization' ? recoveryReadbackState?.lastReleaseStatus ?? 'UNKNOWN' : null,
    prepareRunId: process.env.PREPARE_RUN_ID,
    prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
    protectedSetupEvidence: records.filter(r => setupStates.has(r.data.status)).map(r => r.data),
    operator: process.env.GITHUB_ACTOR, automationRunId: process.env.GITHUB_RUN_ID, automationRunAttempt: process.env.GITHUB_RUN_ATTEMPT,
    applicationVerification: mode === 'preflight' ? 'NOT_DEPLOYED' : mode === 'recovery-finalization' ? 'NOT_VERIFIED' : 'OWNER_SMOKE_PENDING',
  }, null, 2) + '\n');
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT,
      `preflight=${['recovery-finalization', 'recovery-diagnostics'].includes(mode) ? 'NOT_APPLICABLE' : preflightStatus}\nstatus=${status}\n`
      + (mode === 'recovery-diagnostics' ? `snapshot_sha256=${evidence.recoverySnapshot?.snapshotSha256 ?? ''}\n` : ''));
  }
  // Persist canonical evidence to the dedicated ledger, never application main.
  const bookkeeping = mode === 'recovery-diagnostics' ? 'READ_ONLY_DIAGNOSTIC_ARTIFACT_ONLY'
    : mode === 'recovery-finalization' ? 'PERSISTED_BY_FINALIZATION_CHECKPOINTS'
    : persistReleaseBookkeeping({ mode, records, evidence, git, ...ledger });
  console.log(`RELEASE_BOOKKEEPING=${bookkeeping}`);
}
process.exitCode = releaseCode;
