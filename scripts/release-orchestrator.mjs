#!/usr/bin/env node
/** Trusted Prepare-completion controller for GPoint's existing Deploy workflow. */
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {join, resolve} from 'node:path';
import {authorizationRecordPath, candidateScopeDiff, validateTaskAuthorization} from './release-authorization.mjs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';

export const REPOSITORY = 'akhripunov/gpoint';
export const PREPARE_PATH = '.github/workflows/gpoint-prepare.yml';
export const DEPLOY_PATH = '.github/workflows/gpoint-deploy.yml';
const TASK_AUTH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/;
const SHA = /^[a-f0-9]{40}$/;
const DIAGNOSTIC_STAGES = new Set(['event_payload', 'prepare_event_validation', 'current_main_ref',
  'prepare_run_metadata', 'prepare_attempt_metadata', 'prepare_attempt_jobs', 'prepare_job_identity',
  'prepare_job_chronology', 'prepare_receipt_readback', 'deploy_inventory', 'authorization_lookup',
  'authorization_state_read', 'deploy_inventory_before_claim', 'atomic_claim_create',
  'atomic_claim_readback', 'deploy_workflow_dispatch', 'controller_result_summary']);
const DIAGNOSTIC_RESOURCES = new Set(['prepare_run', 'prepare_receipt', 'deploy_workflow_runs',
  'ghcr_prepare_receipt', 'main_ref', 'prepare_evidence', 'ghcr_static_artifact',
  'authorization_state_ref', 'task_authorization_claim', 'deploy_workflow', 'controller_step_summary']);
const SAFE_RECEIPT_STAGES = new Set(['prepare_run_metadata', 'current_main_ref', 'ghcr_receipt',
  'prepare_publisher_logs', 'static_artifact_readback', 'receipt_validation',
  'ghcr_receipt_tag_manifest', 'ghcr_receipt_digest_readback', 'ghcr_receipt_layer_readback',
  'ghcr_receipt_content_validation', 'ghcr_receipt_tag_manifest_auth_challenge',
  'ghcr_receipt_tag_manifest_auth_token', 'ghcr_receipt_tag_manifest_authenticated_read',
  'ghcr_receipt_digest_readback_auth_challenge', 'ghcr_receipt_digest_readback_auth_token',
  'ghcr_receipt_digest_readback_authenticated_read', 'ghcr_receipt_layer_readback_auth_challenge',
  'ghcr_receipt_layer_readback_auth_token', 'ghcr_receipt_layer_readback_authenticated_read',
  'ghcr_artifact_digest', 'ghcr_artifact_digest_auth_challenge', 'ghcr_artifact_digest_auth_token',
  'ghcr_artifact_digest_authenticated_read', 'ghcr_artifact_blob', 'ghcr_artifact_blob_auth_challenge',
  'ghcr_artifact_blob_auth_token', 'ghcr_artifact_blob_authenticated_read']);
const SAFE_RECEIPT_CATEGORIES = new Set(['AUTH', 'PERMISSION', 'NETWORK', 'HTTP', 'INVALID_RESPONSE', 'LOCAL_IO', 'VALIDATION', 'INTERNAL']);
const SAFE_SUBPROCESS_ERROR_CODES = new Set(['EACCES', 'EAGAIN', 'EBADF', 'EBUSY', 'EEXIST', 'EFBIG', 'EIO', 'EISDIR',
  'EMFILE', 'ENFILE', 'ENOENT', 'ENOMEM', 'ENOBUFS', 'ENOSPC', 'ENOTDIR', 'EPERM', 'EROFS', 'ETIMEDOUT', 'EPIPE', 'EINVAL']);
const SAFE_SUBPROCESS_SIGNALS = new Set(['SIGABRT', 'SIGALRM', 'SIGBUS', 'SIGFPE', 'SIGHUP', 'SIGILL', 'SIGINT',
  'SIGKILL', 'SIGPIPE', 'SIGQUIT', 'SIGSEGV', 'SIGTERM', 'SIGUSR1', 'SIGUSR2']);
const SAFE_PYTHON_ERROR_TYPES = new Set(['AssertionError', 'AttributeError', 'ConnectionError', 'FileNotFoundError',
  'ImportError', 'IndentationError', 'KeyError', 'ModuleNotFoundError', 'NameError', 'OSError', 'PermissionError',
  'RuntimeError', 'SyntaxError', 'SystemExit', 'TimeoutError', 'TypeError', 'UnicodeDecodeError', 'ValueError']);

export class OrchestrationError extends Error {
  constructor(message) { super(message); this.name = 'OrchestrationError'; }
}

export function controllerFailureDiagnostic(error, {stage = 'event_payload', resource = '', candidateSha = '',
  prepareRunId = '', prepareRunAttempt = ''} = {}) {
  const nested = error?.controllerDiagnostic ?? {};
  const safeStage = DIAGNOSTIC_STAGES.has(nested.stage) ? nested.stage :
    DIAGNOSTIC_STAGES.has(stage) ? stage : 'event_payload';
  const safeResource = DIAGNOSTIC_RESOURCES.has(nested.resource) ? nested.resource :
    DIAGNOSTIC_RESOURCES.has(resource) ? resource : '';
  const status = Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599
    ? error.status : Number.isInteger(nested.httpStatus) && nested.httpStatus >= 400 && nested.httpStatus <= 599
      ? nested.httpStatus : undefined;
  const code = ['GHCR_READ_FAILED', 'PREPARE_EVIDENCE_FAILED', 'PREPARE_RECEIPT_INVALID',
    'LOCAL_SUBPROCESS_FAILED', 'SUMMARY_WRITE_FAILED'].includes(nested.code) ? nested.code :
    status ? 'GITHUB_HTTP_ERROR' : error instanceof OrchestrationError ? 'ORCHESTRATION_BLOCKED' : 'UNEXPECTED_ERROR';
  const result = {state: 'BLOCKED', stage: safeStage, code};
  if (status) result.httpStatus = status;
  if (safeResource) result.resource = safeResource;
  if (SAFE_RECEIPT_STAGES.has(nested.substage)) result.substage = nested.substage;
  if (typeof nested.errorType === 'string' && /^[A-Za-z_][A-Za-z0-9_]{0,47}$/.test(nested.errorType)) result.errorType = nested.errorType;
  if (SAFE_RECEIPT_CATEGORIES.has(nested.category)) result.category = nested.category;
  if (typeof nested.ghcrReadbackCompleted === 'boolean') result.ghcrReadbackCompleted = nested.ghcrReadbackCompleted;
  if (Number.isInteger(nested.subprocessStatus) && nested.subprocessStatus >= 0 && nested.subprocessStatus <= 255)
    result.subprocessStatus = nested.subprocessStatus;
  if (SAFE_SUBPROCESS_ERROR_CODES.has(nested.subprocessErrorCode)) result.subprocessErrorCode = nested.subprocessErrorCode;
  if (SAFE_SUBPROCESS_SIGNALS.has(nested.subprocessSignal)) result.subprocessSignal = nested.subprocessSignal;
  if (SAFE_PYTHON_ERROR_TYPES.has(nested.subprocessStderrErrorType))
    result.subprocessStderrErrorType = nested.subprocessStderrErrorType;
  if (['missing_diagnostic', 'malformed_diagnostic', 'invalid_schema', 'unsafe_stage',
    'invalid_completion_flag', 'identity_mismatch'].includes(nested.diagnosticRejection))
    result.diagnosticRejection = nested.diagnosticRejection;
  if (SHA.test(candidateSha)) result.candidateSha = candidateSha;
  if (/^\d{1,20}$/.test(prepareRunId)) result.prepareRunId = prepareRunId;
  if (/^\d{1,6}$/.test(prepareRunAttempt)) result.prepareRunAttempt = prepareRunAttempt;
  if (nested.deployDispatched === true) result.deployDispatched = true;
  return result;
}

function localSubprocessFailure(expected, subprocess, diagnosticRejection) {
  const result = {stage:'prepare_receipt_readback',resource:'prepare_receipt',code:'LOCAL_SUBPROCESS_FAILED'};
  if (diagnosticRejection) result.diagnosticRejection = diagnosticRejection;
  if (SHA.test(expected.candidateSha ?? '')) result.candidateSha = expected.candidateSha;
  if (/^\d{1,20}$/.test(String(expected.runId ?? ''))) result.runId = String(expected.runId);
  if (/^\d{1,6}$/.test(String(expected.runAttempt ?? ''))) result.runAttempt = String(expected.runAttempt);
  if (Number.isInteger(subprocess?.status) && subprocess.status >= 0 && subprocess.status <= 255)
    result.subprocessStatus = subprocess.status;
  if (SAFE_SUBPROCESS_SIGNALS.has(subprocess?.signal)) result.subprocessSignal = subprocess.signal;
  if (SAFE_SUBPROCESS_ERROR_CODES.has(subprocess?.error?.code)) result.subprocessErrorCode = subprocess.error.code;
  const stderrType = String(subprocess?.stderr ?? '').match(/(?:^|\n)([A-Za-z][A-Za-z0-9_]{0,47}):(?:\s|$)/)?.[1];
  if (SAFE_PYTHON_ERROR_TYPES.has(stderrType)) result.subprocessStderrErrorType = stderrType;
  return result;
}

export function preparedReceiptFailureDiagnostic(stdout, expected = {}, subprocess = {}) {
  const structured = String(stdout ?? '').split(/\r?\n/).map(line =>
    line.match(/^ARTIFACT_CACHE_DIAGNOSTIC=(\{.{1,4096}\})$/)?.[1]).find(Boolean);
  if (structured) {
    let details;
    try {
      details = JSON.parse(structured);
    } catch { return localSubprocessFailure(expected, subprocess, 'malformed_diagnostic'); }
    if (details.schema !== 1) return localSubprocessFailure(expected, subprocess, 'invalid_schema');
    if (!SAFE_RECEIPT_STAGES.has(details.stage)) return localSubprocessFailure(expected, subprocess, 'unsafe_stage');
    if (typeof details.ghcrReadbackCompleted !== 'boolean')
      return localSubprocessFailure(expected, subprocess, 'invalid_completion_flag');
    const identities = [['candidateSha', 'candidateSha', SHA], ['runId', 'runId', /^\d{1,20}$/],
      ['runAttempt', 'runAttempt', /^\d{1,6}$/]];
    for (const [field, expectedField, pattern] of identities) {
      if (typeof details[field] !== 'string' || !pattern.test(details[field])
          || (expected[expectedField] !== undefined && details[field] !== expected[expectedField]))
        return localSubprocessFailure(expected, subprocess, 'identity_mismatch');
    }
    const isGhcrReceiptRead = details.stage.startsWith('ghcr_receipt') || details.stage.startsWith('ghcr_artifact_');
    const result = {stage:'prepare_receipt_readback', substage:details.stage,
      resource:isGhcrReceiptRead
        ? 'ghcr_prepare_receipt' : details.stage === 'static_artifact_readback' ? 'ghcr_static_artifact'
          : details.stage === 'receipt_validation' ? 'prepare_receipt' : 'prepare_evidence',
      code:isGhcrReceiptRead || details.stage === 'static_artifact_readback'
        ? 'GHCR_READ_FAILED' : details.stage === 'receipt_validation' ? 'PREPARE_RECEIPT_INVALID' : 'PREPARE_EVIDENCE_FAILED',
      ghcrReadbackCompleted:details.ghcrReadbackCompleted};
    if (typeof details.errorType === 'string' && /^[A-Za-z_][A-Za-z0-9_]{0,47}$/.test(details.errorType)) result.errorType = details.errorType;
    if (SAFE_RECEIPT_CATEGORIES.has(details.category)) result.category = details.category;
    if (Number.isInteger(details.httpStatus) && details.httpStatus >= 400 && details.httpStatus <= 599) result.httpStatus = details.httpStatus;
    for (const [field] of identities) result[field] = details[field];
    return result;
  }
  const details = String(stdout ?? '').split(/\r?\n/).map(line =>
    line.match(/^ARTIFACT_CACHE=BLOCKED STAGE=([a-z0-9_]{1,48}) ERROR_TYPE=([A-Za-z0-9_]{1,48})$/))
    .find(Boolean);
  if (!details) return localSubprocessFailure(expected, subprocess, 'missing_diagnostic');
  if (!SAFE_RECEIPT_STAGES.has(details[1])) return localSubprocessFailure(expected, subprocess, 'unsafe_stage');
  const resourceByStage = {prepare_run_metadata: 'prepare_evidence', current_main_ref: 'prepare_evidence',
    ghcr_receipt: 'ghcr_prepare_receipt', prepare_publisher_logs: 'prepare_evidence',
    static_artifact_readback: 'ghcr_static_artifact', receipt_validation: 'prepare_receipt'};
  const codeByStage = {prepare_run_metadata: 'PREPARE_EVIDENCE_FAILED',
    current_main_ref: 'PREPARE_EVIDENCE_FAILED', ghcr_receipt: 'GHCR_READ_FAILED',
    prepare_publisher_logs: 'PREPARE_EVIDENCE_FAILED', static_artifact_readback: 'GHCR_READ_FAILED',
    receipt_validation: 'PREPARE_RECEIPT_INVALID'};
  return {stage: 'prepare_receipt_readback', substage: details[1],
    resource: resourceByStage[details[1]], code: codeByStage[details[1]]};
}

export function appendControllerResultSummary(writeSummary, result, elapsed) {
  const deployDispatched = ['TASK_SCOPED_AUTO_DISPATCHED', 'TASK_SCOPED_AUTO_RETRY_DISPATCHED'].includes(result.state);
  const summary = `## GPoint automatic release orchestration\n\n- State: \`${result.state}\`\n- Candidate: \`${result.candidateSha}\`\n- Previous authorization claim SHA: \`${result.previousClaimSha ?? 'NONE'}\`\n- Authorization: \`${result.authorizationId ?? result.taskAuthorizationId ?? 'EXACT_SHA_REQUIRED'}\`\n- Controller job: \`SELF_HOSTED_REQUIRED\`, trusted project runner; measured controller seconds: \`${elapsed}\`\n- \`SELF_HOSTED_JOB_COUNT=1\`; \`SELF_HOSTED_RUNNER_SECONDS=${elapsed}\` (controller only)\n`;
  try { writeSummary(summary); }
  catch {
    const error = new OrchestrationError('Controller result summary could not be persisted');
    error.controllerDiagnostic = {stage: 'controller_result_summary', resource: 'controller_step_summary',
      code: 'SUMMARY_WRITE_FAILED', deployDispatched};
    throw error;
  }
}

function notifyStage(onStage, stage) {
  if (typeof onStage === 'function') onStage(stage);
}

function preserveHttpStatus(target, source) {
  if (Number.isInteger(source?.status) && source.status >= 400 && source.status <= 599)
    target.status = source.status;
  return target;
}

export function validateEvent(payload) {
  if (payload.repository?.full_name?.toLowerCase() !== REPOSITORY) throw new OrchestrationError('Unexpected repository');
  const run = payload.workflow_run;
  if (!run || !SHA.test(run.head_sha ?? '') || run.head_branch !== 'main' || run.status !== 'completed'
      || run.conclusion !== 'success' || run.path !== PREPARE_PATH
      || !['push', 'workflow_dispatch'].includes(run.event)
      || !/^\d+$/.test(String(run.id ?? '')) || !/^\d+$/.test(String(run.run_attempt ?? ''))) {
    throw new OrchestrationError('Prepare event is not a successful trusted-main run');
  }
  return run;
}

export function taskAutoClaimRef(authorizationId) {
  if (!TASK_AUTH_ID.test(authorizationId ?? '')) throw new OrchestrationError('Invalid task authorization identity');
  // Global per authorization: duplicate Prepare events cannot race into two Deploys.
  return `refs/tags/gpoint-task-auth/${authorizationId}/claimed`;
}

export async function findTaskAuthorization(repoRoot, candidateSha, api) {
  const directory = join(repoRoot, '.release-authorizations');
  if (!fs.existsSync(directory)) return {authorizationId: '', ambiguous: false};
  const matches = [];
  for (const file of fs.readdirSync(directory).filter(name => name.endsWith('.json')).sort()) {
    const id = file.slice(0, -5);
    if (!TASK_AUTH_ID.test(id)) continue;
    try {
      const record = JSON.parse(fs.readFileSync(join(directory, file), 'utf8'));
      const changedPaths = candidateScopeDiff(repoRoot, id, record.production_baseline_sha, candidateSha);
      const proof = validateTaskAuthorization({record, project: 'gpoint', authorizationId: id,
        candidateSha, currentMainSha: candidateSha, productionSha: record.production_baseline_sha, changedPaths});
      let inactive = false;
      for (const state of ['revoked', 'consumed']) {
        const ref = `git/ref/tags/gpoint-task-auth/${id}/${state}`;
        try { await api('GET', ref); inactive = true; }
        catch (error) { if (error.status !== 404) throw error; }
      }
      if (!inactive) matches.push({authorizationId: id, proof});
    } catch {
      // An invalid or stale record cannot authorize automation; exact-SHA release remains separate.
    }
  }
  return matches.length === 1
    ? {authorizationId: matches[0].authorizationId, ambiguous: false}
    : {authorizationId: '', ambiguous: matches.length > 1};
}

export function makeApi(token, fetchImpl = fetch) {
  if (!token) throw new OrchestrationError('GITHUB_TOKEN is required');
  return async (method, path, body) => {
    let response;
    try {
      response = await fetchImpl(`https://api.github.com/repos/${REPOSITORY}/${path.replace(/^\//, '')}`, {
        method, headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'},
        ...(body ? {body: JSON.stringify(body)} : {}),
      });
    } catch (error) {
      throw new OrchestrationError(`GitHub API unavailable (${error?.name ?? 'Error'})`);
    }
    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) {
      const error = new OrchestrationError(`GitHub API request failed (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  };
}

export function resolvePrepareReceipt(sha, runId, attempt, env = process.env) {
  const directory = fs.mkdtempSync(join(tmpdir(), 'gpoint-auto-prepare-'));
  try {
    const result = spawnSync('python3', ['scripts/prepared_artifact_cache.py'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 120_000,
      env: {...env, CANDIDATE_SHA: sha, PREPARE_RUN_ID: runId, PREPARE_RUN_ATTEMPT: attempt,
        ARTIFACT_DESTINATION: directory, RELEASE_MODE: 'preflight',
        GHCR_READ_USERNAME: env.GITHUB_ACTOR ?? 'github-actions[bot]',
        GHCR_READ_TOKEN: env.GITHUB_TOKEN ?? '', GITHUB_TOKEN: env.GITHUB_TOKEN ?? ''},
    });
    if (result.status !== 0) {
      const error = new OrchestrationError('Exact Prepare receipt GHCR readback or run-log provenance verification failed');
      error.controllerDiagnostic = preparedReceiptFailureDiagnostic(result.stdout,
        {candidateSha:sha, runId:String(runId), runAttempt:String(attempt)}, result);
      throw error;
    }
    const evidence = JSON.parse(fs.readFileSync(join(directory, 'ghcr-evidence.json'), 'utf8'));
    if (evidence.runLogProvenance !== 'PASS' ||
        evidence.manifestDigest !== result.stdout.match(/GHCR_RECEIPT_DIGEST=(sha256:[a-f0-9]{64})/)?.[1])
      throw new OrchestrationError('Exact Prepare receipt transport evidence is incomplete');
    return {receiptDigest: evidence.manifestDigest, receiptSha256: evidence.receiptSha256};
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
}

export async function executedPrepareBundle(api, run, resolveReceipt = resolvePrepareReceipt, onStage = () => {}) {
  const sha = run.head_sha;
  const runId = String(run.id);
  const attemptCount = Number(run.run_attempt);
  if (!SHA.test(sha) || !/^\d+$/.test(runId) || !Number.isSafeInteger(attemptCount) || attemptCount < 1) {
    throw new OrchestrationError('Invalid Prepare source identity');
  }
  const epoch = value => {
    if (typeof value !== 'string' || !/Z$|[+-]\d\d:\d\d$/.test(value)) throw new OrchestrationError('Invalid Prepare chronology');
    const ms = Date.parse(value);
    if (!Number.isFinite(ms)) throw new OrchestrationError('Invalid Prepare chronology');
    return ms;
  };
  for (let number = attemptCount; number >= 1; number--) {
    notifyStage(onStage, 'prepare_attempt_metadata');
    const details = await api('GET', `actions/runs/${runId}/attempts/${number}`);
    notifyStage(onStage, 'prepare_attempt_jobs');
    const jobs = await api('GET', `actions/runs/${runId}/attempts/${number}/jobs?per_page=100`);
    if (String(details.run_attempt) !== String(number) || !Array.isArray(jobs.jobs) ||
        (jobs.total_count ?? jobs.jobs.length) > 100) throw new OrchestrationError('Prepare attempts are incomplete');
    const matched = jobs.jobs.filter(job => job.name === 'prepare');
    if (matched.length !== 1) throw new OrchestrationError('Ambiguous executed Prepare job');
    const job = matched[0];
    notifyStage(onStage, 'prepare_job_identity');
    if (job.status !== 'completed' || job.conclusion !== 'success' || job.head_sha !== sha) {
      throw new OrchestrationError('Exact Prepare job did not succeed');
    }
    const started = epoch(details.run_started_at);
    const actualStart = epoch(job.started_at);
    const actualEnd = epoch(job.completed_at);
    notifyStage(onStage, 'prepare_job_chronology');
    if (actualEnd < actualStart) throw new OrchestrationError('Invalid Prepare job chronology');
    if (actualStart < started && actualEnd <= started) continue;
    let receipt;
    try {
      notifyStage(onStage, 'prepare_receipt_readback');
      receipt = await resolveReceipt(sha, runId, String(number));
    } catch (cause) {
      const error = new OrchestrationError('Executed Prepare has no trusted GHCR receipt and run-log provenance');
      if (cause?.controllerDiagnostic) error.controllerDiagnostic = cause.controllerDiagnostic;
      throw preserveHttpStatus(error, cause);
    }
    if (!/^sha256:[a-f0-9]{64}$/.test(receipt?.receiptDigest ?? '') ||
        !/^[a-f0-9]{64}$/.test(receipt?.receiptSha256 ?? ''))
      throw new OrchestrationError('Prepare GHCR receipt digest provenance is invalid');
    return {tag: `prepare-${sha}-${runId}-${number}`, ...receipt, sourceAttempt: String(number)};
  }
  throw new OrchestrationError('No executed trusted Prepare GHCR receipt');
}

async function verifiedPrepare(api, eventRun, resolveReceipt = resolvePrepareReceipt, onStage = () => {}) {
  const sha = eventRun.head_sha;
  notifyStage(onStage, 'current_main_ref');
  if ((await api('GET', 'git/ref/heads/main')).object?.sha !== sha) throw new OrchestrationError('Prepare candidate is no longer current main');
  notifyStage(onStage, 'prepare_run_metadata');
  const run = await api('GET', `actions/runs/${eventRun.id}`);
  for (const key of ['head_sha', 'head_branch', 'status', 'conclusion', 'event', 'path', 'run_attempt']) {
    if (String(run[key]) !== String(eventRun[key])) throw new OrchestrationError('Prepare run metadata changed');
  }
  const {tag, receiptDigest, receiptSha256, sourceAttempt} = await executedPrepareBundle(api, eventRun, resolveReceipt, onStage);
  notifyStage(onStage, 'deploy_inventory');
  const active = await api('GET', `actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100`);
  if ((active.workflow_runs ?? []).some(item => ['queued', 'in_progress', 'waiting', 'pending'].includes(item.status))) {
    throw new OrchestrationError('A GPoint Deploy workflow is active');
  }
  return {sha, tag, receiptDigest, receiptSha256, sourceAttempt};
}

export async function dispatchPrepare(payload, api, orchestratorRunId = process.env.GITHUB_RUN_ID ?? '',
  repoRoot = process.cwd(), authorizationLookup = findTaskAuthorization, resolveReceipt = resolvePrepareReceipt,
  onStage = () => {}) {
  notifyStage(onStage, 'prepare_event_validation');
  const run = validateEvent(payload);
  const {sha, tag, receiptDigest, receiptSha256, sourceAttempt} = await verifiedPrepare(api, run, resolveReceipt, onStage);
  notifyStage(onStage, 'authorization_lookup');
  const authorization = await authorizationLookup(repoRoot, sha, api);
  if (authorization.ambiguous) {
    return {state: 'TASK_AUTHORIZATION_AMBIGUOUS', candidateSha: sha,
      prepareRunId: String(run.id), prepareRunAttempt: sourceAttempt};
  }
  if (!authorization.authorizationId) {
    return {state: 'OWNER_EXACT_SHA_APPROVAL_REQUIRED', candidateSha: sha,
      prepareRunId: String(run.id), prepareRunAttempt: sourceAttempt, tag,
      receiptDigest, receiptSha256};
  }
  notifyStage(onStage, 'deploy_inventory_before_claim');
  const active = await api('GET', `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/runs?branch=main&per_page=100`);
  if ((active.workflow_runs ?? []).some(item => ['queued', 'in_progress', 'waiting', 'pending'].includes(item.status))) {
    throw new OrchestrationError('A GPoint Deploy workflow is active');
  }
  const ref = taskAutoClaimRef(authorization.authorizationId);
  let claimCreated = false;
  let previousClaimSha = '';
  try {
    notifyStage(onStage, 'atomic_claim_create');
    await api('POST', 'git/refs', {ref, sha});
    claimCreated = true;
  } catch (error) {
    if (error.status !== 422)
      throw preserveHttpStatus(new OrchestrationError('Atomic task-scoped Deploy claim failed'), error);
    notifyStage(onStage, 'atomic_claim_readback');
    const existing = await api('GET', `git/ref/tags/${ref.replace('refs/tags/', '')}`);
    if (existing.object?.type !== 'commit' || !SHA.test(existing.object?.sha ?? '')) {
      throw new OrchestrationError('Existing task-scoped authorization claim is invalid');
    }
    previousClaimSha = existing.object.sha;
  }
  try {
    notifyStage(onStage, 'deploy_workflow_dispatch');
    await api('POST', `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/dispatches`, {ref: 'main', inputs: {
      candidate: sha, mode: 'task-scoped-auto', approve_production: 'false',
      prepare_run_id: String(run.id), prepare_run_attempt: String(sourceAttempt),
      prepare_receipt_digest: receiptDigest, prepare_receipt_sha256: receiptSha256,
      orchestrator_run_id: orchestratorRunId, task_authorization_id: authorization.authorizationId,
    }});
  } catch {
    throw new OrchestrationError('Task-scoped Deploy dispatch result is unknown; confirm workflow and production state before retry');
  }
  return {state: claimCreated ? 'TASK_SCOPED_AUTO_DISPATCHED' : 'TASK_SCOPED_AUTO_RETRY_DISPATCHED', candidateSha: sha,
    prepareRunId: String(run.id), prepareRunAttempt: String(sourceAttempt),
    authorizationId: authorization.authorizationId,
    ...(previousClaimSha ? {previousClaimSha} : {})};
}

async function main() {
  const started = performance.now();
  let stage = 'event_payload';
  let payload = {};
  try {
    payload = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
    const api = makeApi(process.env.GITHUB_TOKEN);
    const onStage = value => { stage = value; };
    const result = await dispatchPrepare(payload, api, process.env.GITHUB_RUN_ID ?? '', process.cwd(),
      findTaskAuthorization, resolvePrepareReceipt, onStage);
    const elapsed = Math.round((performance.now() - started) / 1000);
    stage = 'controller_result_summary';
    appendControllerResultSummary(content => fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, content), result, elapsed);
    console.log(JSON.stringify(result));
  } catch (error) {
    const run = payload.workflow_run ?? {};
    const diagnostic = controllerFailureDiagnostic(error, {stage, resource: 'prepare_run',
      candidateSha: run.head_sha ?? '', prepareRunId: String(run.id ?? ''),
      prepareRunAttempt: String(run.run_attempt ?? '')});
    console.error(`AUTO_RELEASE_CONTROLLER=${JSON.stringify(diagnostic)}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
    main().catch(() => { console.error('AUTO_RELEASE_CONTROLLER={"state":"BLOCKED","stage":"event_payload","code":"UNEXPECTED_ERROR"}'); process.exitCode = 1; });
}
