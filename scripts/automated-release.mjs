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
import { createGitHubDeploymentsClient, assertDeploymentMatchesLive, deploymentIdentity,
  selectTrustedPrepareCheck } from './github-deployments.mjs';
import { factsFromReadOnlyInspection, initializeRunScopedState, stateFromLiveVps } from './deployment-run-state.mjs';
import { inspectProductionReadOnly } from './release-gpoint-vps-adapter.mjs';
import { reconcileVerifiedDeployment, reconciledResultEvidence } from './reconcile-deployment.mjs';
import { executeProductionDeployment, deploymentResultStatus } from './deployment-execution.mjs';

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
if (selectedImageTransport === 'ghcr-oci' && proof.staticOnly !== true) {
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
const priorDeployment = await deploymentClient.findDeploymentForRun(process.env.GITHUB_RUN_ID);
const deploymentBaseline = priorDeployment ? null : await deploymentClient.readProductionBaseline();
if (!priorDeployment) assertDeploymentMatchesLive(deploymentBaseline, {runtimeSha: liveBaseline.state.currentProductionSha,
  miniappSha: liveBaseline.state.miniappSourceSha, prismaHead: liveBaseline.state.currentProductionDbHead});
const previous = new Set(fs.readdirSync(liveBaseline.manifestDir));
let releaseCode = 1;
let preflightStatus = 'BLOCKED';
let executionFailure = null;
let deployment = null;
let applyStarted = false;
let installationVerified = false;
let finalSuccessStatusStarted = false;
let reconciliationEvidence = null;
let metadataReconciliationStarted = false;
try {
  if (priorDeployment) {
    metadataReconciliationStarted = true;
    deployment = priorDeployment;
    const original = deploymentIdentity(priorDeployment);
    const result = await reconcileVerifiedDeployment({client: deploymentClient,
      deploymentId: priorDeployment.id, runId: process.env.GITHUB_RUN_ID,
      currentRunAttempt: process.env.GITHUB_RUN_ATTEMPT, candidateSha: sha,
      prepareRunId: process.env.PREPARE_RUN_ID, prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
      prepareReceiptDigest: process.env.GPOINT_PREPARE_RECEIPT_DIGEST, token: process.env.GITHUB_TOKEN,
      prepareProof: proof,
      inspectVps: inspectProductionReadOnly});
    installationVerified = true;
    finalSuccessStatusStarted = true;
    authorizationConsumed = result.originalEvidence.authorizationConsumed === true;
    releaseCode = 0;
    preflightStatus = 'PLANNED';
    reconciliationEvidence = reconciledResultEvidence(result.originalEvidence, {candidateSha: sha, status: 'COMPLETE', releaseState: 'COMPLETE',
      deploymentId: priorDeployment.id, deployRunId: original.runId, deployRunAttempt: original.runAttempt,
      prepareRunId: original.prepareRunId, prepareRunAttempt: original.prepareRunAttempt,
      applicationVerification: 'VPS_READBACK_VERIFIED', dbHeadAfter: liveBaseline.state.currentProductionDbHead,
      agataDbHeadAfter: liveBaseline.state.agataAlembicHead,
      gsalesDbHeadAfter: liveBaseline.state.gsalesMigrationHead,
      gsalesMigrationSha256: liveBaseline.state.gsalesMigrationSha256,
      gsalesMigrationSha256After: liveBaseline.state.gsalesMigrationSha256,
      reconciliation: {evidenceVerified: result.evidenceVerified, liveVerified: result.liveVerified}});
  } else {
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
    const prepareReceiptDigest = process.env.GPOINT_PREPARE_RECEIPT_DIGEST;
    const prepareReceiptSha256 = process.env.GPOINT_PREPARE_RECEIPT_SHA256;
    if (!/^sha256:[a-f0-9]{64}$/.test(prepareReceiptDigest ?? '')
        || !/^[a-f0-9]{64}$/.test(prepareReceiptSha256 ?? ''))
      throw new Error('Exact GHCR Prepare receipt and image digest identities are unavailable');
    const workflowUrl = `https://github.com/akhripunov/gpoint/actions/runs/${process.env.GITHUB_RUN_ID}/attempts/${process.env.GITHUB_RUN_ATTEMPT}`;
    const requiredContext = 'prepare';
    const checkRuns = [];
    for (let page = 1; page <= 100; page += 1) {
      const checks = await github(`commits/${sha}/check-runs?per_page=100&page=${page}`);
      if (!Array.isArray(checks.check_runs) || !Number.isSafeInteger(checks.total_count))
        throw new Error('Exact-SHA check-run inventory is incomplete');
      checkRuns.push(...checks.check_runs);
      if (checkRuns.length >= checks.total_count) break;
      if (page === 100) throw new Error('Exact-SHA check-run inventory exceeded the pagination limit');
    }
    selectTrustedPrepareCheck(checkRuns, {candidateSha: sha, prepareRunId: process.env.PREPARE_RUN_ID});
    const staticRoute = proof.staticOnly === true;
    let deploymentImages = proof.images;
    let plannedManifest = null;
    if (staticRoute) {
      if (proof.route !== 'STATIC_UI_ONLY' || proof.staticArtifactRoute !== 'V4_FAST_STATIC')
        throw new Error('Only the attested V4_FAST_STATIC UI route is eligible');
      plannedManifest = fs.readdirSync(liveBaseline.manifestDir)
        .map(name => JSON.parse(fs.readFileSync(path.join(liveBaseline.manifestDir, name), 'utf8')))
        .find(item => item.candidateSha === sha && item.status === 'PLANNED' && item.route === 'STATIC_UI_ONLY');
      const inherited = plannedManifest?.componentImages;
      if (!inherited || !['app', 'agata', 'gsales'].every(role => /^sha256:[a-f0-9]{64}$/.test(inherited[role]?.imageId ?? '')))
        throw new Error('Static route has no verified inherited runtime image identity set');
      deploymentImages = Object.fromEntries(Object.entries(inherited).map(([role, image]) => [role, {digest: image.imageId}]));
    } else if (!deploymentImages || !['app', 'agata', 'gsales'].every(role => /^sha256:[a-f0-9]{64}$/.test(deploymentImages[role]?.digest ?? ''))) {
      throw new Error('Exact GHCR candidate image digest identities are unavailable');
    }
    const outcome = await executeProductionDeployment({
      createDeployment: async () => (await deploymentClient.beginProductionDeployment({candidateSha: sha,
        runId: process.env.GITHUB_RUN_ID, runAttempt: process.env.GITHUB_RUN_ATTEMPT,
        prepareRunId: process.env.PREPARE_RUN_ID, prepareRunAttempt: process.env.PREPARE_RUN_ATTEMPT,
        prepareReceiptDigest, prepareReceiptSha256,
        imageDigests: Object.fromEntries(Object.entries(deploymentImages).map(([role, image]) => [role, image.digest])),
        route: staticRoute ? 'STATIC_UI_ONLY' : 'FULL_RELEASE',
        ...(staticRoute ? {staticArtifactDigest: proof.staticArtifactDigest,
          staticArtifactContentSha256: proof.staticArtifactSha256} : {}),
        authorizationMode: mode === 'task-scoped-auto' ? 'TASK_SCOPED_AUTO' : 'EXACT_SHA',
        authorizationId: process.env.TASK_AUTHORIZATION_ID, logUrl: workflowUrl},
      {requiredContexts: [requiredContext]})).deployment,
      apply: () => spawnSync('pnpm', ['release:gpoint', '--apply', '--candidate', sha, '--confirm-production-apply'], {
        cwd: root, stdio: 'inherit', timeout: 1_800_000,
      }),
      inspectVps: () => stateFromLiveVps(factsFromReadOnlyInspection(inspectProductionReadOnly())),
      verifyInstalled: async installed => {
        if (staticRoute) {
          if (installed.currentProductionSha !== proof.productionBaselineSha || installed.miniappSourceSha !== sha ||
              installed.currentProductionDbHead !== liveBaseline.state.currentProductionDbHead ||
              installed.agataAlembicHead !== liveBaseline.state.agataAlembicHead ||
              installed.gsalesMigrationHead !== liveBaseline.state.gsalesMigrationHead ||
              installed.gsalesMigrationSha256 !== liveBaseline.state.gsalesMigrationSha256)
            throw new Error('Static UI readback changed the runtime or database baseline');
        } else if (installed.currentProductionSha !== sha || installed.miniappSourceSha !== sha ||
            installed.currentProductionDbHead !== proof.prismaHead || installed.agataAlembicHead !== proof.agataHead ||
            installed.gsalesMigrationHead !== proof.gsalesMigrationHead ||
            installed.gsalesMigrationSha256 !== proof.gsalesMigrationSha256) {
          throw new Error('Independent post-Apply VPS readback does not match the candidate source and migration identities');
        }
        const records = fs.readdirSync(liveBaseline.manifestDir).map(name => JSON.parse(fs.readFileSync(path.join(liveBaseline.manifestDir, name), 'utf8')));
        const completed = records.findLast(item => item.candidateSha === sha && item.status === 'COMPLETE');
        if (!completed || completed.productionShaAfter !== (staticRoute ? proof.productionBaselineSha : sha)
            || (staticRoute && completed.miniappShaAfter !== sha))
          throw new Error('Apply returned success without complete local install evidence');
      },
      afterVerified: async () => {
        if (mode !== 'task-scoped-auto') return;
        await consumeTaskAuthorization({project: 'gpoint', authorizationId: process.env.TASK_AUTHORIZATION_ID,
          candidateSha: sha, token: process.env.GITHUB_TOKEN});
        authorizationConsumed = true;
        process.env.RELEASE_AUTHORIZATION_CONSUMED = 'true';
      },
      publishStatus: (target, targetIdentity, status) => deploymentClient.publishStatus(target, targetIdentity, status),
      identity: {candidateSha: sha, runId: process.env.GITHUB_RUN_ID, runAttempt: process.env.GITHUB_RUN_ATTEMPT},
      candidateSha: sha, logUrl: workflowUrl,
    });
    deployment = outcome.deployment;
    applyStarted = outcome.applyStarted;
    installationVerified = outcome.installationVerified;
    finalSuccessStatusStarted = outcome.successStatusStarted;
    releaseCode = outcome.releaseCode;
    if (outcome.failure) {
      if (outcome.applyStarted && outcome.applyResult?.status !== 0) {
        executionFailure = outcome.live
          ? {stage: 'POST_APPLY_READBACK', executionStatus: 'FAILED',
            diagnostic: 'Apply failed; performed independent read-only VPS inspection before publishing error status',
            observedRuntimeSha: outcome.live.currentProductionSha,
            observedPrismaHead: outcome.live.currentProductionDbHead}
          : {stage: 'POST_APPLY_READBACK', executionStatus: 'BLOCKED',
            diagnostic: 'Apply failed and independent VPS inspection was unavailable'};
      } else {
        executionFailure = {...outcome.failure,
          diagnostic: sanitizeReleaseOutput(outcome.failure.diagnostic)};
      }
    }
  }
  }
} catch (error) {
  deployment ??= error?.deployment ?? null;
  releaseCode = 1;
  executionFailure = {stage: metadataReconciliationStarted ? 'DEPLOYMENT_RECONCILIATION' :
    (applyStarted ? 'POST_APPLY_READBACK_OR_STATUS' : 'PRE_APPLY'),
    executionStatus: 'FAILED', diagnostic: sanitizeReleaseOutput(error?.message)};
  if (!metadataReconciliationStarted && deployment && (!installationVerified || !finalSuccessStatusStarted)) {
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
  let evidence = reconciliationEvidence ?? attempt?.data ?? { candidateSha: sha, status: 'BLOCKED', productionRef: null };
  const status = deploymentResultStatus({evidenceStatus: evidence.status, mode, authorizationConsumed,
    deploymentExists: Boolean(deployment), installationVerified,
    successStatusConfirmed: releaseCode === 0 && finalSuccessStatusStarted && !executionFailure});
  fs.writeFileSync(process.env.RELEASE_RESULT, JSON.stringify({ ...withPreflightEvidence(evidence, preflightStatus, mode),
    mode,
    status,
    ...(executionFailure ? {executionFailure} : {}),
    releaseState: mode === 'preflight' && evidence.status === 'PLANNED' ? 'READY_FOR_OWNER' :
      ['production', 'task-scoped-auto'].includes(mode) && evidence.status === 'COMPLETE' ? status : evidence.status,
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
    deploymentId: deployment?.id ?? null,
    deployRunId: deployment ? deploymentIdentity(deployment).runId : process.env.GITHUB_RUN_ID,
    deployRunAttempt: deployment ? deploymentIdentity(deployment).runAttempt : process.env.GITHUB_RUN_ATTEMPT,
  }, null, 2) + '\n');
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT,
      `preflight=${preflightStatus}\nstatus=${status}\n`);
  }
  console.log(`GITHUB_DEPLOYMENT_ID=${deployment?.id ?? 'NOT_CREATED'} VPS_BASELINE_SHA=${liveBaseline.state.currentProductionSha}`);
  fs.rmSync(runDirectory, {recursive: true, force: true});
}
process.exitCode = releaseCode;
