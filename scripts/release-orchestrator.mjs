#!/usr/bin/env node
/** Trusted Prepare-completion controller for GPoint's existing Deploy workflow. */
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {join, resolve} from 'node:path';
import {authorizationRecordPath, candidateScopeDiff, validateTaskAuthorization} from './release-authorization.mjs';

export const REPOSITORY = 'akhripunov/gpoint';
export const PREPARE_PATH = '.github/workflows/gpoint-prepare.yml';
export const DEPLOY_PATH = '.github/workflows/gpoint-deploy.yml';
const TASK_AUTH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/;
const SHA = /^[a-f0-9]{40}$/;

export class OrchestrationError extends Error {}

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

export async function executedPrepareBundle(api, run) {
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
    const tag = `gpoint-prepare-${sha}-${runId}-${number}`;
    const details = await api('GET', `actions/runs/${runId}/attempts/${number}`);
    const jobs = await api('GET', `actions/runs/${runId}/attempts/${number}/jobs?per_page=100`);
    if (String(details.run_attempt) !== String(number) || !Array.isArray(jobs.jobs) ||
        (jobs.total_count ?? jobs.jobs.length) > 100) throw new OrchestrationError('Prepare attempts are incomplete');
    const matched = jobs.jobs.filter(job => job.name === 'prepare');
    if (matched.length !== 1) throw new OrchestrationError('Ambiguous executed Prepare job');
    const job = matched[0];
    if (job.status !== 'completed' || job.conclusion !== 'success' || job.head_sha !== sha) {
      throw new OrchestrationError('Exact Prepare job did not succeed');
    }
    const started = epoch(details.run_started_at);
    const actualStart = epoch(job.started_at);
    const actualEnd = epoch(job.completed_at);
    if (actualEnd < actualStart) throw new OrchestrationError('Invalid Prepare job chronology');
    let release;
    try { release = await api('GET', `releases/tags/${tag}`); }
    catch (error) { if (error.status !== 404) throw error; }
    if (!release) {
      if (actualStart < started && actualEnd <= started) continue;
      throw new OrchestrationError('Executed Prepare has no immutable bundle');
    }
    if (actualStart < started) throw new OrchestrationError('Prepare artifact attempt does not match executed job');
    if (release.tag_name !== tag || release.draft !== false || release.immutable !== true) {
      throw new OrchestrationError('Prepare release is not immutable and published');
    }
    const ref = await api('GET', `git/ref/tags/${tag}`);
    if (ref.object?.type !== 'commit' || ref.object?.sha !== sha) throw new OrchestrationError('Prepare tag source mismatch');
    const assets = (release.assets ?? []).filter(asset => asset.name === 'bundle.zip');
    if (release.assets?.length !== 1 || assets.length !== 1) throw new OrchestrationError('Expected exactly one immutable Prepare bundle');
    const asset = assets[0];
    if (!/^\d+$/.test(String(asset.id ?? '')) || asset.state !== 'uploaded' || !Number.isSafeInteger(asset.size)
        || asset.size <= 0 || !/^sha256:[a-f0-9]{64}$/.test(asset.digest ?? '')) throw new OrchestrationError('Prepare asset digest/provenance is invalid');
    return {tag, asset, sourceAttempt: String(number)};
  }
  throw new OrchestrationError('No executed immutable Prepare bundle');
}

async function verifiedPrepare(api, eventRun) {
  const sha = eventRun.head_sha;
  if ((await api('GET', 'git/ref/heads/main')).object?.sha !== sha) throw new OrchestrationError('Prepare candidate is no longer current main');
  const run = await api('GET', `actions/runs/${eventRun.id}`);
  for (const key of ['head_sha', 'head_branch', 'status', 'conclusion', 'event', 'path', 'run_attempt']) {
    if (String(run[key]) !== String(eventRun[key])) throw new OrchestrationError('Prepare run metadata changed');
  }
  const {tag, asset, sourceAttempt} = await executedPrepareBundle(api, eventRun);
  const active = await api('GET', `actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100`);
  if ((active.workflow_runs ?? []).some(item => ['queued', 'in_progress', 'waiting', 'pending'].includes(item.status))) {
    throw new OrchestrationError('A GPoint Deploy workflow is active');
  }
  return {sha, tag, asset, sourceAttempt};
}

export async function dispatchPrepare(payload, api, orchestratorRunId = process.env.GITHUB_RUN_ID ?? '',
  repoRoot = process.cwd(), authorizationLookup = findTaskAuthorization) {
  const run = validateEvent(payload);
  const {sha, tag, asset, sourceAttempt} = await verifiedPrepare(api, run);
  const authorization = await authorizationLookup(repoRoot, sha, api);
  if (authorization.ambiguous) {
    return {state: 'TASK_AUTHORIZATION_AMBIGUOUS', candidateSha: sha,
      prepareRunId: String(run.id), prepareRunAttempt: sourceAttempt};
  }
  if (!authorization.authorizationId) {
    return {state: 'OWNER_EXACT_SHA_APPROVAL_REQUIRED', candidateSha: sha,
      prepareRunId: String(run.id), prepareRunAttempt: sourceAttempt, tag,
      assetId: String(asset.id), assetSha256: asset.digest.slice(7)};
  }
  const active = await api('GET', `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/runs?branch=main&per_page=100`);
  if ((active.workflow_runs ?? []).some(item => ['queued', 'in_progress', 'waiting', 'pending'].includes(item.status))) {
    throw new OrchestrationError('A GPoint Deploy workflow is active');
  }
  const ref = taskAutoClaimRef(authorization.authorizationId);
  try {
    await api('POST', 'git/refs', {ref, sha});
  } catch (error) {
    if (error.status !== 422) throw new OrchestrationError('Atomic task-scoped Deploy claim failed');
    const existing = await api('GET', `git/ref/tags/${ref.replace('refs/tags/', '')}`);
    if (existing.object?.type !== 'commit' || existing.object?.sha !== sha) {
      throw new OrchestrationError('Existing task-scoped authorization claim is invalid');
    }
    return {state: 'TASK_SCOPED_AUTO_ALREADY_CLAIMED', candidateSha: existing.object.sha,
      authorizationId: authorization.authorizationId};
  }
  try {
    await api('POST', `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/dispatches`, {ref: 'main', inputs: {
      candidate: sha, mode: 'task-scoped-auto', approve_production: 'false',
      prepare_run_id: String(run.id), prepare_run_attempt: String(sourceAttempt),
      orchestrator_run_id: orchestratorRunId, task_authorization_id: authorization.authorizationId,
    }});
  } catch {
    throw new OrchestrationError('Task-scoped Deploy dispatch failed after durable claim; reconcile before any retry');
  }
  return {state: 'TASK_SCOPED_AUTO_DISPATCHED', candidateSha: sha,
    prepareRunId: String(run.id), prepareRunAttempt: String(sourceAttempt),
    authorizationId: authorization.authorizationId};
}

async function main() {
  const started = performance.now();
  const payload = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
  const api = makeApi(process.env.GITHUB_TOKEN);
  const result = await dispatchPrepare(payload, api);
  const elapsed = Math.round((performance.now() - started) / 1000);
  fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `## GPoint automatic release orchestration\n\n- State: \`${result.state}\`\n- Candidate: \`${result.candidateSha}\`\n- Authorization: \`${result.authorizationId ?? result.taskAuthorizationId ?? 'EXACT_SHA_REQUIRED'}\`\n- Controller job: \`SELF_HOSTED_REQUIRED\`, trusted project runner; measured controller seconds: \`${elapsed}\`\n- \`SELF_HOSTED_JOB_COUNT=1\`; \`SELF_HOSTED_RUNNER_SECONDS=${elapsed}\` (controller only)\n`);
  console.log(JSON.stringify(result));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
    main().catch(error => { console.error(`AUTO_RELEASE_CONTROLLER=BLOCKED TYPE=${error?.name ?? 'Error'}`); process.exitCode = 1; });
}
