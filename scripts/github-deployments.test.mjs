import test from 'node:test';
import assert from 'node:assert/strict';
import {createGitHubDeploymentsClient, deploymentPayload, assertDeploymentMatchesLive,
  selectTrustedPrepareCheck, authorizationReconciliationForStatus} from './github-deployments.mjs';
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

test('trusted Prepare check selects the completed github-actions check from the receipt-bound run', () => {
  const check = {id: 117876296903, name: 'prepare', head_sha: sha, status: 'completed', conclusion: 'success',
    app: {slug: 'github-actions'},
    details_url: 'https://github.com/akhripunov/gpoint/actions/runs/39195064296/job/117876296903'};
  assert.equal(selectTrustedPrepareCheck([check], {candidateSha: sha, prepareRunId: '39195064296'}), check);
});

test('trusted Prepare check rejects a wrong run, application, SHA, state, conclusion, or URL', () => {
  const valid = {id: 100, name: 'prepare', head_sha: sha, status: 'completed', conclusion: 'success',
    app: {slug: 'github-actions'}, details_url: 'https://github.com/akhripunov/gpoint/actions/runs/39195064296/job/100'};
  const invalid = [
    {...valid, details_url: 'https://github.com/akhripunov/gpoint/actions/runs/39195064295/job/100'},
    {...valid, app: {slug: 'untrusted-app'}},
    {...valid, head_sha: 'b'.repeat(40)},
    {...valid, status: 'in_progress'},
    {...valid, conclusion: 'failure'},
    {...valid, details_url: 'https://github.com.evil/akhripunov/gpoint/actions/runs/39195064296/job/100'},
    {...valid, details_url: 'malformed'},
  ];
  for (const check of invalid) {
    assert.throws(() => selectTrustedPrepareCheck([check], {candidateSha: sha, prepareRunId: '39195064296'}));
  }
});

test('trusted Prepare check rejects absent and ambiguous named contexts', () => {
  const check = {id: 100, name: 'prepare', head_sha: sha, status: 'completed', conclusion: 'success',
    app: {slug: 'github-actions'}, details_url: 'https://github.com/akhripunov/gpoint/actions/runs/39195064296/job/100'};
  assert.throws(() => selectTrustedPrepareCheck([], {candidateSha: sha, prepareRunId: '39195064296'}));
  assert.throws(() => selectTrustedPrepareCheck([check, {...check, id: 101}],
    {candidateSha: sha, prepareRunId: '39195064296'}));
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
  const result = await readbackClient.beginProductionDeployment(identity, {requiredContexts: ['prepare']});
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
  await assert.rejects(client.beginProductionDeployment(rerun, {requiredContexts: ['prepare']}),
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
  await client.beginProductionDeployment(identity, {requiredContexts: ['prepare']});
  assert.equal(createBody.ref, sha);
  assert.equal(createBody.environment, 'production');
  assert.equal(createBody.auto_merge, false);
  assert.equal(createBody.production_environment, true);
  assert.equal(createBody.transient_environment, false);
  assert.deepEqual(createBody.required_contexts, ['prepare']);
});

test('an absent ambiguous-create readback never triggers a second create POST', async () => {
  let creates = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async (url, init) => {
    if (url.includes('/deployments?')) return response(200, []);
    if (url.endsWith('/deployments') && init.method === 'POST') { creates += 1; throw new Error('timed out'); }
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.beginProductionDeployment(identity, {requiredContexts: ['prepare']}), /POST request failed/);
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
  await assert.rejects(client.beginProductionDeployment(identity, {requiredContexts: ['prepare']}), /payload read-back does not match/);
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

test('exact-SHA parent lookup fails closed when more than one successful Deployment matches', async () => {
  const first = validDeployment(31, sha, {...identity, runId: '51', logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/51/attempts/2'}, '2026-10-17T00:00:00Z');
  const second = validDeployment(32, sha, {...identity, runId: '52', logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/52/attempts/2'}, '2026-10-18T00:00:00Z');
  const rows = [second, first];
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, rows);
    const id = url.match(/deployments\/(31|32)/)?.[1];
    if (url.endsWith(`/deployments/${id}`)) return response(200, rows.find(item => String(item.id) === id));
    if (url.includes(`/deployments/${id}/statuses?`)) {
      const deployment = rows.find(item => String(item.id) === id);
      const provenance = JSON.parse(deployment.payload);
      return response(200, [{state: 'success',
        created_at: id === '31' ? '2026-10-17T00:01:00Z' : '2026-10-18T00:01:00Z',
        description: `stage=complete sha=${sha.slice(0, 12)} run=${provenance.workflow.runId}/${provenance.workflow.attempt}`,
        log_url: provenance.workflow.url}]);
    }
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'), /ambiguous/);
});

test('exact-SHA parent lookup treats an accessible empty task history as no successful exact Deployment', async () => {
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, []);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'),
    /^DeploymentApiError: No successful FULL_RELEASE Deployment exists for the exact SHA$/);
});

test('exact-SHA parent lookup skips safe historical records for other SHAs and strictly verifies the match', async () => {
  const oldOne = {...validDeployment(61, 'b'.repeat(40), identity, '2026-10-16T00:00:00Z'), payload: '{legacy-one}'};
  const oldTwo = {...validDeployment(62, 'c'.repeat(40), identity, '2026-10-17T00:00:00Z'), payload: '{legacy-two}'};
  const current = validDeployment(63, sha, identity, '2026-10-18T00:00:00Z');
  const rows = [current, oldTwo, oldOne];
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, rows);
    const id = url.match(/deployments\/(61|62|63)/)?.[1];
    const deployment = rows.find(item => String(item.id) === id);
    if (url.endsWith(`/deployments/${id}`)) return response(200, deployment);
    if (url.includes(`/deployments/${id}/statuses?`)) {
      return response(200, [status('success', `${deployment.created_at.slice(0, 10)}T00:05:00Z`,
        `stage=complete sha=${sha.slice(0, 12)} run=42/2`)]);
    }
    throw new Error(`unexpected ${url}`);
  }});
  const verified = await client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE');
  assert.equal(verified.deployment.id, 63);
  assert.equal(verified.identity.candidateSha, sha);
});

test('exact-SHA parent lookup still rejects legacy provenance on the candidate record', async () => {
  const current = {...validDeployment(64, sha, identity, '2026-10-18T00:00:00Z'), payload: '{legacy-current}'};
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [current]);
    if (url.includes('/deployments/64/statuses?')) return response(200, [status('success', '2026-10-18T00:05:00Z')]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'),
    /provenance payload is incomplete or mismatched/);
});

test('exact-SHA parent lookup keeps API and unresolved Deployment failures blocking', async () => {
  const unavailable = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async () =>
    response(403, {message: 'forbidden'})});
  await assert.rejects(unavailable.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'),
    error => error.status === 403);

  const active = validDeployment(65, sha, identity, '2026-10-18T00:00:00Z');
  const unresolved = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [active]);
    if (url.includes('/deployments/65/statuses?')) return response(200, [status('in_progress', '2026-10-18T00:05:00Z')]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(unresolved.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'), /in_progress/);
});

test('parent lookup ignores only its exactly verified in-progress Apply Deployment and read-backs no-success', async () => {
  const ownIdentity = {...identity, candidateSha: 'd'.repeat(40), runId: '88', runAttempt: '3',
    prepareRunId: '80', prepareRunAttempt: '1', route: 'FULL_RELEASE', authorizationId: null,
    logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/88/attempts/3'};
  const own = validDeployment(71, ownIdentity.candidateSha, ownIdentity, '2026-10-19T00:00:00Z');
  const rows = [own];
  let listCalls = 0;
  let statusCalls = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) { listCalls += 1; return response(200, rows); }
    if (url.endsWith('/deployments/71')) return response(200, own);
    if (url.includes('/deployments/71/statuses?')) { statusCalls += 1; return response(200, [{state: 'in_progress',
      description: `stage=pre_apply sha=${ownIdentity.candidateSha.slice(0, 12)} run=88/3`,
      log_url: ownIdentity.logUrl, created_at: '2026-10-19T00:01:00Z'}]); }
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE'), /in_progress/);
  listCalls = 0;
  statusCalls = 0;
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}),
  /No successful FULL_RELEASE Deployment exists for the exact SHA/);
  assert.equal(listCalls, 2);
  assert.equal(statusCalls, 2);
  await assert.rejects(client.readProductionBaseline(), /in_progress/);
});

test('parent lookup verifies a full or static current-attempt identity before excluding it', async () => {
  const staticIdentity = {...identity, candidateSha: 'f'.repeat(40), runId: '90', runAttempt: '4',
    prepareRunId: '80', prepareRunAttempt: '1', route: 'STATIC_UI_ONLY', authorizationId: null,
    logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/90/attempts/4',
    staticArtifactDigest: `sha256:${'6'.repeat(64)}`, staticArtifactContentSha256: '7'.repeat(64)};
  const own = validDeployment(74, staticIdentity.candidateSha, staticIdentity, '2026-10-19T00:00:00Z');
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [own]);
    if (url.endsWith('/deployments/74')) return response(200, own);
    if (url.includes('/deployments/74/statuses?')) return response(200, [{state: 'in_progress',
      description: `stage=pre_apply sha=${staticIdentity.candidateSha.slice(0, 12)} run=90/4`,
      log_url: staticIdentity.logUrl, created_at: '2026-10-19T00:01:00Z'}]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: staticIdentity}), /No successful FULL_RELEASE Deployment exists/);
});

test('parent lookup rejects forged current-attempt identity and unrelated active Deployments', async () => {
  const ownIdentity = {...identity, candidateSha: 'd'.repeat(40), runId: '88', runAttempt: '3',
    prepareRunId: '80', prepareRunAttempt: '1', route: 'FULL_RELEASE', authorizationId: null,
    logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/88/attempts/3'};
  const own = validDeployment(72, ownIdentity.candidateSha, ownIdentity, '2026-10-19T00:00:00Z');
  const otherIdentity = {...identity, candidateSha: 'e'.repeat(40), runId: '89', runAttempt: '1',
    logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/89/attempts/1'};
  const other = validDeployment(73, otherIdentity.candidateSha, otherIdentity, '2026-10-19T00:02:00Z');
  let rows = [own];
  let ownDescription = `stage=pre_apply sha=${ownIdentity.candidateSha.slice(0, 12)} run=88/3`;
  let ownLogUrl = ownIdentity.logUrl;
  let ownReadback = own;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, rows);
    const deploymentId = url.match(/\/deployments\/(72|73)$/)?.[1];
    if (deploymentId) return response(200, deploymentId === '72' ? ownReadback : other);
    const id = url.match(/deployments\/(72|73)\/statuses/)?.[1];
    if (id) {
      const value = id === '72' ? ownIdentity : otherIdentity;
      const state = id === '72' ? 'in_progress' : 'queued';
      return response(200, [{state, description: id === '72' ? ownDescription :
        `stage=pre_apply sha=${value.candidateSha.slice(0, 12)} run=${value.runId}/${value.runAttempt}`,
      log_url: id === '72' ? ownLogUrl : value.logUrl, created_at: '2026-10-19T00:03:00Z'}]);
    }
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: {...ownIdentity, prepareReceiptSha256: 'f'.repeat(64)}}), /identity changed/);
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: 999, identity: ownIdentity}), /missing or ambiguous/);
  ownDescription = `stage=post_apply sha=${ownIdentity.candidateSha.slice(0, 12)} run=88/3`;
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), /exact pre-Apply in-progress state/);
  ownDescription = `stage=pre_apply sha=${ownIdentity.candidateSha.slice(0, 12)} run=88/3`;
  ownLogUrl = 'https://github.com/attacker/gpoint/actions/runs/88/attempts/3';
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), /exact pre-Apply in-progress state/);
  ownLogUrl = ownIdentity.logUrl;
  rows = [own];
  ownReadback = {...own, created_at: '2026-10-19T00:04:00Z'};
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), /ID read-back does not match/);
  ownReadback = own;
  let getFailure = true;
  const unavailableOwnReadback = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', fetchImpl: async url => {
    if (url.includes('/deployments?')) return response(200, [own]);
    if (url.endsWith('/deployments/72')) return response(getFailure ? 503 : 404, {message: 'unavailable'});
    if (url.includes('/deployments/72/statuses?')) return response(200, [{state: 'in_progress',
      description: ownDescription, log_url: ownLogUrl, created_at: '2026-10-19T00:03:00Z'}]);
    throw new Error(`unexpected ${url}`);
  }});
  await assert.rejects(unavailableOwnReadback.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), error => error.status === 503);
  getFailure = false;
  await assert.rejects(unavailableOwnReadback.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), error => error.status === 404);
  rows = [own, other];
  await assert.rejects(client.readSuccessfulProductionDeploymentForSha(sha, 'FULL_RELEASE',
    {deploymentId: own.id, identity: ownIdentity}), /queued/);
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
    {requiredContexts: ['prepare']}), error => error.deployment?.id === 8);
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
    {requiredContexts: ['prepare']}), /GET request failed/);
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
    {requiredContexts: ['prepare']}), /HTTP 403/);
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
    {requiredContexts: ['prepare']}), /is in_progress/);
});


test('authorization reconciliation status binds the original attempt and a distinct metadata-only run', () => {
  const task = {...identity, authorizationMode: 'TASK_SCOPED_AUTO', authorizationId: 'gpoint-reconcile-test-001'};
  const description = 'stage=complete sha=' + sha.slice(0, 12) + ' run=42/2';
  const ordinary = {state: 'success', description, log_url: identity.logUrl};
  assert.equal(authorizationReconciliationForStatus(ordinary, task), null);
  assert.deepEqual(authorizationReconciliationForStatus({...ordinary,
    description: description + ' reconciled=90/1'}, task), {runId: '90', runAttempt: '1'});
  for (const status of [
    {...ordinary, state: 'error'},
    {...ordinary, description: description + ' reconciled=90/0'},
    {...ordinary, description: description + ' reconciled=42/3'},
    {...ordinary, description: description + ' reconciled=90/1 extra'},
    {...ordinary, description: description + ' reconciled=90/1', log_url: 'https://github.com/attacker/run'},
  ]) assert.throws(() => authorizationReconciliationForStatus(status, task));
  assert.throws(() => authorizationReconciliationForStatus({...ordinary,
    description: description + ' reconciled=90/1'}, identity));
});
