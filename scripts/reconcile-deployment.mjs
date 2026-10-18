import fs from 'node:fs';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {deploymentIdentity} from './github-deployments.mjs';
import {factsFromReadOnlyInspection, stateFromLiveVps} from './deployment-run-state.mjs';

const REPOSITORY = 'akhripunov/gpoint';
const API = 'https://api.github.com';
const SHA40 = /^[a-f0-9]{40}$/;

function requireThat(condition, message) {
  if (!condition) throw new Error(message);
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

export async function fetchOriginalDeployEvidence({runId, attempt, token, fetchImpl = globalThis.fetch,
  execFile = execFileSync}) {
  const tag = `deploy-${process.env.CANDIDATE_SHA}-${runId}-${attempt}`;
  requireThat(SHA40.test(process.env.CANDIDATE_SHA ?? ''), 'Exact candidate SHA is required to resolve Deploy evidence');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-deploy-reconcile-'));
  try {
    const reference = `ghcr.io/akhripunov/gpoint-deploy-evidence:${tag}`;
    const digest = execFile(['oras', 'resolve', reference], {encoding: 'utf8', timeout: 30_000}).trim();
    requireThat(/^sha256:[a-f0-9]{64}$/.test(digest), 'Deploy evidence tag did not resolve to an immutable digest');
    const jobs = await apiJson(`actions/runs/${runId}/attempts/${attempt}/jobs?per_page=100`, token, fetchImpl);
    const publishers = (jobs.jobs ?? []).filter(job => job.name === 'deploy'
      && String(job.run_attempt) === String(attempt) && job.status === 'completed'
      && ['success', 'failure'].includes(job.conclusion)
      && (job.steps ?? []).some(step => step.name === 'Publish deployment result as a GHCR OCI artifact'
        && step.status === 'completed' && step.conclusion === 'success'));
    requireThat(publishers.length === 1 && Number.isSafeInteger(publishers[0].id),
      'Exact original Deploy evidence publisher job is missing or ambiguous');
    const logsResponse = await fetchImpl(`${API}/repos/${REPOSITORY}/actions/jobs/${publishers[0].id}/logs`, {
      headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'}, signal: AbortSignal.timeout(60_000)});
    requireThat(logsResponse.ok, 'Original Deploy publisher logs are unavailable');
    const logBytes = Buffer.from(await logsResponse.arrayBuffer());
    requireThat(logBytes.length > 0 && logBytes.length <= 25 * 1024 * 1024, 'Original Deploy logs exceed limits');
    const logZip = path.join(directory, 'job-logs.zip');
    fs.writeFileSync(logZip, logBytes, {flag: 'wx', mode: 0o600});
    const logFiles = execFile(['unzip', '-Z1', logZip], {encoding: 'utf8'}).trim().split(/\r?\n/);
    requireThat(logFiles.length > 0 && logFiles.length <= 100
      && logFiles.every(name => name && !name.startsWith('/') && !name.split('/').includes('..')),
    'Original Deploy job log inventory is invalid');
    const publisherLogs = logFiles.map(name => execFile(['unzip', '-p', logZip, name], {
      encoding: 'utf8', maxBuffer: 25 * 1024 * 1024})).join('\n');
    requireThat(new RegExp(`(?m)^DEPLOY_EVIDENCE_DIGEST=${digest.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`).test(publisherLogs),
      'Exact Deploy evidence digest is absent from its trusted publisher job logs');
    const subject = `ghcr.io/akhripunov/gpoint-deploy-evidence@${digest}`;
    const directoryArg = path.join(directory, 'artifact');
    fs.mkdirSync(directoryArg, {mode: 0o700});
    execFile(['oras', 'pull', subject, '-o', directoryArg], {encoding: 'utf8', timeout: 60_000});
    const names = fs.readdirSync(directoryArg);
    requireThat(names.length === 1 && names[0] === `gpoint-release-result-${runId}-${attempt}.json`,
      'Deploy evidence OCI artifact must contain the exact run result JSON');
    const bytes = fs.readFileSync(path.join(directoryArg, names[0]));
    requireThat(bytes.length <= 1024 * 1024, 'Deploy evidence exceeds its size limit');
    const contentSha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    const evidence = JSON.parse(bytes.toString('utf8'));
    requireThat(evidence?.schema === 1 && evidence.deployRunId === String(runId)
      && evidence.deployRunAttempt === String(attempt) && evidence.candidateSha === process.env.CANDIDATE_SHA,
    'Deploy evidence payload does not bind its exact run and candidate');
    return {...evidence, evidenceManifestDigest: digest, evidenceContentSha256: contentSha256};
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
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
    && deployRun.head_sha === candidateSha && deployRun.path === '.github/workflows/gpoint-deploy.yml',
  'Original Deploy run attempt identity is unavailable or mismatched');

  const evidence = await readDeployEvidence({runId: identity.runId, attempt: identity.runAttempt, token, fetchImpl});
  const completeEvidence = evidence?.status === 'COMPLETE' && evidence.releaseState === 'COMPLETE';
  const statusUnconfirmedEvidence = evidence?.status === 'INSTALLATION_UNCONFIRMED'
    && evidence.releaseState === 'INSTALLATION_UNCONFIRMED'
    && evidence.executionFailure?.stage === 'STATUS_UNCONFIRMED'
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED';
  requireThat(evidence?.candidateSha === candidateSha && Number(evidence.deploymentId) === deployment.id
    && String(evidence.deployRunId) === identity.runId
    && String(evidence.deployRunAttempt) === identity.runAttempt
    && evidence.applicationVerification === 'VPS_READBACK_VERIFIED'
    && (completeEvidence || statusUnconfirmedEvidence),
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
