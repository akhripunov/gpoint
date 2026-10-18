#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { verifyReleaseBundle, selectImageTransport } from './release-bundle.mjs';
import { fileURLToPath } from 'node:url';
import { candidateAgataMigrationHead, candidatePrismaMigrationHead, sanitizeReleaseOutput } from './release-gpoint.mjs';
import { assertInstalledWorkerMatches } from './worker-runtime-guard.mjs';
import { verifyOciReadAccess } from './release-oci.mjs';
import { withPreflightEvidence } from './release-evidence.mjs';
import { AuthorizationError, authorizationRecordPath, candidateScopeDiff,
  checkTaskAuthorizationState, consumeTaskAuthorization, validateTaskAuthorization,
  assertNoTaskScopedMigrations } from './release-authorization.mjs';
import { createGitHubDeploymentsClient, assertDeploymentMatchesLive, deploymentIdentity } from './github-deployments.mjs';
import { factsFromReadOnlyInspection, initializeRunScopedState, stateFromLiveVps } from './deployment-run-state.mjs';
import { inspectProductionReadOnly } from './release-gpoint-vps-adapter.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const sha = process.env.CANDIDATE_SHA;
const mode = process.env.RELEASE_MODE;
if (!['preflight', 'production', 'task-scoped-auto'].includes(mode)) throw new Error('Explicit release mode is required');
if (mode === 'production' && process.env.APPROVE_PRODUCTION !== 'true') throw new Error('Explicit production approval is required');
if (mode === 'task-scoped-auto' && (process.env.APPROVE_PRODUCTION !== 'false' || !process.env.TASK_AUTHORIZATION_ID)) throw new Error('TASK_SCOPED_AUTO requires its durable authorization ID and no exact-SHA approval input');
if (!['preflight', 'task-scoped-auto'].includes(mode) && process.env.TASK_AUTHORIZATION_ID) throw new Error('Task authorization ID is only valid in linked preflight or TASK_SCOPED_AUTO mode');
const proof = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, sha);
if (String(proof.runId) !== process.env.PREPARE_RUN_ID) throw new Error('Receipt does not belong to the approved GitHub run');
const selectedImageTransport = selectImageTransport(proof, process.env.IMAGE_TRANSPORT || 'prepared');
if (selectedImageTransport === 'ghcr-oci') {
  await verifyOciReadAccess(proof, process.env.GHCR_READ_USERNAME, process.env.GHCR_READ_TOKEN);
}
const runGit = (args, input, timeout) => execFileSync('git', args, { cwd: root, encoding: 'utf8', input, timeout }).trim();
const git = args => runGit(args);
const executorSha = sha;
if (!/^[a-f0-9]{40}$/.test(executorSha ?? '') || git(['rev-parse', 'HEAD']) !== executorSha
    || git(['status', '--porcelain'])) throw new Error('Release checkout must be clean at its verified executor SHA');
const checksum = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
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
const workerRoot = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/runtime/current');
const workerAssets = ['gpoint_mac_worker.py', 'agata_worker_pool.py', 'lab_queue.py', 'lab_fixture_manifest.json', 'requirements.txt', 'run_codex_worker.sh'];
assertInstalledWorkerMatches(mode, workerAssets.map(asset => {
  const sourceBytes = execFileSync('git', ['show', `${sha}:services/agata/worker/${asset}`], {
    cwd: root, encoding: 'buffer', stdio: ['ignore', 'pipe', 'ignore'],
  });
  const installed = path.join(workerRoot, asset);
  const installedExists = fs.existsSync(installed);
  return { sourceExists: true, installedExists,
    matches: installedExists && crypto.createHash('sha256').update(sourceBytes).digest('hex') === checksum(installed) };
}));
if (!process.env.RELEASE_RESULT || !process.env.GITHUB_RUN_ID) throw new Error('Release evidence paths are required');
const runDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-deployment-attempt-'));
const liveBaseline = initializeRunScopedState({directory: runDirectory});
process.env.GPOINT_RELEASE_STATE_FILE = liveBaseline.stateFile;
process.env.GPOINT_RELEASE_MANIFEST_DIR = liveBaseline.manifestDir;
process.env.GPOINT_RELEASE_LIVE_BASELINE_FILE = liveBaseline.stateFile;
const deploymentClient = createGitHubDeploymentsClient({token: process.env.GITHUB_TOKEN});
const deploymentBaseline = await deploymentClient.readProductionBaseline();
assertDeploymentMatchesLive(deploymentBaseline, {runtimeSha: liveBaseline.state.currentProductionSha,
  miniappSha: liveBaseline.state.miniappSourceSha, prismaHead: liveBaseline.state.currentProductionDbHead});
const previous = new Set(fs.readdirSync(liveBaseline.manifestDir));
let releaseCode = 1;
let preflightStatus = 'BLOCKED';
let executionFailure = null;
let deployment = null;
let applyStarted = false;
let installationVerified = false;
let finalSuccessStatusStarted = false;
try {
  const preflightEnv = { ...process.env, IMAGE_TRANSPORT: selectedImageTransport,
    GPOINT_RELEASE_SKIP_BUILD: '1' };
  delete preflightEnv.GHCR_READ_TOKEN;
  const preflight = spawnSync('pnpm', ['release:gpoint', '--dry-run', '--candidate', sha], {
    cwd: root, env: preflightEnv, stdio: 'inherit', timeout: 2_100_000,
  });
  preflightStatus = preflight.status === 0 ? 'PLANNED' : 'FAILED';
  if (preflight.status !== 0) throw new Error('Canonical preflight blocked the release');
  const linkedAuthorizationId = process.env.TASK_AUTHORIZATION_ID;
  if (mode === 'task-scoped-auto' || (mode === 'preflight' && linkedAuthorizationId)) {
    const authorizationId = linkedAuthorizationId;
    const currentMainSha = (await github('git/ref/heads/main')).object?.sha;
    const planned = fs.readdirSync(liveBaseline.manifestDir).filter(name => !previous.has(name))
      .map(name => JSON.parse(fs.readFileSync(path.join(liveBaseline.manifestDir, name), 'utf8')))
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
  if (mode === 'preflight') {
    releaseCode = 0;
  } else {
    const immediateVps = stateFromLiveVps(factsFromReadOnlyInspection(inspectProductionReadOnly()));
    assertDeploymentMatchesLive(deploymentBaseline, {runtimeSha: immediateVps.currentProductionSha,
      miniappSha: immediateVps.miniappSourceSha, prismaHead: immediateVps.currentProductionDbHead});
    if (immediateVps.currentProductionSha !== liveBaseline.state.currentProductionSha
        || immediateVps.currentProductionDbHead !== liveBaseline.state.currentProductionDbHead)
      throw new Error('VPS or database baseline changed after preflight; refusing production mutation');
    const prepareTag = `gpoint-prepare-${sha}-${process.env.PREPARE_RUN_ID}-${process.env.PREPARE_RUN_ATTEMPT}`;
    const prepareRelease = await github(`releases/tags/${prepareTag}`);
    const prepareAssetId = Number(process.env.PREPARE_ASSET_ID);
    const prepareAsset = prepareRelease?.assets?.find(asset => asset.id === prepareAssetId && asset.name === 'bundle.zip');
    if (!prepareRelease || !Number.isSafeInteger(prepareAssetId) || !prepareAsset
        || !/^sha256:[a-f0-9]{64}$/.test(prepareAsset.digest ?? ''))
      throw new Error('Exact immutable Prepare Release and bundle asset identity are unavailable');
    const workflowUrl = `https://github.com/akhripunov/gpoint/actions/runs/${process.env.GITHUB_RUN_ID}/attempts/${process.env.GITHUB_RUN_ATTEMPT}`;
    const requiredContext = 'GPoint — Prepare / prepare';
    const checkRuns = [];
    for (let page = 1; page <= 100; page += 1) {
      const checks = await github(`commits/${sha}/check-runs?per_page=100&page=${page}`);
      if (!Array.isArray(checks.check_runs) || !Number.isSafeInteger(checks.total_count))
        throw new Error('Exact-SHA check-run inventory is incomplete');
      checkRuns.push(...checks.check_runs);
      if (checkRuns.length >= checks.total_count) break;
      if (page === 100) throw new Error('Exact-SHA check-run inventory exceeded the pagination limit');
    }
    const prepareCheck = checkRuns.find(check => check.name === requiredContext
      && check.status === 'completed' && check.conclusion === 'success' && check.head_sha === sha);
    if (!prepareCheck) throw new Error('Required successful exact-SHA Prepare status context is unavailable');
    deployment = (await deploymentClient.beginProductionDeployment({candidateSha: sha,
      runId: process.env.GITHUB_RUN_ID, runAttempt: process.env.GITHUB_RUN_ATTEMPT,
      prepareRunId: process.env.PREPARE_RUN_ID, prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
      prepareReleaseId: prepareRelease.id, prepareReleaseTag: prepareTag, prepareAssetId,
      prepareAssetSha256: prepareAsset.digest,
      authorizationMode: mode === 'task-scoped-auto' ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA',
      authorizationId: process.env.TASK_AUTHORIZATION_ID, logUrl: workflowUrl},
    {requiredContexts: [requiredContext]})).deployment;
    applyStarted = true;
    const result = spawnSync('pnpm', ['release:gpoint', '--apply', '--candidate', sha, '--confirm-production-apply'], {
      cwd: root, stdio: 'inherit', timeout: 1_800_000,
    });
    releaseCode = result.status ?? 1;
    if (releaseCode === 0) {
      const installed = stateFromLiveVps(factsFromReadOnlyInspection(inspectProductionReadOnly()));
      if (installed.currentProductionSha !== sha || installed.miniappSourceSha !== sha ||
          installed.currentProductionDbHead !== proof.prismaHead ||
          installed.agataAlembicHead !== proof.agataHead ||
          installed.gsalesMigrationHead !== proof.gsalesMigrationHead ||
          installed.gsalesMigrationSha256 !== proof.gsalesMigrationSha256)
        throw new Error('Independent post-Apply VPS readback does not match the candidate source and migration identities');
      const records = fs.readdirSync(liveBaseline.manifestDir).map(name => JSON.parse(fs.readFileSync(path.join(liveBaseline.manifestDir, name), 'utf8')));
      const completed = records.findLast(item => item.candidateSha === sha && item.status === 'COMPLETE');
      if (!completed || completed.productionShaAfter !== sha)
        throw new Error('Apply returned success without complete local install evidence');
      installationVerified = true;
    }
    if (mode === 'task-scoped-auto' && releaseCode === 0) {
      try {
        await consumeTaskAuthorization({project: 'gpoint', authorizationId: process.env.TASK_AUTHORIZATION_ID,
          candidateSha: sha, token: process.env.GITHUB_TOKEN});
        authorizationConsumed = true;
        process.env.RELEASE_AUTHORIZATION_CONSUMED = 'true';
      } catch (error) {
        throw new AuthorizationError('TASK_SCOPED_AUTO consumption failed after Apply; deployment remains unconfirmed', {cause: error});
      }
    }
    if (releaseCode === 0) {
      finalSuccessStatusStarted = true;
      await deploymentClient.publishStatus(deployment, deploymentIdentity(deployment), {state: 'success',
        description: `stage=complete sha=${sha.slice(0, 12)} run=${process.env.GITHUB_RUN_ID}/${process.env.GITHUB_RUN_ATTEMPT}`,
        logUrl: workflowUrl});
    } else {
      try {
        const afterFailure = stateFromLiveVps(factsFromReadOnlyInspection(inspectProductionReadOnly()));
        executionFailure = {stage: 'POST_APPLY_READBACK', executionStatus: 'FAILED',
          diagnostic: 'Apply failed; performed independent read-only VPS inspection before publishing error status',
          observedRuntimeSha: afterFailure.currentProductionSha,
          observedPrismaHead: afterFailure.currentProductionDbHead};
      } catch (readbackError) {
        executionFailure = {stage: 'POST_APPLY_READBACK', executionStatus: 'BLOCKED',
          diagnostic: `Apply failed and independent VPS inspection was unavailable: ${sanitizeReleaseOutput(readbackError?.message)}`};
      }
      await deploymentClient.publishStatus(deployment, deploymentIdentity(deployment), {state: 'error',
        description: `stage=post_apply sha=${sha.slice(0, 12)} run=${process.env.GITHUB_RUN_ID}/${process.env.GITHUB_RUN_ATTEMPT}`,
        logUrl: workflowUrl});
    }
  }
} catch (error) {
  deployment ??= error?.deployment ?? null;
  releaseCode = 1;
  executionFailure = {stage: applyStarted ? 'POST_APPLY_READBACK_OR_STATUS' : 'PRE_APPLY',
    executionStatus: 'FAILED', diagnostic: sanitizeReleaseOutput(error?.message)};
  if (deployment && (!installationVerified || !finalSuccessStatusStarted)) {
    try {
      const logUrl = `https://github.com/akhripunov/gpoint/actions/runs/${process.env.GITHUB_RUN_ID}/attempts/${process.env.GITHUB_RUN_ATTEMPT}`;
      await deploymentClient.publishStatus(deployment, deploymentIdentity(deployment), {state: applyStarted ? 'error' : 'failure',
        description: `stage=${applyStarted ? 'post_apply' : 'pre_apply'} sha=${sha.slice(0, 12)} run=${process.env.GITHUB_RUN_ID}/${process.env.GITHUB_RUN_ATTEMPT}`,
        logUrl});
    } catch (statusError) { console.error(`DEPLOYMENT_STATUS_UNCONFIRMED=${sanitizeReleaseOutput(statusError?.message)}`); }
  }
} finally {
  const directory = liveBaseline.manifestDir;
  const records = fs.readdirSync(directory).filter(name => !previous.has(name)).map(name => ({
    name, data: JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8')),
  })).filter(record => record.data.candidateSha === sha);
  const setupStates = new Set(['CONFIG_PROVISION_BASELINE_RECORDED', 'GSales_CONFIG_PROVISIONED', 'WORKER_CONFIGURED']);
  const attempt = (mode === 'preflight' ? records : records.filter(r => r.data.status !== 'PLANNED' && !setupStates.has(r.data.status))).at(-1);
  let evidence = attempt?.data ?? { candidateSha: sha, status: 'BLOCKED', productionRef: null };
  const status = mode === 'task-scoped-auto' && evidence.status === 'COMPLETE' && !authorizationConsumed
      ? 'INSTALLATION_UNCONFIRMED' : evidence.status;
  fs.writeFileSync(process.env.RELEASE_RESULT, JSON.stringify({ ...withPreflightEvidence(evidence, preflightStatus, mode),
    mode,
    status,
    ...(executionFailure ? {executionFailure} : {}),
    releaseState: mode === 'preflight' && evidence.status === 'PLANNED' ? 'READY_FOR_OWNER' :
      ['production', 'task-scoped-auto'].includes(mode) && evidence.status === 'COMPLETE' ?
        (mode === 'task-scoped-auto' && !authorizationConsumed ? 'INSTALLATION_UNCONFIRMED' : 'COMPLETE') : evidence.status,
    authorizationMode: mode === 'task-scoped-auto' ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA',
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
    prepareRunId: process.env.PREPARE_RUN_ID,
    prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
    protectedSetupEvidence: records.filter(r => setupStates.has(r.data.status)).map(r => r.data),
    operator: process.env.GITHUB_ACTOR, automationRunId: process.env.GITHUB_RUN_ID, automationRunAttempt: process.env.GITHUB_RUN_ATTEMPT,
    applicationVerification: mode === 'preflight' ? 'NOT_DEPLOYED' :
      installationVerified ? 'VPS_READBACK_VERIFIED' : 'NOT_VERIFIED',
  }, null, 2) + '\n');
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT,
      `preflight=${preflightStatus}\nstatus=${status}\n`);
  }
  console.log(`GITHUB_DEPLOYMENT_ID=${deployment?.id ?? 'NOT_CREATED'} VPS_BASELINE_SHA=${liveBaseline.state.currentProductionSha}`);
  fs.rmSync(runDirectory, {recursive: true, force: true});
}
process.exitCode = releaseCode;
