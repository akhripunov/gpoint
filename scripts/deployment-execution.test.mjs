import test from 'node:test';
import assert from 'node:assert/strict';
import {executeProductionDeployment, deploymentResultStatus} from './deployment-execution.mjs';
import {createGitHubDeploymentsClient, deploymentPayload} from './github-deployments.mjs';

const sha = 'a'.repeat(40);
const identity = {candidateSha: sha, runId: '42', runAttempt: '2'};
const logUrl = 'https://github.com/akhripunov/gpoint/actions/runs/42/attempts/2';

function harness({applyStatus = 0, applyError, inspectError, verifyError,
  createError, successStatusError, afterVerifiedError} = {}) {
  const events = [];
  const deployment = {id: 73};
  const fixture = {runtimeSha: 'b'.repeat(40), dbRows: ['customer-1', 'invoice-2'],
    config: {databaseUrl: 'postgres://existing', workerToken: 'kept'}, volumes: ['postgres-data']};
  const dependencies = {
    createDeployment: async () => {
      events.push('create');
      if (createError) throw Object.assign(new Error(createError), {deployment});
      return deployment;
    },
    apply: async () => {
      events.push('apply');
      if (applyError) throw new Error(applyError);
      fixture.runtimeSha = sha;
      return {status: applyStatus};
    },
    inspectVps: async () => {
      events.push('inspect');
      if (inspectError) throw new Error(inspectError);
      return {...fixture};
    },
    verifyInstalled: async live => {
      events.push('verify');
      if (verifyError) throw new Error(verifyError);
      assert.equal(live.runtimeSha, sha);
    },
    afterVerified: async () => {
      events.push('afterVerified');
      if (afterVerifiedError) throw new Error(afterVerifiedError);
    },
    publishStatus: async (target, targetIdentity, status) => {
      events.push(`status:${status.state}`);
      assert.equal(target.id, deployment.id);
      assert.equal(targetIdentity, identity);
      if (status.state === 'success' && successStatusError) throw new Error(successStatusError);
    },
    identity, candidateSha: sha, logUrl,
  };
  return {events, fixture, deployment, dependencies};
}

test('calls Apply once and publishes success only after fresh VPS verification', async () => {
  const ctx = harness();
  const result = await executeProductionDeployment(ctx.dependencies);
  assert.equal(result.releaseCode, 0);
  assert.equal(result.installationVerified, true);
  assert.equal(ctx.events.filter(event => event === 'apply').length, 1);
  assert.ok(ctx.events.indexOf('inspect') < ctx.events.indexOf('verify'));
  assert.ok(ctx.events.indexOf('verify') < ctx.events.indexOf('status:success'));
});

test('passes the exact created Deployment to the Apply callback', async () => {
  const ctx = harness();
  let received;
  await executeProductionDeployment({...ctx.dependencies, apply: async deployment => {
    received = deployment;
    return {status: 0};
  }});
  assert.equal(received, ctx.deployment);
});

test('pre-Apply Deployment failure records failure and never invokes Apply', async () => {
  const ctx = harness({createError: 'initial status could not be confirmed'});
  const result = await executeProductionDeployment(ctx.dependencies);
  assert.equal(result.applyStarted, false);
  assert.equal(ctx.events.includes('apply'), false);
  assert.equal(ctx.events.includes('inspect'), false);
  assert.ok(ctx.events.includes('status:failure'));
  assert.equal(ctx.events.includes('status:success'), false);
});

test('nonzero Apply is inspected and reports error without retrying Apply', async () => {
  const ctx = harness({applyStatus: 1});
  const result = await executeProductionDeployment(ctx.dependencies);
  assert.equal(result.releaseCode, 1);
  assert.equal(ctx.events.filter(event => event === 'apply').length, 1);
  assert.ok(ctx.events.includes('inspect'));
  assert.ok(ctx.events.includes('status:error'));
  assert.equal(ctx.events.includes('status:success'), false);
});

test('successful Apply with mismatched or unavailable VPS readback cannot publish success', async t => {
  for (const options of [{verifyError: 'VPS SHA mismatch'}, {inspectError: 'VPS unavailable'}]) {
    await t.test(options.verifyError ?? options.inspectError, async () => {
      const ctx = harness(options);
      const result = await executeProductionDeployment(ctx.dependencies);
      assert.equal(result.releaseCode, 1);
      assert.equal(ctx.events.filter(event => event === 'apply').length, 1);
      assert.ok(ctx.events.includes('status:error'));
      assert.equal(ctx.events.includes('status:success'), false);
      assert.equal(deploymentResultStatus({evidenceStatus: 'COMPLETE', mode: 'production',
        deploymentExists: true, installationVerified: result.installationVerified,
        successStatusConfirmed: false}), 'INSTALLATION_UNCONFIRMED');
    });
  }
});

test('status timeout after verified install stays unconfirmed without contradictory error status', async () => {
  const ctx = harness({successStatusError: 'status remains unconfirmed'});
  const result = await executeProductionDeployment(ctx.dependencies);
  assert.equal(result.installationVerified, true);
  assert.equal(result.releaseCode, 1);
  assert.equal(result.failure.stage, 'STATUS_UNCONFIRMED');
  assert.deepEqual(ctx.events.filter(event => event.startsWith('status:')), ['status:success']);
  assert.equal(deploymentResultStatus({evidenceStatus: 'COMPLETE', mode: 'production', deploymentExists: true,
    installationVerified: true, successStatusConfirmed: false}), 'INSTALLATION_UNCONFIRMED');
  assert.equal(deploymentResultStatus({evidenceStatus: 'COMPLETE', mode: 'production', deploymentExists: true,
    installationVerified: true, successStatusConfirmed: true}), 'COMPLETE');
});

test('accepted success-status timeout is reconciled on the same Deployment without another Apply', async () => {
  const fullIdentity = {...identity, prepareRunId: '41', prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`,
    prepareReceiptSha256: 'c'.repeat(64), imageDigests: {app: `sha256:${'1'.repeat(64)}`, agata: `sha256:${'2'.repeat(64)}`, gsales: `sha256:${'3'.repeat(64)}`}, authorizationMode: 'EXACT_SHA', logUrl};
  const deployment = {id: 73, environment: 'production', production_environment: true,
    transient_environment: false, task: 'gpoint-production-deploy', sha, ref: sha,
    payload: JSON.stringify(deploymentPayload(fullIdentity))};
  let storedStatus;
  let statusPosts = 0;
  const client = createGitHubDeploymentsClient({token: 'mock', apiBase: 'https://mock', sleep: async () => {},
    fetchImpl: async (url, init) => {
      if (url.includes('/statuses?')) return {status: 200, ok: true, text: async () => JSON.stringify(storedStatus ? [storedStatus] : [])};
      if (url.endsWith('/statuses') && init.method === 'POST') {
        statusPosts += 1;
        storedStatus = {...JSON.parse(init.body), created_at: '2026-10-18T00:01:00Z'};
        throw new Error('accepted, response timed out');
      }
      throw new Error(`unexpected ${url}`);
    }});
  let applies = 0;
  const result = await executeProductionDeployment({
    createDeployment: async () => deployment,
    apply: async () => { applies += 1; return {status: 0}; },
    inspectVps: async () => ({runtimeSha: sha}),
    verifyInstalled: async live => assert.equal(live.runtimeSha, sha),
    publishStatus: (target, targetIdentity, status) => client.publishStatus(target, targetIdentity, status),
    identity: fullIdentity, candidateSha: sha, logUrl,
  });
  assert.equal(result.releaseCode, 0);
  assert.equal(applies, 1);
  assert.equal(statusPosts, 1);
});

test('simulated Apply preserves existing database, config and volume sentinels', async () => {
  const ctx = harness();
  const before = {dbRows: [...ctx.fixture.dbRows], config: {...ctx.fixture.config}, volumes: [...ctx.fixture.volumes]};
  await executeProductionDeployment(ctx.dependencies);
  assert.deepEqual(ctx.fixture.dbRows, before.dbRows);
  assert.deepEqual(ctx.fixture.config, before.config);
  assert.deepEqual(ctx.fixture.volumes, before.volumes);
});

test('failed independent inspection after failed Apply records error without another Apply', async () => {
  const ctx = harness({applyStatus: 1, inspectError: 'VPS unavailable'});
  const result = await executeProductionDeployment(ctx.dependencies);
  assert.equal(result.releaseCode, 1);
  assert.equal(ctx.events.filter(event => event === 'apply').length, 1);
  assert.ok(ctx.events.includes('status:error'));
  assert.equal(ctx.events.includes('status:success'), false);
});
