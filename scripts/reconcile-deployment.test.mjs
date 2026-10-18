import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {deploymentPayload} from './github-deployments.mjs';
import {fetchOriginalDeployEvidence, reconcileVerifiedDeployment, reconciledResultEvidence} from './reconcile-deployment.mjs';

const sha = 'a'.repeat(40);
const identity = {candidateSha: sha, runId: '42', runAttempt: '1', prepareRunId: '41',
  prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, prepareReceiptSha256: 'c'.repeat(64),
  imageDigests: {app: `sha256:${'1'.repeat(64)}`, agata: `sha256:${'2'.repeat(64)}`, gsales: `sha256:${'3'.repeat(64)}`}, authorizationMode: 'EXACT_SHA',
  logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/42/attempts/1'};
const deployment = {id: 7, environment: 'production', production_environment: true,
  transient_environment: false, task: 'gpoint-production-deploy', sha, ref: sha,
  payload: JSON.stringify(deploymentPayload(identity))};
const evidence = {candidateSha: sha, deploymentId: 7, deployRunId: '42', deployRunAttempt: '1',
  prepareRunId: '41', prepareRunAttempt: '1', status: 'COMPLETE', releaseState: 'COMPLETE',
  applicationVerification: 'VPS_READBACK_VERIFIED', dbHeadAfter: 'prisma_001', agataDbHeadAfter: 'agata0010',
  gsalesDbHeadAfter: '001_gsales', gsalesRuntime: {migrationSha256: 'c'.repeat(64)}, authorizationConsumed: true};
const prepareProof = {candidateSha: sha, runId: 41, runAttempt: 1, prismaHead: 'prisma_001',
  agataHead: 'agata0010', gsalesMigrationHead: '001_gsales', gsalesMigrationSha256: 'c'.repeat(64)};
const facts = [
  `LIVE_RELEASE_ENV_SHA=${sha}`, `LIVE_API_SHA=${sha}`, `LIVE_WORKER_SHA=${sha}`, `LIVE_BOT_SHA=${sha}`,
  `LIVE_MINIAPP_SHA=${sha}`, `LIVE_AGATA_API_SHA=${sha}`, `LIVE_AGATA_WORKER_SHA=${sha}`,
  'LIVE_API_RUNNING=true', 'LIVE_API_HEALTH=healthy', 'LIVE_WORKER_RUNNING=true', 'LIVE_BOT_RUNNING=true',
  `LIVE_API_IMAGE=sha256:${'1'.repeat(64)}`, `LIVE_WORKER_IMAGE=sha256:${'2'.repeat(64)}`,
  `LIVE_BOT_IMAGE=sha256:${'3'.repeat(64)}`, 'LIVE_AGATA_API_RUNNING=true', 'LIVE_AGATA_API_HEALTH=healthy',
  `LIVE_AGATA_API_IMAGE_ID=sha256:${'4'.repeat(64)}`, 'LIVE_AGATA_WORKER_RUNNING=true',
  'LIVE_AGATA_WORKER_HEALTH=healthy', `LIVE_AGATA_WORKER_IMAGE_ID=sha256:${'5'.repeat(64)}`,
  ...['API', 'AGATA_API', 'AGATA_MEMORY_WORKER'].flatMap(role => [
    `LIVE_GSALES_${role}_SHA=${sha}`, `LIVE_GSALES_${role}_RUNNING=true`,
    `LIVE_GSALES_${role}_HEALTH=healthy`, `LIVE_GSALES_${role}_IMAGE_ID=sha256:${'6'.repeat(64)}`]),
  'LIVE_PRISMA_HEAD=prisma_001', 'LIVE_AGATA_HEAD=agata0010', 'LIVE_GSALES_HEAD=001_gsales',
  `LIVE_GSALES_MIGRATION_SHA256=${'c'.repeat(64)}`,
].join('\n');

function setup({deployEvidence = evidence, deployAttempt = '1', statusState = 'in_progress', deployConclusion = 'success'} = {}) {
  const writes = [];
  const client = {
    getDeployment: async id => { assert.equal(id, 7); return deployment; },
    listStatuses: async () => [{state: statusState,
      description: statusState === 'success' ? `stage=complete sha=${sha.slice(0, 12)} run=42/1` : 'stage=post_apply sha=aaaaaaaaaaaa',
      log_url: identity.logUrl,
      created_at: '2026-10-18T00:01:00Z'}],
    publishStatus: async (target, targetIdentity, status) => {
      writes.push({target, targetIdentity, status}); return {state: status.state};
    },
  };
  const fetchImpl = async url => {
    const data = url.includes('/releases/tags/') ? {id: 91, tag_name: identity.prepareReleaseTag, draft: false,
      assets: [{id: 92, name: 'bundle.zip', digest: identity.prepareAssetSha256}]} :
      url.includes('/actions/runs/41/attempts/') ? {head_sha: sha, path: '.github/workflows/gpoint-prepare.yml',
        status: 'completed', conclusion: 'success'} :
      {id: 42, run_attempt: Number(deployAttempt), head_sha: sha, path: '.github/workflows/gpoint-deploy.yml',
        status: 'completed', conclusion: deployConclusion};
    return {ok: true, json: async () => data};
  };
  return {client, writes, fetchImpl, deployEvidence,
    inspectVps: () => facts};
}

test('durable Deploy evidence resolver verifies exact successful run and reads the OCI artifact by logged digest', async () => {
  const deployDigest = `sha256:${'d'.repeat(64)}`;
  const durable = {...evidence, schema: 1, deploymentId: 7, deployRunId: '42', deployRunAttempt: '2',
    prepareRunId: '41', prepareRunAttempt: '1', ciReceipt: {ghcrManifestDigest: identity.prepareReceiptDigest,
      prepareReceiptSha256: 'f'.repeat(64), prepareReceiptJson: '{"synthetic":true}'}};
  const calls = [];
  const fetchImpl = async url => {
    if (url.endsWith('/actions/runs/42/attempts/2')) return {ok: true, json: async () => ({id: 42,
      run_attempt: 2, head_sha: sha, path: '.github/workflows/gpoint-deploy.yml', status: 'completed',
      conclusion: 'success', event: 'workflow_dispatch', head_branch: 'main'})};
    if (url.endsWith('/actions/runs/42/attempts/2/jobs?per_page=100')) return {ok: true,
      json: async () => ({jobs: [{id: 102, name: 'deploy', run_attempt: 2, status: 'completed',
        conclusion: 'success', steps: [{name: 'Publish deployment result as a GHCR OCI artifact',
          status: 'completed', conclusion: 'success'}]}]})};
    if (url.endsWith('/actions/jobs/102/logs')) return {ok: true,
      arrayBuffer: async () => Buffer.from('synthetic publisher logs')};
    if (url.endsWith('/actions/runs/41/attempts/1')) return {ok: true, json: async () => ({id: 41,
      run_attempt: 1, head_sha: sha, path: '.github/workflows/gpoint-prepare.yml', status: 'completed',
      conclusion: 'success', event: 'push', head_branch: 'main'})};
    throw new Error(`unexpected ${url}`);
  };
  const execFile = (args, options = {}) => {
    calls.push(args);
    if (args[0] === 'oras' && args[1] === 'resolve') return `${deployDigest}\n`;
    if (args[0] === 'unzip' && args[1] === '-Z1') return 'publisher.log\n';
    if (args[0] === 'unzip' && args[1] === '-p') return `DEPLOY_EVIDENCE_DIGEST=${deployDigest}\n`;
    if (args[0] === 'oras' && args[1] === 'pull') {
      assert.equal(args[2], `ghcr.io/akhripunov/gpoint-deploy-evidence@${deployDigest}`);
      const output = args[args.indexOf('-o') + 1];
      fs.writeFileSync(`${output}/gpoint-release-result-42-2.json`, JSON.stringify(durable));
      return '';
    }
    assert.fail(`unexpected subprocess ${args.join(' ')}`);
  };
  const result = await fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
    deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
    prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl, execFile});
  assert.equal(result.evidenceManifestDigest, deployDigest);
  assert.equal(result.evidenceContentSha256.length, 64);
  assert.ok(calls.some(args => args[0] === 'oras' && args[1] === 'pull'
    && args[2] === `ghcr.io/akhripunov/gpoint-deploy-evidence@${deployDigest}`));
});

test('durable Deploy evidence resolver rejects a success artifact with a failed Actions run', async () => {
  let orasTouched = false;
  await assert.rejects(fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
    deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1', prepareReceiptDigest: identity.prepareReceiptDigest,
    token: 'test', fetchImpl: async () => ({ok: true, json: async () => ({id: 42, run_attempt: 2,
      head_sha: sha, path: '.github/workflows/gpoint-deploy.yml', status: 'in_progress', conclusion: null,
      event: 'workflow_dispatch', head_branch: 'main'})}), execFile: () => { orasTouched = true; return ''; }}),
  /not completed successfully on main/);
  assert.equal(orasTouched, false);
});

test('reconciles only the exact original Deployment after original and fresh VPS evidence agree', async () => {
  const ctx = setup();
  const result = await reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof, fetchImpl: ctx.fetchImpl,
    inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence});
  assert.equal(result.published, true);
  assert.equal(ctx.writes.length, 1);
  assert.equal(ctx.writes[0].target.id, 7);
  assert.equal(ctx.writes[0].status.state, 'success');
});

test('refuses to reconcile when the original Deploy artifact does not prove a verified install', async () => {
  const ctx = setup({deployEvidence: {...evidence, applicationVerification: 'NOT_VERIFIED'}});
  await assert.rejects(reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof, fetchImpl: ctx.fetchImpl,
    inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence}), /does not prove a verified COMPLETE installation/);
  assert.equal(ctx.writes.length, 0);
});

test('refuses a successful Deployment status when its exact Deploy Actions attempt did not complete successfully', async () => {
  const ctx = setup();
  const fetchImpl = async (url, init) => {
    const response = await ctx.fetchImpl(url, init);
    const data = await response.json();
    if (url.includes('/actions/runs/42/attempts/1')) data.conclusion = 'failure';
    return {...response, json: async () => data};
  };
  await assert.rejects(reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof,
    fetchImpl, inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence}),
  /does not prove a verified COMPLETE installation/);
  assert.equal(ctx.writes.length, 0);
});

test('accepts only exact verified installation with final-status uncertainty for later metadata reconciliation', async () => {
  const statusUnconfirmed = {...evidence, status: 'INSTALLATION_UNCONFIRMED',
    releaseState: 'INSTALLATION_UNCONFIRMED', executionFailure: {stage: 'STATUS_UNCONFIRMED'},
    applicationVerification: 'VPS_READBACK_VERIFIED'};
  const ctx = setup({deployEvidence: statusUnconfirmed, deployConclusion: 'failure'});
  const result = await reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof, fetchImpl: ctx.fetchImpl,
    inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence});
  assert.equal(result.evidenceVerified, true);
  assert.equal(result.liveVerified, true);
  assert.equal(result.published, true);
  assert.equal(ctx.writes[0].status.state, 'success');
});

test('does not reconcile installation uncertainty caused by any stage except final status publication', async () => {
  const statusUnconfirmed = {...evidence, status: 'INSTALLATION_UNCONFIRMED',
    releaseState: 'INSTALLATION_UNCONFIRMED', executionFailure: {stage: 'POST_APPLY_READBACK_OR_STATUS'},
    applicationVerification: 'VPS_READBACK_VERIFIED'};
  const ctx = setup({deployEvidence: statusUnconfirmed});
  await assert.rejects(reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof, fetchImpl: ctx.fetchImpl,
    inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence}), /does not prove a verified COMPLETE installation/);
  assert.equal(ctx.writes.length, 0);
});

test('fresh verification treats an already successful exact status as idempotently reconciled', async () => {
  const ctx = setup({statusState: 'success'});
  const result = await reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof,
    fetchImpl: ctx.fetchImpl, inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence});
  assert.equal(result.published, false);
  assert.equal(ctx.writes.length, 0);
});

test('refuses an attempt or candidate mismatch without publishing any status', async () => {
  const ctx = setup({deployAttempt: '2'});
  await assert.rejects(reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: 'd'.repeat(40), prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof, fetchImpl: ctx.fetchImpl,
    inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence}), /does not match the exact candidate/);
  assert.equal(ctx.writes.length, 0);
});

test('conflicting equal-time statuses block metadata reconciliation', async () => {
  const ctx = setup();
  ctx.client.listStatuses = async () => [
    {state: 'in_progress', description: 'stage=post_apply', created_at: '2026-10-18T00:01:00Z'},
    {state: 'error', description: 'stage=post_apply', created_at: '2026-10-18T00:01:00Z'},
  ];
  await assert.rejects(reconcileVerifiedDeployment({client: ctx.client, deploymentId: 7,
    runId: '42', currentRunAttempt: '2', candidateSha: sha, prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, token: 'test', prepareProof,
    fetchImpl: ctx.fetchImpl, inspectVps: ctx.inspectVps, readDeployEvidence: async () => ctx.deployEvidence}),
  /conflicting statuses at the latest timestamp/);
  assert.equal(ctx.writes.length, 0);
});

test('moves an original status failure under reconciledFrom when producing a verified result', () => {
  const originalFailure = {stage: 'STATUS_UNCONFIRMED', diagnostic: 'write outcome unknown'};
  const value = reconciledResultEvidence({...evidence, status: 'INSTALLATION_UNCONFIRMED',
    releaseState: 'INSTALLATION_UNCONFIRMED', executionFailure: originalFailure},
  {status: 'COMPLETE', releaseState: 'COMPLETE', reconciliation: {liveVerified: true}});
  assert.equal(value.status, 'COMPLETE');
  assert.equal(value.releaseState, 'COMPLETE');
  assert.equal(Object.hasOwn(value, 'executionFailure'), false);
  assert.deepEqual(value.reconciledFrom, {status: 'INSTALLATION_UNCONFIRMED',
    releaseState: 'INSTALLATION_UNCONFIRMED', executionFailure: originalFailure});
});
