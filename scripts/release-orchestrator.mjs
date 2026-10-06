#!/usr/bin/env node
/** Trusted workflow_run controller; only dispatches an exact read-only GPoint preflight. */
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';

export const REPOSITORY = 'akhripunov/gpoint';
export const PREPARE_PATH = '.github/workflows/gpoint-prepare.yml';
export const DEPLOY_PATH = '.github/workflows/gpoint-deploy.yml';
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

export function claimRef(run) {
  return `refs/tags/gpoint-orchestrator/preflight-${run.head_sha}-${run.id}-${run.run_attempt}`;
}

async function matchingPreflightDispatch(api, run, controllerRunId) {
  const sha = run.head_sha;
  const route = `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/runs?branch=main&head_sha=${sha}&per_page=100`;
  const data = await api('GET', route);
  const title = `GPoint preflight ${sha} prepare/${run.id}-${run.run_attempt} controller/${controllerRunId}`;
  const matches = (data.workflow_runs ?? []).filter(item => item.head_sha === sha
    && item.head_branch === 'main' && item.event === 'workflow_dispatch'
    && item.path === DEPLOY_PATH && item.display_title?.startsWith(title)
    && /^\d+$/.test(String(item.id ?? '')) && /^\d+$/.test(String(item.run_attempt ?? '')));
  if (matches.length > 1) throw new OrchestrationError('Multiple preflight runs match one controller dispatch');
  return matches[0] ?? null;
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

async function verifiedPrepare(api, eventRun) {
  const sha = eventRun.head_sha;
  if ((await api('GET', 'git/ref/heads/main')).object?.sha !== sha) throw new OrchestrationError('Prepare candidate is no longer current main');
  const run = await api('GET', `actions/runs/${eventRun.id}`);
  for (const key of ['head_sha', 'head_branch', 'status', 'conclusion', 'event', 'path', 'run_attempt']) {
    if (String(run[key]) !== String(eventRun[key])) throw new OrchestrationError('Prepare run metadata changed');
  }
  const tag = `gpoint-prepare-${sha}-${eventRun.id}-${eventRun.run_attempt}`;
  const release = await api('GET', `releases/tags/${tag}`);
  if (release.tag_name !== tag || release.draft !== false || release.immutable !== true) throw new OrchestrationError('Prepare release is not immutable and published');
  const ref = await api('GET', `git/ref/tags/${tag}`);
  if (ref.object?.type !== 'commit' || ref.object?.sha !== sha) throw new OrchestrationError('Prepare release tag does not point to candidate SHA');
  const assets = (release.assets ?? []).filter(asset => asset.name === 'bundle.zip');
  if (release.assets?.length !== 1 || assets.length !== 1) throw new OrchestrationError('Expected exactly one immutable Prepare bundle');
  const asset = assets[0];
  if (!/^\d+$/.test(String(asset.id ?? '')) || asset.state !== 'uploaded' || !Number.isSafeInteger(asset.size)
      || asset.size <= 0 || !/^sha256:[a-f0-9]{64}$/.test(asset.digest ?? '')) throw new OrchestrationError('Prepare asset digest/provenance is invalid');
  const active = await api('GET', `actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100`);
  if ((active.workflow_runs ?? []).some(item => ['queued', 'in_progress', 'waiting', 'pending'].includes(item.status))) {
    throw new OrchestrationError('A GPoint Deploy workflow is active');
  }
  return {sha, tag, asset};
}

export async function dispatchPrepare(payload, api, orchestratorRunId = process.env.GITHUB_RUN_ID ?? '', sleep = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const run = validateEvent(payload);
  const {sha, tag, asset} = await verifiedPrepare(api, run);
  const ref = claimRef(run);
  try {
    await api('POST', 'git/refs', {ref, sha});
  } catch (error) {
    if (error.status !== 422) throw new OrchestrationError('Atomic preflight claim failed');
    const existing = await api('GET', `git/ref/tags/${ref.replace('refs/tags/', '')}`);
    if (existing.object?.type !== 'commit' || existing.object?.sha !== sha) throw new OrchestrationError('Existing preflight claim differs from candidate');
    return {state: 'PREFLIGHT_ALREADY_CLAIMED', candidateSha: sha, prepareRunId: String(run.id), prepareRunAttempt: String(run.run_attempt), tag, assetId: String(asset.id), assetSha256: asset.digest.slice(7)};
  }
  try {
    await api('POST', `actions/workflows/${DEPLOY_PATH.split('/').at(-1)}/dispatches`, {ref: 'main', inputs: {
      candidate: sha, mode: 'preflight', approve_production: 'false',
      prepare_run_id: String(run.id), prepare_run_attempt: String(run.run_attempt),
      orchestrator_run_id: orchestratorRunId,
    }});
  } catch (dispatchError) {
    // Claim is retained. Only read back the correlated run; never repeat an uncertain POST.
    for (let index = 0; index < 3; index++) {
      try {
        const existing = await matchingPreflightDispatch(api, run, orchestratorRunId);
        if (existing) return {state: 'PREFLIGHT_DISPATCH_RECONCILED', candidateSha: sha,
          prepareRunId: String(run.id), prepareRunAttempt: String(run.run_attempt),
          deployRunId: String(existing.id), deployRunAttempt: String(existing.run_attempt),
          tag, assetId: String(asset.id), assetSha256: asset.digest.slice(7)};
      } catch { /* bounded read-only retry; preserve the uncertain dispatch state */ }
      if (index < 2) await sleep(2000);
    }
    throw new OrchestrationError('Preflight dispatch outcome is uncertain; no blind retry is allowed', {cause: dispatchError});
  }
  return {state: 'PREFLIGHT_DISPATCHED', candidateSha: sha, prepareRunId: String(run.id), prepareRunAttempt: String(run.run_attempt), tag, assetId: String(asset.id), assetSha256: asset.digest.slice(7)};
}

async function main() {
  const started = performance.now();
  const payload = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
  const result = await dispatchPrepare(payload, makeApi(process.env.GITHUB_TOKEN));
  const elapsed = Math.round((performance.now() - started) / 1000);
  fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `## GPoint automatic preflight orchestration\n\n- State: \`${result.state}\`\n- Candidate: \`${result.candidateSha}\`\n- Prepare run/attempt: \`${result.prepareRunId}/${result.prepareRunAttempt}\`\n- Immutable tag: \`${result.tag}\`\n- Asset ID/SHA256: \`${result.assetId}\` / \`${result.assetSha256}\`\n- Controller job: \`SELF_HOSTED_REQUIRED\`, trusted project runner; measured controller seconds: \`${elapsed}\`\n- \`SELF_HOSTED_JOB_COUNT=1\`; \`SELF_HOSTED_RUNNER_SECONDS=${elapsed}\` (controller only)\n- \`DUPLICATE_HOSTED_WORK_REMOVED=YES\`; \`CACHE_PREVENTED_DOWNLOAD=UNKNOWN\` until Mac preflight\n`);
  console.log(JSON.stringify(result));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch(error => { console.error(`AUTO_PREFLIGHT=BLOCKED TYPE=${error?.name ?? 'Error'}`); process.exitCode = 1; });
}
