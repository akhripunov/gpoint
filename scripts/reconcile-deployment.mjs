import fs from 'node:fs';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {deploymentIdentity} from './github-deployments.mjs';
import {factsFromReadOnlyInspection, stateFromLiveVps} from './deployment-run-state.mjs';
import {createGitHubDeploymentsClient} from './github-deployments.mjs';
import {AuthorizationError, authorizationRecordPath, authorizationStateRef, candidateScopeDiff, consumeTaskAuthorization,
  validateTaskAuthorization, checkTaskAuthorizationState} from './release-authorization.mjs';
import {validateOciReceipt} from './release-oci.mjs';
import {inspectProductionReadOnly} from './release-gpoint-vps-adapter.mjs';
import {acquireReleaseLock} from './release-lock.mjs';

const REPOSITORY = 'akhripunov/gpoint';
const API = 'https://api.github.com';
const SHA40 = /^[a-f0-9]{40}$/;
const MAX_DEPLOY_LOG_BYTES = 25 * 1024 * 1024;
const PRODUCTION_LOCK_FILE = path.join(os.homedir(), '.config/gpoint/release-production.lock');

export class ReconciliationGateError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ReconciliationGateError';
  }
}

function requireThat(condition, message) {
  if (!condition) throw new ReconciliationGateError(message);
}

export function safeReconciliationFailure(error) {
  if (error instanceof ReconciliationGateError) {
    const message = String(error.message).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, 240);
    return `GATE=${message}`;
  }
  if (error instanceof AuthorizationError) {
    const message = String(error.message);
    const http = /^Task authorization consumption failed \(HTTP ([345]\d\d)\)$/.exec(message);
    if (http) return `GATE=Task authorization consumption failed (HTTP ${http[1]})`;
    if (message === 'Task authorization consumption response was lost and exact marker is absent')
      return 'GATE=Task authorization consumption response lost with exact marker absent';
    return 'TYPE=AuthorizationError';
  }
  const name = /^[A-Za-z][A-Za-z0-9_]{0,40}$/.test(String(error?.name ?? '')) ? error.name : 'Error';
  return `TYPE=${name}`;
}

function strictUtf8(bytes, label) {
  try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
  catch { throw new Error(`${label} is not valid UTF-8`); }
}

async function readBoundedBody(response, limit) {
  const contentLength = response.headers?.get?.('content-length');
  if (contentLength && /^\d+$/.test(contentLength) && Number(contentLength) > limit)
    throw new Error('Original Deploy logs exceed limits');
  if (!response.body?.getReader) {
    const bytes = Buffer.from(await response.arrayBuffer());
    requireThat(bytes.length <= limit, 'Original Deploy logs exceed limits');
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      total += chunk.length;
      if (total > limit) {
        try { await reader.cancel(); } catch {}
        throw new Error('Original Deploy logs exceed limits');
      }
      chunks.push(chunk);
    }
  } catch (error) {
    try { await reader.cancel(); } catch {}
    throw error;
  }
  return Buffer.concat(chunks, total);
}

function readPublisherJobLogs(bytes, directory, run) {
  requireThat(bytes.length > 0 && bytes.length <= MAX_DEPLOY_LOG_BYTES,
    'Original Deploy logs exceed limits or are empty');
  const zipSignature = bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b
    && ((bytes[2] === 0x03 && bytes[3] === 0x04) || (bytes[2] === 0x05 && bytes[3] === 0x06)
      || (bytes[2] === 0x07 && bytes[3] === 0x08));
  if (bytes[0] === 0x50 && bytes[1] === 0x4b && !zipSignature)
    throw new Error('Original Deploy logs have an invalid ZIP signature');
  if (!zipSignature) return strictUtf8(bytes, 'Original Deploy logs');

  const zipPath = path.join(directory, 'job-logs.zip');
  fs.writeFileSync(zipPath, bytes, {flag: 'wx', mode: 0o600});
  const inventory = strictUtf8(run('unzip', ['-Z1', zipPath], {
    encoding: 'buffer', timeout: 30_000, maxBuffer: MAX_DEPLOY_LOG_BYTES,
    stdio: ['ignore', 'pipe', 'pipe'],
  }), 'Original Deploy ZIP inventory').trimEnd().split(/\r?\n/);
  requireThat(inventory.length > 0 && inventory.length <= 100
    && new Set(inventory).size === inventory.length
    && inventory.every(name => name && !name.startsWith('/') && !name.split('/').includes('..')),
  'Original Deploy log ZIP inventory is invalid');
  let totalBytes = 0;
  const files = inventory.map(name => {
    const fileBytes = run('unzip', ['-p', zipPath, name], {
      encoding: 'buffer', timeout: 30_000, maxBuffer: MAX_DEPLOY_LOG_BYTES,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    totalBytes += fileBytes.length;
    requireThat(totalBytes <= MAX_DEPLOY_LOG_BYTES, 'Original Deploy logs exceed limits');
    return strictUtf8(fileBytes, 'Original Deploy log file');
  });
  return files.join('\n');
}

function verifyLoggedDigest(logs, digest) {
  const key = 'DEPLOY_EVIDENCE_DIGEST=';
  const lines = logs.split(/\r?\n/).map(line => {
    const stamped = line.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)$/);
    if (!stamped) return line;
    requireThat(Number.isFinite(Date.parse(stamped[1])), 'Original Deploy log timestamp is invalid');
    return stamped[2];
  });
  const markers = lines.filter(line => line.trimStart().startsWith(key));
  requireThat(markers.length === 1 && markers[0] === `${key}${digest}`,
    'Exact Deploy evidence digest is absent, invalid, or ambiguous in its trusted publisher job logs');
}

export function reconciledResultEvidence(originalEvidence, verifiedFields) {
  const {executionFailure, ...evidence} = originalEvidence ?? {};
  return {...evidence, ...verifiedFields,
    ...(executionFailure ? {reconciledFrom: {status: originalEvidence.status,
      releaseState: originalEvidence.releaseState, executionFailure}} : {})};
}

async function apiJson(route, token, fetchImpl) {
  const response = await fetchImpl(`${API}/repos/${REPOSITORY}/${route}`, {headers: {
    Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'}, signal: AbortSignal.timeout(30_000)});
  if (!response.ok) throw new Error(`Reconciliation evidence GET failed (HTTP ${response.status})`);
  return response.json();
}

async function readBoundDeploymentArtifact({runId, attempt, candidateSha, token, fetchImpl = globalThis.fetch,
  publisherJob = 'deploy', publisherStep = 'Publish deployment result as a GHCR OCI artifact', requireCandidateHead = true,
  publisherConclusions = ['success', 'failure'],
  execFile = execFileSync, registryUsername = process.env.GHCR_READ_USERNAME,
  registryToken = process.env.GHCR_READ_TOKEN}) {
  requireThat(SHA40.test(candidateSha ?? ''), 'Exact candidate SHA is required to resolve Deploy evidence');
  const tag = `deploy-${candidateSha}-${runId}-${attempt}`;
  console.log('V5_PARENT_EVIDENCE_PHASE=read_workflow_run');
  const deployRun = await apiJson(`actions/runs/${runId}/attempts/${attempt}`, token, fetchImpl);
  requireThat(String(deployRun.id) === String(runId) && String(deployRun.run_attempt) === String(attempt)
    && SHA40.test(deployRun.head_sha ?? '') && (!requireCandidateHead || deployRun.head_sha === candidateSha) && deployRun.path === '.github/workflows/gpoint-deploy.yml'
    && deployRun.status === 'completed' && ['success', 'failure'].includes(deployRun.conclusion)
    && deployRun.event === 'workflow_dispatch' && deployRun.head_branch === 'main',
  'Exact Deploy Actions run attempt is not completed successfully on main');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-deploy-reconcile-'));
  try {
    requireThat(typeof registryUsername === 'string' && registryUsername.length > 0
      && typeof registryToken === 'string' && registryToken.length > 0,
    'Explicit GHCR read credentials are required for Deploy evidence');
    fs.chmodSync(directory, 0o700);
    const registryConfig = path.join(directory, 'registry-config.json');
    const registryAuth = Buffer.from(`${registryUsername}:${registryToken}`).toString('base64');
    fs.writeFileSync(registryConfig, JSON.stringify({auths: {'ghcr.io': {auth: registryAuth}}}),
      {flag: 'wx', mode: 0o600});
    fs.chmodSync(registryConfig, 0o600);
    const run = (file, args, options = {}) => execFile(file, args, options);
    const reference = `ghcr.io/akhripunov/gpoint-deploy-evidence:${tag}`;
  console.log('V5_PARENT_EVIDENCE_PHASE=resolve_evidence_oci');
    const digest = run('oras', ['resolve', '--registry-config', registryConfig, reference],
      {encoding: 'utf8', timeout: 30_000}).trim();
    requireThat(/^sha256:[a-f0-9]{64}$/.test(digest), 'Deploy evidence tag did not resolve to an immutable digest');
  console.log('V5_PARENT_EVIDENCE_PHASE=read_publisher_jobs');
    const jobs = await apiJson(`actions/runs/${runId}/attempts/${attempt}/jobs?per_page=100`, token, fetchImpl);
    const publishers = (jobs.jobs ?? []).filter(job => job.name === publisherJob
      && String(job.run_id) === String(runId) && job.head_sha === deployRun.head_sha
      && String(job.run_attempt) === String(attempt) && job.status === 'completed'
      && publisherConclusions.includes(job.conclusion)
      && (job.steps ?? []).some(step => step.name === publisherStep
        && step.status === 'completed' && step.conclusion === 'success'));
    requireThat(publishers.length === 1 && Number.isSafeInteger(publishers[0].id),
      'Exact original Deploy evidence publisher job is missing or ambiguous');
  console.log('V5_PARENT_EVIDENCE_PHASE=fetch_publisher_logs');
    const logsResponse = await fetchImpl(`${API}/repos/${REPOSITORY}/actions/jobs/${publishers[0].id}/logs`, {
      headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'}, signal: AbortSignal.timeout(60_000)});
    requireThat(logsResponse.ok, 'Original Deploy publisher logs are unavailable');
    const logBytes = await readBoundedBody(logsResponse, MAX_DEPLOY_LOG_BYTES);
    const publisherLogs = readPublisherJobLogs(logBytes, directory, run);
  console.log('V5_PARENT_EVIDENCE_PHASE=verify_publisher_logs');
    verifyLoggedDigest(publisherLogs, digest);
    const subject = `ghcr.io/akhripunov/gpoint-deploy-evidence@${digest}`;
    const directoryArg = path.join(directory, 'artifact');
    fs.mkdirSync(directoryArg, {mode: 0o700});
  console.log('V5_PARENT_EVIDENCE_PHASE=pull_evidence_oci');
    run('oras', ['pull', '--registry-config', registryConfig, subject, '-o', directoryArg],
      {encoding: 'utf8', timeout: 60_000});
    const names = fs.readdirSync(directoryArg);
    requireThat(names.length === 1 && names[0] === `gpoint-release-result-${runId}-${attempt}.json`,
      'Deploy evidence OCI artifact must contain the exact run result JSON');
    const bytes = fs.readFileSync(path.join(directoryArg, names[0]));
    requireThat(bytes.length <= 1024 * 1024, 'Deploy evidence exceeds its size limit');
    const contentSha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  console.log('V5_PARENT_EVIDENCE_PHASE=decode_evidence_json');
    const evidence = JSON.parse(bytes.toString('utf8'));
    return {evidence, deployRun, digest, contentSha256};
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
}

export function isAuthorizationConsumptionFailure(evidence) {
  return evidence?.status === 'INSTALLATION_UNCONFIRMED'
    && evidence.releaseState === 'INSTALLATION_UNCONFIRMED'
    && evidence.authorizationMode === 'TASK_SCOPED_AUTO' && evidence.authorizationConsumed === false
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED'
    && evidence.serverDeployStatus === 'COMPLETE'
    && evidence.executionFailure?.stage === 'POST_APPLY_READBACK'
    && evidence.executionFailure.executionStatus === 'FAILED'
    && evidence.executionFailure.diagnostic === 'Task authorization consumption failed (HTTP 403)';
}

// The canonical coordinator originally emitted an unversioned manifest. Resolve its
// observed per-service identities only from the hash-bound Prepare receipt and raw
// post-switch artifact proofs; never confuse an OCI config digest with a native ID.
function normalizeCanonicalDeployEvidence(evidence, candidateSha) {
  if (evidence?.schema === 1) return evidence;
  requireThat(evidence?.schema === undefined && evidence?.componentImages === undefined
    && evidence?.serverDeployStatus === 'COMPLETE'
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED'
    && evidence.productionShaAfter === candidateSha && evidence.agataRuntimeSha === candidateSha
    && evidence.miniappSourceSha === candidateSha
    && ['production', 'task-scoped-auto'].includes(evidence.mode),
  'Original Deploy evidence has an unsupported canonical result format');
  const ci = evidence.ciReceipt;
  requireThat(typeof ci?.prepareReceiptJson === 'string'
    && crypto.createHash('sha256').update(ci.prepareReceiptJson).digest('hex') === ci.prepareReceiptSha256,
  'Canonical original Prepare receipt content hash differs from its recorded identity');
  const receipt = JSON.parse(ci.prepareReceiptJson);
  validateOciReceipt(receipt, candidateSha, {runId: String(evidence.prepareRunId),
    runAttempt: String(evidence.prepareRunAttempt)});
  const groups = {app: ['api', 'worker', 'bot', 'miniapp'], agata: ['agataApi', 'agataWorker'],
    gsales: ['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker']};
  const componentImages = {};
  for (const [role, names] of Object.entries(groups)) {
    const image = receipt.images[role];
    const nativeId = receipt.deployRehearsal.images[role].nativeId;
    for (const name of names) {
      const observed = evidence.artifactIdentities?.[name];
      requireThat(observed?.sourceSha === candidateSha && observed.adapter?.buildSha === candidateSha
        && observed.adapter.imageId === nativeId,
      `Canonical original service identity differs from its rehearsed native image: ${name}`);
      if (role === 'agata') requireThat(observed.adapter.apiImage === nativeId
        && observed.adapter.workerImage === nativeId,
      'Canonical original AGATA API/worker image identity differs');
      if (role === 'gsales') requireThat(['agataImageId', 'coreApiImage', 'coreWorkerImage']
        .every(key => observed.adapter[key] === receipt.deployRehearsal.images.agata.nativeId),
      'Canonical original GSales dependency image differs from rehearsed AGATA');
    }
    componentImages[role] = {name: role, sourceSha: candidateSha, imageId: image.configDigest,
      registryDigest: image.digest, platform: image.platform};
  }
  return {...evidence, schema: 1, componentImages};
}

const METADATA_RECONCILIATION_OPERATION = 'AUTHORIZATION_CONSUMPTION_RECONCILIATION';
const DEPLOYMENT_STATUS_ID = status => JSON.stringify([status?.state, status?.description ?? null,
  status?.log_url ?? null, status?.environment_url ?? null]);
const git = (root, ...args) => execFileSync('git', args, {cwd: root, encoding: 'utf8', timeout: 20_000}).trim();

export function assertCandidateExecutionScope({candidateSha, executorSha, allowedPaths, changedPaths}) {
  requireThat(SHA40.test(candidateSha ?? '') && SHA40.test(executorSha ?? '') && candidateSha !== executorSha,
    'Candidate and trusted executor SHAs must be distinct exact commits');
  requireThat(Array.isArray(allowedPaths) && Array.isArray(changedPaths), 'Authorized path scope is unavailable');
  for (const name of changedPaths) {
    requireThat(typeof name === 'string' && name && !name.startsWith('/') && !name.split('/').includes('..'),
      'Candidate-to-executor diff contains an invalid path');
    requireThat(allowedPaths.some(scope => name === scope.replace(/\/$/, '')
      || (scope.endsWith('/') && name.startsWith(scope))),
    `Candidate-to-executor path is outside the original authorization scope: ${name}`);
  }
}

function assertOwnerDispatch(env) {
  requireThat(env.GITHUB_ACTIONS === 'true' && env.GITHUB_EVENT_NAME === 'workflow_dispatch'
    && env.GITHUB_REF === 'refs/heads/main' && env.GITHUB_REPOSITORY === REPOSITORY && env.OWNER_REPOSITORY === 'akhripunov'
    && env.OWNER_APPROVAL_ACTOR === env.OWNER_REPOSITORY
    && env.OWNER_TRIGGERING_ACTOR === env.OWNER_REPOSITORY,
  'Authorization reconciliation requires explicit repository-owner dispatch in the canonical repository');
  requireThat(typeof env.GITHUB_TOKEN === 'string' && env.GITHUB_TOKEN.length > 0,
    'GitHub token is required for authorization reconciliation');
  requireThat(/^[1-9]\d*$/.test(env.GITHUB_RUN_ID ?? '')
    && /^[1-9]\d*$/.test(env.GITHUB_RUN_ATTEMPT ?? ''),
  'Current reconciliation workflow run ID and attempt must be positive integers');
}

async function currentMainSha({token, fetchImpl}) {
  const ref = await apiJson('git/ref/heads/main', token, fetchImpl);
  requireThat(SHA40.test(ref?.object?.sha ?? ''), 'GitHub main ref SHA is unavailable');
  return ref.object.sha;
}

function assertTrustedCheckout({root, env, executorSha, candidateSha, record}) {
  requireThat(env.GITHUB_SHA === executorSha && git(root, 'rev-parse', 'HEAD') === executorSha,
    'Checkout does not match the exact trusted workflow executor SHA');
  requireThat(git(root, 'status', '--porcelain', '--untracked-files=all') === '',
    'Trusted workflow checkout is not clean');
  execFileSync('git', ['merge-base', '--is-ancestor', candidateSha, executorSha], {cwd: root, timeout: 20_000});
  const changed = git(root, 'diff', '--name-only', '-z', candidateSha, executorSha)
    .split('\0').filter(Boolean);
  if (Array.isArray(record?.allowed_paths)) assertCandidateExecutionScope({candidateSha, executorSha,
    allowedPaths: record.allowed_paths, changedPaths: changed});
  return changed;
}

async function readTaskAuthorizationMarkers({authorizationId, candidateSha, token, fetchImpl}) {
  const result = {};
  for (const name of ['revoked', 'consumed', 'claimed']) {
    const route = authorizationStateRef('gpoint', authorizationId, name).replace(/^refs\/tags\//, 'git/ref/tags/');
    const response = await fetchImpl(`${API}/repos/${REPOSITORY}/${route}`, {headers: {
      Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'}, signal: AbortSignal.timeout(30_000)});
    if (response.status === 404) { result[name] = false; continue; }
    requireThat(response.ok, `GitHub task authorization ${name} marker read failed (HTTP ${response.status})`);
    const ref = await response.json();
    requireThat(ref?.object?.type === 'commit' && SHA40.test(ref.object.sha ?? ''),
      `Task authorization ${name} marker is malformed`);
    result[name] = true;
    result[`${name}Sha`] = ref.object.sha;
  }
  requireThat(!result.revoked && result.claimed, 'Task authorization is revoked or has no active claim');
  requireThat(!result.consumed || result.consumedSha === candidateSha,
    'Task authorization was consumed for a different candidate');
  return result;
}

function readLatestDeploymentStatus(statuses) {
  requireThat(Array.isArray(statuses) && statuses.length > 0, 'Existing Deployment has no status history');
  requireThat(statuses.every(item => item?.created_at && Number.isFinite(Date.parse(item.created_at))),
    'Existing Deployment status history has invalid timestamps');
  const ordered = [...statuses].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  const at = Date.parse(ordered[0].created_at);
  requireThat(new Set(ordered.filter(item => Date.parse(item.created_at) === at).map(DEPLOYMENT_STATUS_ID)).size === 1,
    'Existing Deployment has conflicting latest statuses');
  return ordered[0];
}

async function assertNoConflictingProductionDeployments(client, deploymentId) {
  const records = await client.listDeployments();
  requireThat(Array.isArray(records), 'Production Deployment inventory is unavailable');
  const production = records.filter(item => item?.environment === 'production'
    && item?.production_environment === true && item?.transient_environment === false
    && item?.task === 'gpoint-production-deploy');
  const targets = production.filter(item => item.id === deploymentId);
  requireThat(targets.length === 1, 'Existing Deployment is missing or ambiguous in production history');
  for (const record of production) {
    requireThat(record.created_at && Number.isFinite(Date.parse(record.created_at)),
      'Production Deployment history has an invalid creation time');
  }
  const newest = [...production].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
  requireThat(newest[0]?.id === deploymentId && (newest.length < 2
    || Date.parse(newest[0].created_at) !== Date.parse(newest[1].created_at)),
  'Existing Deployment is not the unique latest production Deployment');
  for (const record of production) {
    if (record.id === deploymentId) continue;
    const status = readLatestDeploymentStatus(await client.listStatuses(record.id));
    requireThat(!['queued', 'pending', 'in_progress', 'error'].includes(status.state),
      `Other production Deployment ${record.id} has unresolved status ${status.state}`);
  }
}

function assertReconciliationStatus(status, identity, candidateSha, {allowAlreadyComplete = false} = {}) {
  requireThat(status?.log_url === identity.logUrl, 'Existing Deployment status does not link to its original run attempt');
  const originalFailure = status.state === 'error'
    && status.description === `stage=post_apply sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`;
  const alreadyComplete = allowAlreadyComplete && status.state === 'success'
    && new RegExp(`^stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt} reconciled=\\d+/\\d+$`).test(status.description ?? '');
  requireThat(originalFailure || alreadyComplete,
    'Existing Deployment is not in the exact post-Apply failure or exact reconciled-complete state');
  return alreadyComplete;
}

function validateHistoricalAuthorization({root, candidateSha, executorSha, evidence, authorizationId}) {
  if (evidence) requireThat(evidence.authorizationMode === 'TASK_SCOPED_AUTO'
    && evidence.authorizationId === authorizationId && evidence.authorizationConsumed === false,
  'Original Deploy evidence does not bind the unconsumed TASK_SCOPED_AUTO authorization');
  const relative = authorizationRecordPath(authorizationId);
  const atCandidate = execFileSync('git', ['show', `${candidateSha}:${relative}`], {cwd: root, encoding: 'buffer', timeout: 20_000});
  const atExecutor = execFileSync('git', ['show', `${executorSha}:${relative}`], {cwd: root, encoding: 'buffer', timeout: 20_000});
  requireThat(atCandidate.equals(atExecutor), 'Immutable task authorization record changed after the installed candidate');
  let record;
  try { record = JSON.parse(atCandidate.toString('utf8')); }
  catch { throw new Error('Original task authorization record is invalid JSON'); }
  const baseline = record.production_baseline_sha;
  if (evidence) requireThat(evidence.productionBaselineSha === baseline,
    'Original Deploy evidence has a different production baseline');
  const changedPaths = candidateScopeDiff(root, authorizationId, baseline, candidateSha);
  const proof = validateTaskAuthorization({record, project: 'gpoint', authorizationId,
    candidateSha, currentMainSha: candidateSha, productionSha: baseline, changedPaths});
  if (evidence) requireThat(evidence.authorizationRecordSha256 === proof.recordSha256
      && evidence.authorizationScopeSha256 === proof.ownerTaskCommitmentSha256
      && evidence.authorizationSourceInstructionId === proof.sourceInstructionIdentifier
      && evidence.authorizationChangedPaths === proof.changedPaths,
    'Original Deploy evidence does not match the immutable authorization scope record');
  const executionPaths = git(root, 'diff', '--name-only', '-z', candidateSha, executorSha).split('\0').filter(Boolean);
  assertCandidateExecutionScope({candidateSha, executorSha, allowedPaths: record.allowed_paths,
    changedPaths: executionPaths});
  return {record, proof, changedPaths, executionPaths};
}

function assertReceiptAndProductionEvidence({evidence, identity, deploymentId, candidateSha, prepareProof, receiptValidator = validateOciReceipt}) {
  requireThat(isAuthorizationConsumptionFailure(evidence) && Number(evidence.deploymentId) === deploymentId,
    'Original Deploy evidence does not prove the exact verified HTTP 403 authorization-consumption failure');
  const ci = evidence.ciReceipt;
  requireThat(ci?.ghcrManifestDigest === identity.prepareReceiptDigest
    && ci?.prepareReceiptSha256 === identity.prepareReceiptSha256
    && typeof ci?.prepareReceiptJson === 'string', 'Original Deploy lacks the embedded exact Prepare receipt');
  const receiptBytes = Buffer.from(ci.prepareReceiptJson, 'utf8');
  requireThat(crypto.createHash('sha256').update(receiptBytes).digest('hex') === identity.prepareReceiptSha256,
    'Embedded Prepare receipt content hash differs from the original Deployment');
  let receipt;
  try { receipt = JSON.parse(receiptBytes.toString('utf8')); }
  catch { throw new Error('Embedded Prepare receipt is invalid JSON'); }
  receiptValidator(receipt, candidateSha, {runId: identity.prepareRunId, runAttempt: identity.prepareRunAttempt});
  requireThat(receipt.prismaHead === evidence.dbHeadAfter
    && receipt.agataHead === evidence.agataDbHeadAfter
    && receipt.gsalesMigrationHead === evidence.gsalesDbHeadAfter
    && receipt.gsalesMigrationSha256 === (evidence.gsalesMigrationSha256After ?? evidence.gsalesRuntime?.migrationSha256)
    && (!prepareProof || (receipt.prismaHead === prepareProof.prismaHead
      && receipt.agataHead === prepareProof.agataHead && receipt.gsalesMigrationHead === prepareProof.gsalesMigrationHead
      && receipt.gsalesMigrationSha256 === prepareProof.gsalesMigrationSha256)),
  'Prepare receipt migration identities differ from the original Deploy evidence');
  requireThat(receipt.deployRehearsal?.status === 'PASS' && receipt.deployRehearsal?.dockerVersion === '29.1.3'
    && receipt.deployRehearsal?.store === 'containerd' && receipt.deployRehearsal?.snapshotter === 'overlayfs',
  'Prepare receipt lacks the exact private containerd deploy rehearsal');
  const imageRoles = ['app', 'agata', 'gsales'];
  requireThat(evidence.componentImages && imageRoles.every(role => evidence.componentImages[role]),
    'Original production component image identities are missing');
  for (const role of imageRoles) {
    const image = receipt.images?.[role];
    const rehearsed = receipt.deployRehearsal.images?.[role];
    const component = evidence.componentImages[role];
    requireThat(image && rehearsed && /^sha256:[a-f0-9]{64}$/.test(rehearsed.nativeId ?? '')
      && rehearsed.nativeId !== image.configDigest && rehearsed.repository === image.repository
      && rehearsed.digest === image.digest && rehearsed.configDigest === image.configDigest
      && rehearsed.sourceSha === candidateSha && component.sourceSha === candidateSha
      && component.imageId === image.configDigest && component.registryDigest === image.digest
      && component.platform === 'linux/amd64',
    `Receipt rehearsal and original production image identity differ for ${role}`);
  }
  return receipt;
}

function verifyLiveProduction(output, evidence, receipt, candidateSha) {
  const facts = factsFromReadOnlyInspection(output);
  const live = stateFromLiveVps(facts);
  // Component inventory imageId is the OCI config digest; Docker containerd reports nativeId.
  const expectedImages = receipt.deployRehearsal.images;
  requireThat(live.currentProductionSha === candidateSha && live.miniappSourceSha === candidateSha
    && live.currentProductionDbHead === evidence.dbHeadAfter
    && live.agataAlembicHead === evidence.agataDbHeadAfter
    && live.gsalesMigrationHead === evidence.gsalesDbHeadAfter
    && live.gsalesMigrationSha256 === (evidence.gsalesMigrationSha256After ?? evidence.gsalesRuntime?.migrationSha256),
  'Fresh VPS source or database identity differs from original Deploy evidence');
  const requiredSource = ['LIVE_RELEASE_ENV_SHA', 'LIVE_API_SHA', 'LIVE_WORKER_SHA', 'LIVE_BOT_SHA',
    'LIVE_MINIAPP_SHA', 'LIVE_AGATA_API_SHA', 'LIVE_AGATA_WORKER_SHA',
    'LIVE_GSALES_API_SHA', 'LIVE_GSALES_AGATA_API_SHA', 'LIVE_GSALES_AGATA_MEMORY_WORKER_SHA'];
  requireThat(requiredSource.every(key => facts[key] === candidateSha),
    'Fresh VPS service source identity differs from the installed candidate');
  const runningHealthy = ['LIVE_API_RUNNING', 'LIVE_WORKER_RUNNING', 'LIVE_BOT_RUNNING',
    'LIVE_AGATA_API_RUNNING', 'LIVE_AGATA_WORKER_RUNNING', 'LIVE_GSALES_API_RUNNING',
    'LIVE_GSALES_AGATA_API_RUNNING', 'LIVE_GSALES_AGATA_MEMORY_WORKER_RUNNING'];
  const healthy = ['LIVE_API_HEALTH', 'LIVE_AGATA_API_HEALTH', 'LIVE_AGATA_WORKER_HEALTH',
    'LIVE_GSALES_API_HEALTH', 'LIVE_GSALES_AGATA_API_HEALTH', 'LIVE_GSALES_AGATA_MEMORY_WORKER_HEALTH'];
  requireThat(runningHealthy.every(key => facts[key] === 'true') && healthy.every(key => facts[key] === 'healthy'),
    'Fresh VPS API, worker, bot, AGATA or GSales health is not verified');
  const imageChecks = [
    ['LIVE_API_IMAGE', expectedImages.app.nativeId], ['LIVE_WORKER_IMAGE', expectedImages.app.nativeId],
    ['LIVE_BOT_IMAGE', expectedImages.app.nativeId], ['LIVE_AGATA_API_IMAGE_ID', expectedImages.agata.nativeId],
    ['LIVE_AGATA_WORKER_IMAGE_ID', expectedImages.agata.nativeId],
    ['LIVE_GSALES_API_IMAGE_ID', expectedImages.gsales.nativeId],
    ['LIVE_GSALES_AGATA_API_IMAGE_ID', expectedImages.agata.nativeId],
    ['LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID', expectedImages.agata.nativeId],
  ];
  const mismatchedImage = imageChecks.find(([key, value]) => facts[key] !== value)?.[0];
  requireThat(!mismatchedImage, `Fresh VPS native image IDs differ at ${mismatchedImage}`);
  requireThat(receipt.deployRehearsal.images.app.nativeId !== receipt.deployRehearsal.images.agata.nativeId
    && receipt.deployRehearsal.images.app.nativeId !== receipt.deployRehearsal.images.gsales.nativeId
    && receipt.deployRehearsal.images.agata.nativeId !== receipt.deployRehearsal.images.gsales.nativeId,
  'Private containerd rehearsal native IDs are not role-distinct');
  const relevantKeys = [...requiredSource, ...runningHealthy, ...healthy, ...imageChecks.map(([key]) => key),
    'LIVE_PRISMA_HEAD', 'LIVE_AGATA_HEAD', 'LIVE_GSALES_HEAD', 'LIVE_GSALES_MIGRATION_SHA256'];
  return {live, fingerprint: Object.fromEntries(relevantKeys.map(key => [key, facts[key]]))};
}

async function verifyAuthorizationContext({client, deploymentId, candidateSha, token, env, root,
  fetchImpl = globalThis.fetch, readDeployEvidence = fetchOriginalDeployEvidence,
  verifyAuthorization = validateHistoricalAuthorization, readAuthorizationState = readTaskAuthorizationMarkers,
  currentMain = currentMainSha, assertCheckout = assertTrustedCheckout, allowAlreadyComplete = false}) {
  assertOwnerDispatch(env);
  requireThat(Number.isSafeInteger(deploymentId) && deploymentId > 0 && SHA40.test(candidateSha ?? ''),
    'Exact existing Deployment ID and candidate SHA are required');
  const executorSha = env.GITHUB_SHA;
  requireThat(SHA40.test(executorSha ?? ''), 'Trusted workflow executor SHA is invalid');
  assertCheckout({root, env, executorSha, candidateSha});
  const remoteMainSha = await currentMain({token, fetchImpl});
  requireThat(remoteMainSha === executorSha, 'Workflow checkout is not the exact current main SHA');

  const deployment = await client.getDeployment(deploymentId);
  const identity = deploymentIdentity(deployment);
  requireThat(deployment.id === deploymentId && identity.candidateSha === candidateSha
    && identity.route === 'FULL_RELEASE' && identity.authorizationMode === 'TASK_SCOPED_AUTO'
    && typeof identity.authorizationId === 'string' && identity.authorizationId.length > 0,
  'Existing Deployment is not the exact full-release TASK_SCOPED_AUTO candidate');
  requireThat(env.GITHUB_RUN_ID !== identity.runId,
    'Authorization reconciliation must use a distinct workflow run from the original Deploy');
  await assertNoConflictingProductionDeployments(client, deploymentId);
  const status = readLatestDeploymentStatus(await client.listStatuses(deploymentId));
  const alreadyComplete = assertReconciliationStatus(status, identity, candidateSha, {allowAlreadyComplete});

  let evidence = null;
  if (readDeployEvidence) {
    evidence = await readDeployEvidence({runId: identity.runId, attempt: identity.runAttempt,
      candidateSha, deploymentId, prepareRunId: identity.prepareRunId,
      prepareRunAttempt: identity.prepareRunAttempt, prepareReceiptDigest: identity.prepareReceiptDigest,
      prepareReceiptSha256: identity.prepareReceiptSha256, token, fetchImpl, allowAuthorizationFailure: true});
  }
  const authorizationProof = await verifyAuthorization({root, candidateSha, executorSha, evidence,
    authorizationId: identity.authorizationId, identity});
  const markerState = await readAuthorizationState({authorizationId: identity.authorizationId,
    candidateSha, token, fetchImpl});
  requireThat(markerState.claimed && !markerState.revoked
    && (!markerState.consumed || markerState.consumedSha === candidateSha),
  'Task authorization claim is revoked, absent, or consumed for another candidate');
  if (alreadyComplete) requireThat(markerState.consumed && markerState.consumedSha === candidateSha,
    'Already-complete Deployment has no exact consumed authorization marker');
  return {deployment, identity, status, alreadyComplete, evidence, authorizationProof,
    markerState, executorSha, remoteMainSha};
}

export async function validateAuthorizationConsumption({client, deploymentId, candidateSha,
  token = process.env.GITHUB_TOKEN, env = process.env, root = process.cwd(), fetchImpl = globalThis.fetch,
  verifyAuthorization = validateHistoricalAuthorization, readAuthorizationState = readTaskAuthorizationMarkers,
  currentMain = currentMainSha, assertCheckout = assertTrustedCheckout}) {
  return verifyAuthorizationContext({client, deploymentId, candidateSha, token, env, root, fetchImpl,
    readDeployEvidence: null, verifyAuthorization, readAuthorizationState, currentMain, assertCheckout,
    allowAlreadyComplete: true});
}

export async function finalizeAuthorizationConsumption({client, deploymentId, candidateSha,
  token = process.env.GITHUB_TOKEN, env = process.env, root = process.cwd(), fetchImpl = globalThis.fetch,
  inspectVps = inspectProductionReadOnly, readDeployEvidence = fetchOriginalDeployEvidence,
  verifyAuthorization = validateHistoricalAuthorization, readAuthorizationState = readTaskAuthorizationMarkers,
  consumeAuthorization = consumeTaskAuthorization, checkAuthorization = checkTaskAuthorizationState,
  currentMain = currentMainSha, execGit = git, receiptValidator = validateOciReceipt,
  assertCheckout = assertTrustedCheckout, writeResult = true, acquireProductionLock = acquireReleaseLock}) {
  assertOwnerDispatch(env);
  const releaseId = `gpoint-auth-reconciliation-${deploymentId}-${env.GITHUB_RUN_ID}-${env.GITHUB_RUN_ATTEMPT}`;
  const unlock = acquireProductionLock(PRODUCTION_LOCK_FILE, releaseId, path.join(root, '.gpoint-release.lock'));
  try {
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_original_identity');
  const context = await verifyAuthorizationContext({client, deploymentId, candidateSha, token, env, root,
    fetchImpl, readDeployEvidence, verifyAuthorization, readAuthorizationState, currentMain, assertCheckout,
    allowAlreadyComplete: true});
  const {deployment, identity, status: initialStatus, evidence, authorizationProof, executorSha} = context;
  requireThat(evidence && isAuthorizationConsumptionFailure(evidence),
    'Original Deploy evidence does not prove the exact failed authorization consumption');
  requireThat(evidence.authorizationId === identity.authorizationId
    && evidence.authorizationRecordSha256 === authorizationProof.proof.recordSha256
    && evidence.authorizationScopeSha256 === authorizationProof.proof.ownerTaskCommitmentSha256
    && evidence.authorizationSourceInstructionId === authorizationProof.proof.sourceInstructionIdentifier,
  'Original Deploy evidence does not match the verified immutable authorization');
  const prepareProof = {candidateSha, runId: identity.prepareRunId, runAttempt: identity.prepareRunAttempt,
    prismaHead: evidence.ciReceipt?.prismaHead, agataHead: evidence.ciReceipt?.agataHead,
    gsalesMigrationHead: evidence.ciReceipt?.gsalesMigrationHead,
    gsalesMigrationSha256: evidence.ciReceipt?.gsalesMigrationSha256};
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_receipt_and_images');
  const receipt = assertReceiptAndProductionEvidence({evidence, identity, deploymentId, candidateSha, prepareProof, receiptValidator});
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=inspect_original_vps');
  const factsBefore = inspectVps();
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_original_vps');
  const liveBefore = verifyLiveProduction(factsBefore, evidence, receipt, candidateSha);

  // Re-read every mutable identity immediately before the only permitted writes.
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_main');
  const latestMain = await currentMain({token, fetchImpl});
  requireThat(latestMain === executorSha, 'Current main changed before authorization reconciliation');
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_existing_deployment');
  const currentDeployment = await client.getDeployment(deploymentId);
  requireThat(currentDeployment.id === deployment.id
    && JSON.stringify(deploymentIdentity(currentDeployment)) === JSON.stringify(identity),
    'Existing Deployment identity changed before authorization reconciliation');
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_deployment_status');
  const currentStatus = readLatestDeploymentStatus(await client.listStatuses(deploymentId));
  requireThat(DEPLOYMENT_STATUS_ID(currentStatus) === DEPLOYMENT_STATUS_ID(initialStatus),
    'Existing Deployment status changed before authorization reconciliation');
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_deployment_inventory');
  await assertNoConflictingProductionDeployments(client, deploymentId);
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_authorization');
  const markers = await readAuthorizationState({authorizationId: identity.authorizationId,
    candidateSha, token, fetchImpl});
  requireThat(markers.claimed && !markers.revoked && (!markers.consumed || markers.consumedSha === candidateSha),
    'Authorization state changed before exact marker consumption');
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=consume_exact_authorization');
  if (!markers.consumed) {
    await consumeAuthorization({project: 'gpoint', authorizationId: identity.authorizationId,
      candidateSha, token, fetchImpl});
  }
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_consumption');
  await checkAuthorization({project: 'gpoint', authorizationId: identity.authorizationId,
    candidateSha, consumedCandidateSha: candidateSha, token, fetchImpl});
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=reread_consumption_marker');
  const readbackMarkers = await readAuthorizationState({authorizationId: identity.authorizationId,
    candidateSha, token, fetchImpl});
  requireThat(readbackMarkers.claimed && !readbackMarkers.revoked && readbackMarkers.consumed
    && readbackMarkers.consumedSha === candidateSha,
  'Exact task authorization consumption marker readback failed');

  console.log('AUTHORIZATION_RECONCILIATION_PHASE=inspect_final_vps');
  const factsAfter = inspectVps();
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_final_vps');
  const liveAfter = verifyLiveProduction(factsAfter, evidence, receipt, candidateSha);
  requireThat(JSON.stringify(liveAfter.fingerprint) === JSON.stringify(liveBefore.fingerprint),
    'VPS readback changed during authorization reconciliation');
  const runId = env.GITHUB_RUN_ID;
  const runAttempt = env.GITHUB_RUN_ATTEMPT;
  const description = `stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt} reconciled=${runId}/${runAttempt}`;
  requireThat(description.length <= 140, 'Authorization reconciliation status description exceeds GitHub limits');
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=publish_same_deployment');
  await client.publishStatus(deployment, identity, {state: 'success', description, logUrl: identity.logUrl});
  console.log('AUTHORIZATION_RECONCILIATION_PHASE=verify_same_deployment_status');
  const published = readLatestDeploymentStatus(await client.listStatuses(deploymentId));
  requireThat(published.state === 'success' && published.description === description
    && published.log_url === identity.logUrl,
  'Existing Deployment success status readback does not match the exact reconciliation attempt');

  const {executionFailure, ...retained} = evidence;
  delete retained.evidenceManifestDigest;
  delete retained.evidenceContentSha256;
  delete retained.deployRunConclusion;
  const result = {...retained, schema: 1, operation: METADATA_RECONCILIATION_OPERATION,
    status: 'COMPLETE', releaseState: 'COMPLETE', candidateSha, executorSha,
    deployRunId: identity.runId, deployRunAttempt: identity.runAttempt,
    automationRunId: String(runId), automationRunAttempt: String(runAttempt), deploymentId,
    prepareRunId: identity.prepareRunId, prepareRunAttempt: identity.prepareRunAttempt,
    authorizationMode: 'TASK_SCOPED_AUTO', authorizationId: identity.authorizationId,
    authorizationConsumed: true, applicationVerification: 'VPS_READBACK_VERIFIED',
    originalEvidence: {manifestDigest: evidence.evidenceManifestDigest,
      contentSha256: evidence.evidenceContentSha256, runId: identity.runId, runAttempt: identity.runAttempt},
    reconciledFrom: {status: evidence.status, releaseState: evidence.releaseState, executionFailure},
    reconciliation: {operation: METADATA_RECONCILIATION_OPERATION, originalDeploymentId: deploymentId,
      consumedCandidateSha: candidateSha, statusVerified: true, liveVerified: true}};
  if (writeResult) {
    const resultPath = env.RELEASE_RESULT;
    requireThat(typeof resultPath === 'string' && path.isAbsolute(resultPath), 'Private RELEASE_RESULT path is required');
    console.log('AUTHORIZATION_RECONCILIATION_PHASE=write_reconciliation_evidence');
    fs.writeFileSync(resultPath, `${JSON.stringify(result, null, 2)}\n`, {flag: 'wx', mode: 0o600});
    if (env.GITHUB_OUTPUT) fs.appendFileSync(env.GITHUB_OUTPUT, 'status=COMPLETE\n');
  }
  return result;
  } finally {
    unlock();
  }
}

export async function fetchOriginalDeployEvidence({runId, attempt, candidateSha, deploymentId,
  prepareRunId, prepareRunAttempt, prepareReceiptDigest, prepareReceiptSha256, token, fetchImpl = globalThis.fetch,
  execFile = execFileSync, registryUsername = process.env.GHCR_READ_USERNAME,
  registryToken = process.env.GHCR_READ_TOKEN, allowAuthorizationFailure = false}) {
  const {evidence: storedEvidence, deployRun, digest, contentSha256} = await readBoundDeploymentArtifact({
    runId, attempt, candidateSha, token, fetchImpl, execFile, registryUsername, registryToken});
  const evidence = normalizeCanonicalDeployEvidence(storedEvidence, candidateSha);
    const complete = evidence?.status === 'COMPLETE' && evidence.releaseState === 'COMPLETE'
      && evidence.applicationVerification === 'VPS_READBACK_VERIFIED';
    const statusUnconfirmed = evidence?.status === 'INSTALLATION_UNCONFIRMED'
      && evidence.releaseState === 'INSTALLATION_UNCONFIRMED'
      && evidence.executionFailure?.stage === 'STATUS_UNCONFIRMED'
      && evidence.applicationVerification === 'VPS_READBACK_VERIFIED';
    const authorizationFailure = allowAuthorizationFailure === true && isAuthorizationConsumptionFailure(evidence);
    requireThat(evidence?.schema === 1 && evidence.deployRunId === String(runId)
      && evidence.deployRunAttempt === String(attempt) && evidence.candidateSha === candidateSha
      && Number(evidence.deploymentId) === deploymentId
      && ((deployRun.conclusion === 'success' && complete) || (deployRun.conclusion === 'failure' && (statusUnconfirmed || authorizationFailure)))
      && (evidence.authorizationMode !== 'TASK_SCOPED_AUTO' || evidence.authorizationConsumed === true || authorizationFailure),
    'Deploy evidence payload does not bind its exact run and candidate');
    requireThat(String(evidence.prepareRunId) === String(prepareRunId)
      && String(evidence.prepareRunAttempt) === String(prepareRunAttempt)
      && evidence.ciReceipt?.ghcrManifestDigest === prepareReceiptDigest
      && (prepareReceiptSha256 === undefined || evidence.ciReceipt?.prepareReceiptSha256 === prepareReceiptSha256)
      && /^[a-f0-9]{64}$/.test(evidence.ciReceipt?.prepareReceiptSha256 ?? '')
      && typeof evidence.ciReceipt?.prepareReceiptJson === 'string',
    'Deploy evidence does not retain the exact verified Prepare receipt identity');
    const prepareRun = await apiJson(`actions/runs/${prepareRunId}/attempts/${prepareRunAttempt}`, token, fetchImpl);
    requireThat(String(prepareRun.id) === String(prepareRunId)
      && String(prepareRun.run_attempt) === String(prepareRunAttempt)
      && prepareRun.head_sha === candidateSha && prepareRun.path === '.github/workflows/gpoint-prepare.yml'
      && prepareRun.status === 'completed' && prepareRun.conclusion === 'success'
      && prepareRun.event === 'push' && prepareRun.head_branch === 'main',
    'Exact Prepare Actions run attempt is not completed successfully on main');
    return {...evidence, deployRunConclusion: deployRun.conclusion,
      evidenceManifestDigest: digest, evidenceContentSha256: contentSha256};
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort()
    .map(key => [key, canonicalJson(value[key])]));
  return value;
}

export async function fetchAuthorizationReconciliationEvidence({runId, attempt, candidateSha,
  deploymentId, originalRunId, originalRunAttempt, prepareRunId, prepareRunAttempt,
  prepareReceiptDigest, prepareReceiptSha256, token, fetchImpl = globalThis.fetch,
  execFile = execFileSync, registryUsername = process.env.GHCR_READ_USERNAME,
  registryToken = process.env.GHCR_READ_TOKEN, readArtifact = readBoundDeploymentArtifact,
  readOriginal = fetchOriginalDeployEvidence, receiptValidator = validateOciReceipt}) {
  requireThat(SHA40.test(candidateSha ?? '') && Number.isSafeInteger(deploymentId) && deploymentId > 0,
    'Exact candidate SHA and existing Deployment ID are required for reconciliation evidence');
  const originalRun = String(originalRunId), originalAttempt = String(originalRunAttempt);
  const original = await readOriginal({runId: originalRun, attempt: originalAttempt, candidateSha,
    deploymentId, prepareRunId, prepareRunAttempt, prepareReceiptDigest, prepareReceiptSha256,
    token, fetchImpl, execFile, registryUsername, registryToken, allowAuthorizationFailure: true});
  requireThat(original.deployRunConclusion === 'failure' && isAuthorizationConsumptionFailure(original),
    'Original Deploy artifact is not the exact verified authorization-consumption failure');
  const {evidence: attestation, deployRun: reconcileRun, digest, contentSha256} = await readArtifact({
    runId: String(runId), attempt: String(attempt), candidateSha, token, fetchImpl,
    publisherJob: 'reconcile-authorization',
    publisherStep: 'Publish reconciliation attestation as a GHCR OCI artifact',
    requireCandidateHead: false, publisherConclusions: ['success'], execFile, registryUsername, registryToken});
  const executorSha = reconcileRun.head_sha;
  requireThat(reconcileRun.conclusion === 'success' && SHA40.test(executorSha ?? '')
    && attestation.schema === 1 && attestation.operation === METADATA_RECONCILIATION_OPERATION
    && attestation.status === 'COMPLETE' && attestation.releaseState === 'COMPLETE'
    && attestation.candidateSha === candidateSha && attestation.executorSha === executorSha
    && String(attestation.automationRunId) === String(runId)
    && String(attestation.automationRunAttempt) === String(attempt)
    && String(attestation.deploymentId) === String(deploymentId)
    && String(attestation.deployRunId) === originalRun
    && String(attestation.deployRunAttempt) === originalAttempt,
  'Authorization reconciliation OCI artifact does not bind its exact successful workflow and Deployment');
  requireThat(String(attestation.prepareRunId) === String(prepareRunId)
    && String(attestation.prepareRunAttempt) === String(prepareRunAttempt)
    && attestation.authorizationMode === 'TASK_SCOPED_AUTO'
    && attestation.authorizationId === original.authorizationId
    && attestation.authorizationConsumed === true
    && attestation.applicationVerification === 'VPS_READBACK_VERIFIED',
  'Authorization reconciliation attestation changed the original Prepare or authorization identity');
  requireThat(JSON.stringify(canonicalJson(attestation.originalEvidence)) === JSON.stringify(canonicalJson({
    manifestDigest: original.evidenceManifestDigest, contentSha256: original.evidenceContentSha256,
    runId: originalRun, runAttempt: originalAttempt})),
  'Authorization reconciliation attestation is not bound to the immutable original OCI evidence');
  requireThat(JSON.stringify(canonicalJson(attestation.reconciledFrom)) === JSON.stringify(canonicalJson({
    status: original.status, releaseState: original.releaseState, executionFailure: original.executionFailure})),
  'Authorization reconciliation attestation does not preserve the original failure');
  requireThat(JSON.stringify(canonicalJson(attestation.reconciliation)) === JSON.stringify(canonicalJson({
    operation: METADATA_RECONCILIATION_OPERATION, originalDeploymentId: deploymentId,
    consumedCandidateSha: candidateSha, statusVerified: true, liveVerified: true})),
  'Authorization reconciliation attestation lacks exact status and live-readback proof');
  for (const key of ['ciReceipt', 'componentImages', 'dbHeadAfter', 'agataDbHeadAfter',
    'gsalesDbHeadAfter', 'gsalesMigrationSha256After', 'workerOperationalStatus',
    'serverDeployStatus', 'applicationVerification']) {
    requireThat(JSON.stringify(canonicalJson(attestation[key])) === JSON.stringify(canonicalJson(original[key])),
      `Authorization reconciliation changed original production evidence: ${key}`);
  }
  const receipt = assertReceiptAndProductionEvidence({evidence: original, identity: {
    prepareRunId: String(prepareRunId), prepareRunAttempt: String(prepareRunAttempt),
    prepareReceiptDigest, prepareReceiptSha256}, deploymentId, candidateSha});
  receiptValidator(receipt, candidateSha, {runId: String(prepareRunId), runAttempt: String(prepareRunAttempt)});
  return {...attestation, deployRunConclusion: 'failure', reconciliationRunConclusion: 'success',
    authorizationReconciliationVerified: true, evidenceManifestDigest: digest,
    evidenceContentSha256: contentSha256,
    originalEvidenceManifestDigest: original.evidenceManifestDigest,
    originalEvidenceContentSha256: original.evidenceContentSha256};
}

/**
 * Reconcile metadata for an existing Deployment only after its original Deploy
 * GHCR OCI evidence and a fresh read-only VPS inspection independently prove installation.
 * This function never creates a Deployment and has no Apply/migration path.
 */
export async function reconcileVerifiedDeployment({client, deploymentId, runId, currentRunAttempt,
  candidateSha, prepareRunId, prepareRunAttempt, prepareReceiptDigest, token,
  prepareProof, fetchImpl = globalThis.fetch, inspectVps, readDeployEvidence = fetchOriginalDeployEvidence,
  publish = true}) {
  requireThat(Number.isSafeInteger(deploymentId) && deploymentId > 0, 'Exact existing Deployment ID is required');
  requireThat(SHA40.test(candidateSha ?? ''), 'Exact candidate SHA is required');
  const deployment = await client.getDeployment(deploymentId);
  const identity = deploymentIdentity(deployment);
  requireThat(identity.candidateSha === candidateSha && identity.runId === String(runId),
    'Existing Deployment does not match the exact candidate and workflow run');
  requireThat(identity.runAttempt !== String(currentRunAttempt),
    'Reconciliation must target an earlier Deploy attempt, not the current attempt');
  requireThat(identity.prepareRunId === String(prepareRunId)
    && identity.prepareRunAttempt === String(prepareRunAttempt)
    && identity.prepareReceiptDigest === prepareReceiptDigest,
  'Existing Deployment does not match the exact original Prepare evidence');
  requireThat(prepareProof?.candidateSha === candidateSha
    && String(prepareProof.runId) === identity.prepareRunId
    && String(prepareProof.runAttempt) === identity.prepareRunAttempt
    && prepareProof.prismaHead && prepareProof.agataHead
    && prepareProof.gsalesMigrationHead && /^[a-f0-9]{64}$/.test(prepareProof.gsalesMigrationSha256 ?? ''),
  'Current immutable Prepare receipt does not prove the original candidate migration identities');

  const prepareRun = await apiJson(`actions/runs/${identity.prepareRunId}/attempts/${identity.prepareRunAttempt}`, token, fetchImpl);
  requireThat(prepareRun.head_sha === candidateSha && prepareRun.path === '.github/workflows/gpoint-prepare.yml'
    && prepareRun.status === 'completed' && prepareRun.conclusion === 'success',
  'Original Prepare run attempt is not a successful exact-SHA Prepare');

  const deployRun = await apiJson(`actions/runs/${runId}/attempts/${identity.runAttempt}`, token, fetchImpl);
  requireThat(String(deployRun.id) === String(runId) && String(deployRun.run_attempt) === identity.runAttempt
    && deployRun.head_sha === candidateSha && deployRun.path === '.github/workflows/gpoint-deploy.yml'
    && deployRun.status === 'completed' && ['success', 'failure'].includes(deployRun.conclusion),
  'Original Deploy is not a completed exact-SHA Deploy attempt');

  const evidence = await readDeployEvidence({runId: identity.runId, attempt: identity.runAttempt,
    candidateSha, deploymentId: deployment.id, prepareRunId: identity.prepareRunId,
    prepareRunAttempt: identity.prepareRunAttempt, prepareReceiptDigest, token, fetchImpl});
  const completeEvidence = evidence?.status === 'COMPLETE' && evidence.releaseState === 'COMPLETE';
  const statusUnconfirmedEvidence = evidence?.status === 'INSTALLATION_UNCONFIRMED'
    && evidence.releaseState === 'INSTALLATION_UNCONFIRMED'
    && evidence.executionFailure?.stage === 'STATUS_UNCONFIRMED'
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED';
  requireThat(evidence?.candidateSha === candidateSha && Number(evidence.deploymentId) === deployment.id
    && String(evidence.deployRunId) === identity.runId
    && String(evidence.deployRunAttempt) === identity.runAttempt
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED'
    && ((completeEvidence && deployRun.conclusion === 'success') ||
      (statusUnconfirmedEvidence && deployRun.conclusion === 'failure')),
  'Original Deploy result does not prove a verified COMPLETE installation');
  if (identity.authorizationMode === 'TASK_SCOPED_AUTO')
    requireThat(evidence.authorizationConsumed === true, 'TASK_SCOPED_AUTO authorization was not consumed by the original Deploy');

  const statuses = await client.listStatuses(deployment.id);
  requireThat(statuses.length > 0, 'Original Deployment has no status history');
  requireThat(statuses.every(item => item?.created_at && Number.isFinite(Date.parse(item.created_at))),
    'Original Deployment status history has an invalid timestamp');
  statuses.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  const latestTime = Date.parse(statuses[0].created_at);
  const tiedLatest = statuses.filter(item => Date.parse(item.created_at) === latestTime);
  const statusSignature = item => JSON.stringify([item.state, item.description ?? null,
    item.log_url ?? null, item.environment_url ?? null]);
  requireThat(new Set(tiedLatest.map(statusSignature)).size === 1,
    'Original Deployment has conflicting statuses at the latest timestamp');
  const current = statuses[0];
  requireThat(current.log_url === identity.logUrl,
    'Original Deployment status does not link to its exact Deploy run attempt');
  const alreadyComplete = current.state === 'success'
    && current.description === `stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`;
  requireThat(alreadyComplete || (['in_progress', 'error'].includes(current.state)
    && /stage=(?:pre_apply|post_apply)/.test(current.description ?? '')),
  'Original Deployment does not have a reconcilable nonterminal status');
  requireThat(typeof inspectVps === 'function', 'Fresh VPS inspection callback is required');
  const live = stateFromLiveVps(factsFromReadOnlyInspection(inspectVps()));
  if (identity.route === 'STATIC_UI_ONLY') {
    requireThat(prepareProof.route === 'STATIC_UI_ONLY' && evidence.route === 'STATIC_UI_ONLY'
      && live.currentProductionSha === prepareProof.productionBaselineSha
      && live.miniappSourceSha === candidateSha
      && live.currentProductionDbHead === evidence.dbHeadAfter
      && live.agataAlembicHead === evidence.agataDbHeadAfter
      && live.gsalesMigrationHead === evidence.gsalesDbHeadAfter
      && live.gsalesMigrationSha256 === (evidence.gsalesMigrationSha256 ?? evidence.gsalesRuntime?.migrationSha256),
    'Fresh static UI read-back changed runtime or database baseline');
  } else {
    requireThat(live.currentProductionSha === candidateSha && live.miniappSourceSha === candidateSha
      && live.currentProductionDbHead === evidence.dbHeadAfter
      && live.agataAlembicHead === evidence.agataDbHeadAfter
      && live.gsalesMigrationHead === evidence.gsalesDbHeadAfter
      && live.currentProductionDbHead === prepareProof.prismaHead
      && live.agataAlembicHead === prepareProof.agataHead
      && live.gsalesMigrationHead === prepareProof.gsalesMigrationHead
      && live.gsalesMigrationSha256 === prepareProof.gsalesMigrationSha256
      && live.gsalesMigrationSha256 === (evidence.gsalesMigrationSha256 ?? evidence.gsalesRuntime?.migrationSha256),
    'Fresh VPS source or migration read-back does not match original Deploy evidence');
  }

  if (alreadyComplete || !publish) return {deploymentId: deployment.id, identity, evidenceVerified: true, liveVerified: true,
    published: false, originalEvidence: evidence};
  const status = await client.publishStatus(deployment, identity, {state: 'success',
    description: `stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`,
    logUrl: identity.logUrl});
  return {deploymentId: deployment.id, identity, evidenceVerified: true, liveVerified: true,
    published: true, status, originalEvidence: evidence};
}

async function authorizationCli(argv) {
  requireThat(argv.length === 5 && ['--validate-authorization', '--finalize-authorization'].includes(argv[0])
    && argv[1] === '--deployment-id' && argv[3] === '--candidate',
  'Expected --validate-authorization|--finalize-authorization --deployment-id <id> --candidate <sha>');
  const [mode, , rawId, , candidateSha] = argv;
  requireThat(/^[1-9]\d*$/.test(rawId) && Number.isSafeInteger(Number(rawId)), 'Existing Deployment ID is invalid');
  const deploymentId = Number(rawId);
  const client = createGitHubDeploymentsClient({token: process.env.GITHUB_TOKEN});
  if (mode === '--validate-authorization') {
    const context = await validateAuthorizationConsumption({client, deploymentId, candidateSha});
    const outputs = `candidate=${candidateSha}\nrun_id=${context.identity.prepareRunId}\nrun_attempt=${context.identity.prepareRunAttempt}\n` +
      `task_authorization_id=${context.identity.authorizationId}\noriginal_run_id=${context.identity.runId}\n` +
      `original_run_attempt=${context.identity.runAttempt}\n`;
    if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, outputs);
    console.log(`AUTHORIZATION_RECONCILIATION_VALIDATION=PASS DEPLOYMENT_ID=${deploymentId}`);
    return;
  }
  try {
    await finalizeAuthorizationConsumption({client, deploymentId, candidateSha});
    console.log(`AUTHORIZATION_RECONCILIATION=COMPLETE DEPLOYMENT_ID=${deploymentId}`);
  } catch (error) {
    const resultPath = process.env.RELEASE_RESULT;
    if (typeof resultPath === 'string' && path.isAbsolute(resultPath)) {
      try {
        fs.writeFileSync(resultPath, `${JSON.stringify({schema: 1,
          operation: METADATA_RECONCILIATION_OPERATION, candidateSha,
          executorSha: process.env.GITHUB_SHA ?? null,
          status: 'INSTALLATION_UNCONFIRMED', releaseState: 'INSTALLATION_UNCONFIRMED',
          executionFailure: {stage: 'AUTHORIZATION_RECONCILIATION', executionStatus: 'BLOCKED',
            diagnostic: 'Metadata-only finalization stopped before every verification gate passed'}}, null, 2)}\n`,
        {flag: 'wx', mode: 0o600});
      } catch {}
    }
    if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, 'status=INSTALLATION_UNCONFIRMED\n');
    console.error(`AUTHORIZATION_RECONCILIATION=BLOCKED ${safeReconciliationFailure(error)}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  authorizationCli(process.argv.slice(2)).catch(error => {
    console.error(`AUTHORIZATION_RECONCILIATION=BLOCKED ${safeReconciliationFailure(error)}`);
    process.exitCode = 1;
  });
}
