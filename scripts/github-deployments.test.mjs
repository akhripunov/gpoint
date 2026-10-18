import test from 'node:test';
import assert from 'node:assert/strict';
import {createGitHubDeploymentsClient, deploymentPayload, assertDeploymentMatchesLive} from './github-deployments.mjs';

const sha = 'a'.repeat(40);
const assetSha = `sha256:${'b'.repeat(64)}`;
const identity = {candidateSha: sha, runId: '42', runAttempt: '2', prepareRunId: '41',
  prepareRunAttempt: '1', prepareReleaseId: 91, prepareReleaseTag: `gpoint-prepare-${sha}-41-1`,
  prepareAssetId: 92, prepareAssetSha256: assetSha, authorizationMode: 'EXACT_SHA',
  logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/42/attempts/2'};
const response = (status, body) => ({status, ok: status >= 200 && status < 300,
  text: async () => body === null ? '' : JSON.stringify(body)});

test('Deployment provenance binds exact candidate, Prepare Release, asset and attempt', () => {
  const payload = deploymentPayload(identity);
  assert.equal(payload.candidateSha, sha);
  assert.deepEqual(payload.workflow, {runId: '42', attempt: '2', url: identity.logUrl});
  assert.equal(payload.prepare.releaseId, 91);
  assert.equal(payload.prepare.assetId, 92);
});

test('empty required_contexts is rejected before any API request', async () => {
  let calls = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', fetchImpl: async () => { calls += 1; return response(200, []); }});
  await assert.rejects(client.beginProductionDeployment(identity, {requiredContexts: []}), /empty list would bypass checks/);
  assert.equal(calls, 0);
});

test('ambiguous Deployment create is read back by exact workflow attempt without duplicate POST', async () => {
  const created = {id: 7, environment: 'production', production_environment: true, transient_environment: false,
    task: 'gpoint-production-deploy', sha, ref: sha, created_at: '2026-10-18T00:00:00Z',
    payload: JSON.stringify(deploymentPayload(identity))};
  // A first empty list and a later exact read-back; no second Deployment POST.
  let listCount = 0;
  const readbackClient = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/deployments?')) { listCount += 1; return response(200, listCount <= 2 ? [] : [created]); }
    if (url.includes('/statuses?')) return response(200, []);
    if (url.endsWith('/deployments') && init.method === 'POST') throw new Error('timeout after accepted write');
    if (url.endsWith('/deployments/7/statuses')) return response(201, {state: 'in_progress'});
    throw new Error(`unexpected ${url}`);
  }});
  const result = await readbackClient.beginProductionDeployment(identity, {requiredContexts: ['GPoint — Prepare / prepare']});
  assert.equal(result.deployment.id, 7);
  assert.equal(listCount, 3);
});

test('failed status POST reads back the same deployment and never invokes Apply', async () => {
  const deployment = {id: 7, environment: 'production', production_environment: true, transient_environment: false,
    task: 'gpoint-production-deploy', sha, ref: sha, created_at: '2026-10-18T00:00:00Z',
    payload: JSON.stringify(deploymentPayload(identity))};
  let posts = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/statuses?')) return response(200, [{state: 'success', description: 'installed',
      log_url: identity.logUrl, environment_url: 'https://gpoint.one', created_at: '2026-10-18T00:01:00Z'}]);
    if (url.endsWith('/statuses') && init.method === 'POST') { posts += 1; throw new Error('timeout'); }
    throw new Error(`unexpected ${url}`);
  }});
  const value = await client.publishStatus(deployment, identity, {state: 'success', description: 'installed', logUrl: identity.logUrl});
  assert.equal(value.readBackAfterAmbiguousPost, true);
  assert.equal(posts, 1);
});

test('rerunning a workflow cannot create a second Deployment on a new run attempt', async () => {
  const prior = {id: 7, environment: 'production', production_environment: true, transient_environment: false,
    task: 'gpoint-production-deploy', sha, ref: sha, created_at: '2026-10-18T00:00:00Z',
    payload: JSON.stringify(deploymentPayload(identity))};
  const rerun = {...identity, runAttempt: '3'};
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async () => response(200, [prior])});
  await assert.rejects(client.beginProductionDeployment(rerun, {requiredContexts: ['GPoint — Prepare / prepare']}),
    /workflow run already has a Deployment/);
});

test('a newer success supersedes a terminal older failure as the Deploy baseline', async () => {
  const record = (id, valueSha, created, runId) => ({id, environment: 'production',
    production_environment: true, transient_environment: false, task: 'gpoint-production-deploy',
    sha: valueSha, ref: valueSha, created_at: created, payload: JSON.stringify({...deploymentPayload(identity),
      candidateSha: valueSha, workflow: {...deploymentPayload(identity).workflow, runId: String(runId)}})});
  const old = record(6, 'c'.repeat(40), '2026-10-17T00:00:00Z', 40);
  const latest = record(7, sha, '2026-10-18T00:00:00Z', 42);
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [latest, old]);
    if (url.includes('/deployments/7/statuses?')) return response(200, [{state: 'success',
      description: 'stage=complete', log_url: identity.logUrl, environment_url: 'https://gpoint.one',
      created_at: '2026-10-18T00:01:00Z'}]);
    if (url.includes('/deployments/6/statuses?')) return response(200, [{state: 'error',
      description: 'stage=post_apply', log_url: identity.logUrl, environment_url: 'https://gpoint.one',
      created_at: '2026-10-17T00:01:00Z'}]);
    throw new Error(`unexpected ${url}`);
  }});
  const baseline = await client.readProductionBaseline();
  assert.equal(baseline.baselineSha, sha);
  assert.equal(baseline.deploymentId, 7);
});

test('a created Deployment is exposed when its initial status cannot be confirmed', async () => {
  const created = {id: 8, environment: 'production', production_environment: true,
    transient_environment: false, task: 'gpoint-production-deploy', sha, ref: sha,
    created_at: '2026-10-18T00:00:00Z', payload: JSON.stringify(deploymentPayload(identity))};
  let calls = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', sleep: async () => {},
    fetchImpl: async (url, init) => {
      if (url.includes('/deployments?')) return response(200, []);
      if (url.endsWith('/deployments') && init.method === 'POST') return response(201, created);
      if (url.includes('/statuses?')) return response(200, []);
      if (url.endsWith('/statuses') && init.method === 'POST') { calls += 1; return response(403, {message: 'forbidden'}); }
      throw new Error(`unexpected ${url}`);
    }});
  await assert.rejects(client.beginProductionDeployment(identity,
    {requiredContexts: ['GPoint — Prepare / prepare']}), error => error.deployment?.id === 8);
  assert.equal(calls, 2);
});

test('an unavailable Deployments API fails closed before creating a Deployment', async () => {
  let creates = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock',
    fetchImpl: async (url, init) => {
      if (url.includes('/deployments?')) throw new Error('network unavailable');
      if (url.endsWith('/deployments') && init.method === 'POST') { creates += 1; return response(201, {}); }
      throw new Error(`unexpected ${url}`);
    }});
  await assert.rejects(client.beginProductionDeployment(identity,
    {requiredContexts: ['GPoint — Prepare / prepare']}), /GET request failed/);
  assert.equal(creates, 0);
});

test('missing deployments write permission blocks the create without a retry', async () => {
  let creates = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock',
    fetchImpl: async (url, init) => {
      if (url.includes('/deployments?')) return response(200, []);
      if (url.endsWith('/deployments') && init.method === 'POST') { creates += 1; return response(403, {message: 'forbidden'}); }
      throw new Error(`unexpected ${url}`);
    }});
  await assert.rejects(client.beginProductionDeployment(identity,
    {requiredContexts: ['GPoint — Prepare / prepare']}), /HTTP 403/);
  assert.equal(creates, 1);
});

test('a live VPS SHA that differs from the successful GitHub Deployment blocks', () => {
  assert.throws(() => assertDeploymentMatchesLive({baselineSha: sha},
    {runtimeSha: 'c'.repeat(40), miniappSha: sha, prismaHead: 'migration_001'}), /does not match populated VPS/);
  assert.throws(() => assertDeploymentMatchesLive({baselineSha: sha},
    {runtimeSha: sha, miniappSha: sha, prismaHead: 'NONE'}), /Prisma migration head/);
});

test('an active conflicting Deployment blocks a new candidate', async () => {
  const priorIdentity = {...identity, runId: '40', runAttempt: '1', candidateSha: 'c'.repeat(40)};
  const active = {id: 6, environment: 'production', production_environment: true,
    transient_environment: false, task: 'gpoint-production-deploy', sha: priorIdentity.candidateSha,
    ref: priorIdentity.candidateSha, created_at: '2026-10-18T00:00:00Z',
    payload: JSON.stringify(deploymentPayload(priorIdentity))};
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock',
    fetchImpl: async url => {
      if (url.includes('/deployments?')) return response(200, [active]);
      if (url.includes('/deployments/6/statuses?')) return response(200, [{state: 'in_progress',
        description: 'stage=pre_apply', log_url: priorIdentity.logUrl, environment_url: 'https://gpoint.one',
        created_at: '2026-10-18T00:01:00Z'}]);
      throw new Error(`unexpected ${url}`);
    }});
  await assert.rejects(client.beginProductionDeployment(identity,
    {requiredContexts: ['GPoint — Prepare / prepare']}), /is in_progress/);
});
