import test from 'node:test';
import assert from 'node:assert/strict';
import {createGitHubDeploymentsClient, deploymentPayload, assertDeploymentMatchesLive} from './github-deployments.mjs';
import './reconcile-deployment.test.mjs';

const sha = 'a'.repeat(40);
const assetSha = `sha256:${'b'.repeat(64)}`;
const identity = {candidateSha: sha, runId: '42', runAttempt: '2', prepareRunId: '41',
  prepareRunAttempt: '1', prepareReceiptDigest: assetSha, prepareReceiptSha256: 'c'.repeat(64),
  imageDigests: {app: assetSha, agata: `sha256:${'d'.repeat(64)}`, gsales: `sha256:${'e'.repeat(64)}`}, authorizationMode: 'EXACT_SHA',
  logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/42/attempts/2'};
const response = (status, body) => ({status, ok: status >= 200 && status < 300,
  text: async () => body === null ? '' : JSON.stringify(body)});

test('Deployment provenance binds exact candidate, Prepare OCI receipt digest and attempt', () => {
  const payload = deploymentPayload(identity);
  assert.equal(payload.candidateSha, sha);
  assert.deepEqual(payload.workflow, {runId: '42', attempt: '2', url: identity.logUrl});
  assert.equal(payload.prepare.receiptDigest, identity.prepareReceiptDigest);
  assert.deepEqual(payload.prepare.imageDigests, identity.imageDigests);
});

test('STATIC_UI_ONLY Deployment binds a separate Mini App OCI artifact and inherited image identities', () => {
  const staticIdentity = {...identity, route: 'STATIC_UI_ONLY',
    staticArtifactDigest: `sha256:${'f'.repeat(64)}`, staticArtifactContentSha256: 'a'.repeat(64)};
  const payload = deploymentPayload(staticIdentity);
  assert.equal(payload.route, 'STATIC_UI_ONLY');
  assert.deepEqual(payload.prepare.staticArtifact, {digest: staticIdentity.staticArtifactDigest,
    contentSha256: staticIdentity.staticArtifactContentSha256});
  assert.deepEqual(payload.prepare.imageDigests, identity.imageDigests);
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

test('first Deployment history is optional and returns an explicit empty baseline', async () => {
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async () => response(200, [])});
  assert.deepEqual(await client.readProductionBaseline(), {
    historyExists: false, baselineSha: null, deploymentId: null, status: null, preApplyFailure: false,
  });
});

test('a second release uses the latest successful Deployment as baseline', async () => {
  const earlier = {...validDeployment(1, 'b'.repeat(40), identity, '2026-10-16T00:00:00Z')};
  const latest = {...validDeployment(2, sha, identity, '2026-10-17T00:00:00Z')};
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [earlier, latest]);
    if (url.includes('/deployments/1/statuses')) return response(200, [status('success', '2026-10-16T00:05:00Z')]);
    if (url.includes('/deployments/2/statuses')) return response(200, [status('success', '2026-10-17T00:05:00Z')]);
    throw new Error(`unexpected ${url}`);
  }});
  const baseline = await client.readProductionBaseline();
  assert.equal(baseline.historyExists, true);
  assert.equal(baseline.baselineSha, sha);
  assert.equal(baseline.deploymentId, 2);
});

test('conflicting equal-time latest statuses block production baseline selection', async () => {
  const deployment = validDeployment(9, sha, identity, '2026-10-18T00:00:00Z');
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [deployment]);
    if (url.includes('/statuses?')) return response(200, [
      status('success', '2026-10-18T00:05:00Z', 'stage=complete'),
      status('error', '2026-10-18T00:05:00Z', 'stage=post_apply'),
    ]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readProductionBaseline(), /conflicting statuses at the latest timestamp/);
});

test('active and post-Apply failed Deployments block another baseline selection', async () => {
  for (const state of ['in_progress', 'error', 'failure']) {
    const deployment = validDeployment(3, sha, identity, '2026-10-18T00:00:00Z');
    const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
      if (url.includes('/deployments?')) return response(200, [deployment]);
      if (url.includes('/statuses')) return response(200, [status(state, '2026-10-18T00:05:00Z',
        state === 'failure' ? 'stage=post_apply sha=aaaaaaaaaaaa' : undefined)]);
      throw new Error(`unexpected ${url}`);
    }});
    await assert.rejects(client.readProductionBaseline(), /in_progress|error|outside the verified pre-Apply stage/);
  }
});

test('a verified pre-Apply failure may fall back to the preceding successful install', async () => {
  const failed = validDeployment(2, sha, identity, '2026-10-18T00:00:00Z');
  const success = validDeployment(1, 'b'.repeat(40), identity, '2026-10-17T00:00:00Z');
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [failed, success]);
    if (url.includes('/deployments/2/statuses')) return response(200, [status('failure', '2026-10-18T00:05:00Z', 'stage=pre_apply')]);
    if (url.includes('/deployments/1/statuses')) return response(200, [status('success', '2026-10-17T00:05:00Z')]);
    throw new Error(`unexpected ${url}`);
  }});
  const baseline = await client.readProductionBaseline();
  assert.equal(baseline.baselineSha, 'b'.repeat(40));
  assert.equal(baseline.failedDeploymentId, 2);
  assert.equal(baseline.preApplyFailure, true);
});

test('Deployment POST sends exact production flags, SHA and required Prepare context', async () => {
  let createBody;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/deployments?')) return response(200, []);
    if (url.endsWith('/deployments') && init.method === 'POST') {
      createBody = JSON.parse(init.body);
      return response(201, validDeployment(9, sha, identity, '2026-10-18T00:00:00Z'));
    }
    if (url.endsWith('/statuses') && init.method === 'POST') return response(201, {state: 'in_progress'});
    throw new Error(`unexpected ${url}`);
  }});
  await client.beginProductionDeployment(identity, {requiredContexts: ['GPoint — Prepare / prepare']});
  assert.equal(createBody.ref, sha);
  assert.equal(createBody.environment, 'production');
  assert.equal(createBody.auto_merge, false);
  assert.equal(createBody.production_environment, true);
  assert.equal(createBody.transient_environment, false);
  assert.deepEqual(createBody.required_contexts, ['GPoint — Prepare / prepare']);
});

test('an absent ambiguous-create readback never triggers a second create POST', async () => {
  let creates = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/deployments?')) return response(200, []);
    if (url.endsWith('/deployments') && init.method === 'POST') { creates += 1; throw new Error('timed out'); }
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.beginProductionDeployment(identity, {requiredContexts: ['GPoint — Prepare / prepare']}), /POST request failed/);
  assert.equal(creates, 1);
});

test('a missing status after ambiguous write retries only that same Deployment status', async () => {
  const deployment = validDeployment(7, sha, identity, '2026-10-18T00:00:00Z');
  const posts = [];
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', sleep: async () => {}, fetchImpl: async (url, init) => {
    if (url.includes('/statuses?')) return response(200, []);
    if (url.endsWith('/statuses') && init.method === 'POST') {
      posts.push(url);
      if (posts.length === 1) throw new Error('timeout before write');
      return response(201, {state: 'success'});
    }
    throw new Error(`unexpected ${url}`);
  }});
  const result = await client.publishStatus(deployment, identity, {state: 'success', description: 'installed', logUrl: identity.logUrl});
  assert.equal(result.state, 'success');
  assert.deepEqual(posts, ['https://mock/repos/akhripunov/gpoint/deployments/7/statuses',
    'https://mock/repos/akhripunov/gpoint/deployments/7/statuses']);
});

test('tampered Prepare asset provenance returned by the API is rejected', async () => {
  const tampered = validDeployment(8, sha, identity, '2026-10-18T00:00:00Z');
  const payload = JSON.parse(tampered.payload);
  payload.prepare.receiptSha256 = 'd'.repeat(64);
  tampered.payload = JSON.stringify(payload);
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/deployments?')) return response(200, []);
    if (url.endsWith('/deployments') && init.method === 'POST') return response(201, tampered);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.beginProductionDeployment(identity, {requiredContexts: ['GPoint — Prepare / prepare']}), /payload read-back does not match/);
});

function validDeployment(id, candidate, deploymentIdentityValue, createdAt) {
  return {id, environment: 'production', production_environment: true, transient_environment: false,
    task: 'gpoint-production-deploy', sha: candidate, ref: candidate, created_at: createdAt,
    payload: JSON.stringify(deploymentPayload({...deploymentIdentityValue, candidateSha: candidate}))};
}

function status(state, createdAt, description = `stage=${state}`) {
  return {state, description, log_url: identity.logUrl, environment_url: 'https://gpoint.one', created_at: createdAt};
}

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

test('verified V5 parent lookup returns the exact successful production Deployment and identity', async () => {
  const deployment = validDeployment(22, sha, identity, '2026-10-18T00:00:00Z');
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [deployment]);
    if (url.endsWith('/deployments/22')) return response(200, deployment);
    if (url.includes('/deployments/22/statuses')) return response(200, [status('success', '2026-10-18T00:05:00Z')]);
    throw new Error(`unexpected ${url}`);
  }});
  const result = await client.readLatestSuccessfulProductionDeployment();
  assert.equal(result.deployment.sha, sha);
  assert.equal(result.status.state, 'success');
  assert.equal(result.identity.prepareReceiptDigest, identity.prepareReceiptDigest);
  assert.deepEqual(result.identity.imageDigests, identity.imageDigests);
});

test('verified V5 parent lookup rejects absent or non-successful Deployments', async () => {
  for (const state of ['pending', 'failure']) {
    const deployment = validDeployment(23, sha, identity, '2026-10-18T00:00:00Z');
    const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
      if (url.includes('/deployments?')) return response(200, [deployment]);
      if (url.endsWith('/deployments/23')) return response(200, deployment);
      if (url.includes('/deployments/23/statuses')) return response(200, [status(state, '2026-10-18T00:05:00Z')]);
      throw new Error(`unexpected ${url}`);
    }});
    await assert.rejects(client.readLatestSuccessfulProductionDeployment(), /pending|failure|failed outside/);
  }
});

test('verified V5 parent lookup rejects incomplete durable authorization provenance', async () => {
  const deployment = validDeployment(24, sha, identity, '2026-10-18T00:00:00Z');
  const payload = JSON.parse(deployment.payload);
  payload.authorization = {mode: 'TASK_SCOPED_AUTO', id: null};
  deployment.payload = JSON.stringify(payload);
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [deployment]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readLatestSuccessfulProductionDeployment(), /provenance payload is incomplete/);
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
