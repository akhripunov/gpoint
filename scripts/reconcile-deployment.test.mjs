import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {deploymentPayload} from './github-deployments.mjs';
import {assertCandidateExecutionScope, fetchAuthorizationReconciliationEvidence, fetchOriginalDeployEvidence,
  finalizeAuthorizationConsumption, reconcileVerifiedDeployment, reconciledResultEvidence,
  ReconciliationGateError, safeReconciliationFailure,
  validateAuthorizationConsumption} from './reconcile-deployment.mjs';

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
  let publisherLogBytes = Buffer.from(`2026-10-19T04:18:11.1234567Z DEPLOY_EVIDENCE_DIGEST=${deployDigest}\n`);
  let publisherBody = null;
  const calls = [];
  const fetchImpl = async url => {
    if (url.endsWith('/actions/runs/42/attempts/2')) return {ok: true, json: async () => ({id: 42,
      run_attempt: 2, head_sha: sha, path: '.github/workflows/gpoint-deploy.yml', status: 'completed',
      conclusion: 'success', event: 'workflow_dispatch', head_branch: 'main'})};
    if (url.endsWith('/actions/runs/42/attempts/2/jobs?per_page=100')) return {ok: true,
      json: async () => ({jobs: [{id: 102, name: 'deploy', run_id: 42, head_sha: sha,
        run_attempt: 2, status: 'completed',
        conclusion: 'success', steps: [{name: 'Publish deployment result as a GHCR OCI artifact',
          status: 'completed', conclusion: 'success'}]}]})};
    if (url.endsWith('/actions/jobs/102/logs')) return publisherBody
      ? {ok: true, body: publisherBody}
      : {ok: true, arrayBuffer: async () => publisherLogBytes};
    if (url.endsWith('/actions/runs/41/attempts/1')) return {ok: true, json: async () => ({id: 41,
      run_attempt: 1, head_sha: sha, path: '.github/workflows/gpoint-prepare.yml', status: 'completed',
      conclusion: 'success', event: 'push', head_branch: 'main'})};
    throw new Error(`unexpected ${url}`);
  };
  const configPaths = [];
  const registryUsername = 'synthetic-reader';
  const registryToken = 'synthetic-ghcr-token';
  const execFile = (file, args, options = {}) => {
    assert.equal(typeof file, 'string');
    calls.push([file, ...args]);
    if (file === 'oras') {
      const config = args[args.indexOf('--registry-config') + 1];
      configPaths.push(config);
      assert.ok(config);
      assert.equal(fs.statSync(path.dirname(config)).mode & 0o777, 0o700);
      assert.equal(fs.statSync(config).mode & 0o777, 0o600);
      const body = JSON.parse(fs.readFileSync(config, 'utf8'));
      assert.equal(body.auths['ghcr.io'].auth,
        Buffer.from(`${registryUsername}:${registryToken}`).toString('base64'));
      assert.equal(JSON.stringify(args).includes(registryToken), false);
    }
    if (file === 'oras' && args[0] === 'resolve') return `${deployDigest}\n`;
    if (file === 'oras' && args[0] === 'pull') {
      assert.equal(args[args.indexOf(args.find(value => value.startsWith('ghcr.io/akhripunov/gpoint-deploy-evidence@')))],
        `ghcr.io/akhripunov/gpoint-deploy-evidence@${deployDigest}`);
      const output = args[args.indexOf('-o') + 1];
      fs.writeFileSync(`${output}/gpoint-release-result-42-2.json`, JSON.stringify(durable));
      return '';
    }
    if (file === 'unzip') return execFileSync(file, args, options);
    assert.fail(`unexpected subprocess ${args.join(' ')}`);
  };
  const result = await fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
    deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
    prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl, execFile,
    registryUsername, registryToken});
  assert.equal(result.evidenceManifestDigest, deployDigest);
  assert.equal(result.evidenceContentSha256.length, 64);
  assert.ok(calls.some(args => args[0] === 'oras' && args[1] === 'pull'
    && args.includes(`ghcr.io/akhripunov/gpoint-deploy-evidence@${deployDigest}`)));
  assert.ok(calls.every(args => args[0] !== 'unzip'));
  assert.ok(configPaths.length === 2 && new Set(configPaths).size === 1);
  assert.equal(fs.existsSync(configPaths[0]), false);

  for (const invalidLogs of [
    Buffer.from(`2026-10-19T04:18:11.1234567Z DEPLOY_EVIDENCE_DIGEST=${deployDigest}\nDEPLOY_EVIDENCE_DIGEST=${deployDigest}\n`),
    Buffer.from(`DEPLOY_EVIDENCE_DIGEST=${deployDigest}\n DEPLOY_EVIDENCE_DIGEST=sha256:${'e'.repeat(64)}\n`),
    Buffer.from('2026-10-19T04:18:11.1234567Z DEPLOY_EVIDENCE_DIGEST=sha256:abcd\n'),
    Buffer.from([0xff, 0xfe, 0xfd]),
    Buffer.from([0x50, 0x4b, 0x03, 0x04, ...Buffer.from('truncated archive')]),
  ]) {
    publisherLogBytes = invalidLogs;
    await assert.rejects(fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
      deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
      prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl, execFile,
      registryUsername, registryToken}));
  }

  const zipBytes = execFileSync('python3', ['-c',
    'import io,sys,zipfile; b=io.BytesIO(); z=zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED); z.writestr("1_deploy.txt",sys.stdin.buffer.read()); z.close(); sys.stdout.buffer.write(b.getvalue())',
  ], {input: Buffer.from(`2026-10-19T04:18:11.1234567Z DEPLOY_EVIDENCE_DIGEST=${deployDigest}\n`)});
  publisherLogBytes = zipBytes;
  const zipResult = await fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
    deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
    prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl, execFile,
    registryUsername, registryToken});
  assert.equal(zipResult.evidenceManifestDigest, deployDigest);

  let canceled = false;
  const chunks = [Buffer.alloc(25 * 1024 * 1024), Buffer.from('overflow')];
  publisherBody = {getReader: () => ({read: async () => chunks.length
    ? {done: false, value: chunks.shift()} : {done: true}, cancel: async () => { canceled = true; }})};
  await assert.rejects(fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
    deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
    prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl, execFile,
    registryUsername, registryToken}), /exceed limits/);
  assert.equal(canceled, true);

  publisherBody = null;
  publisherLogBytes = Buffer.from(`2026-10-19T04:18:11.1234567Z DEPLOY_EVIDENCE_DIGEST=${deployDigest}\n`);
  const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-fake-oras-'));
  const orasBin = path.join(binDir, 'oras');
  fs.writeFileSync(orasBin, `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
if (!args.includes('--registry-config')) process.exit(21);
const config = args[args.indexOf('--registry-config') + 1];
if (JSON.parse(fs.readFileSync(config, 'utf8')).auths['ghcr.io'].auth !==
    Buffer.from('${registryUsername}:${registryToken}').toString('base64')) process.exit(22);
if (args[0] === 'resolve') process.stdout.write('${deployDigest}\\n');
else if (args[0] === 'pull') fs.writeFileSync(path.join(args[args.indexOf('-o') + 1],
    'gpoint-release-result-42-2.json'), ${JSON.stringify(JSON.stringify(durable))});
else process.exit(23);
`, {mode: 0o700});
  fs.chmodSync(orasBin, 0o700);
  const previousPath = process.env.PATH;
  try {
    process.env.PATH = `${binDir}${path.delimiter}${previousPath ?? ''}`;
    const defaultRunnerResult = await fetchOriginalDeployEvidence({runId: '42', attempt: '2', candidateSha: sha,
      deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
      prepareReceiptDigest: identity.prepareReceiptDigest, token: 'test', fetchImpl,
      registryUsername, registryToken});
    assert.equal(defaultRunnerResult.evidenceManifestDigest, deployDigest);
  } finally {
    process.env.PATH = previousPath;
    fs.rmSync(binDir, {recursive: true, force: true});
  }
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

const executorSha = 'b'.repeat(40);
const authorizationId = 'gpoint-me42-final-v5-integration-20261018-010';
const registryRepos = {app: 'ghcr.io/akhripunov/gpoint-runtime', agata: 'ghcr.io/akhripunov/gpoint-agata-runtime',
  gsales: 'ghcr.io/akhripunov/gpoint-g-sales-runtime'};
function authorizationFinalizerFixture({statusState = 'error', statusDescription, marker = {claimed: true,
  revoked: false, consumed: false}, evidenceOverrides = {}, driftStatus = false,
  mainSha = executorSha, authorizationProof = null, inspectVpsOutput = null, envOverrides = {},
  otherDeployments = []} = {}) {
  const images = Object.fromEntries(['app', 'agata', 'gsales'].map((role, index) => [role, {
    repository: registryRepos[role], digest: `sha256:${String(index + 1).repeat(64)}`,
    configDigest: `sha256:${String(index + 4).repeat(64)}`, id: `sha256:${String(index + 4).repeat(64)}`,
    sourceSha: sha, platform: 'linux/amd64'}]));
  const receipt = {schema: 3, transport: 'ghcr-oci', status: 'READY', candidateSha: sha,
    runId: 41, runAttempt: 1, prismaHead: 'prisma_001', agataHead: 'agata0010',
    gsalesMigrationHead: '001_gsales', gsalesMigrationSha256: 'c'.repeat(64),
    images, deployRehearsal: {status: 'PASS', dockerVersion: '29.1.3', store: 'containerd',
      snapshotter: 'overlayfs', images: Object.fromEntries(Object.keys(images).map((role, index) => [role, {
        ...images[role], nativeId: `sha256:${String(index + 7).repeat(64)}`}]))}};
  const receiptJson = JSON.stringify(receipt);
  const receiptSha256 = createHash('sha256').update(receiptJson).digest('hex');
  const componentImages = Object.fromEntries(Object.keys(images).map(role => [role, {
    name: role, sourceSha: sha, imageId: images[role].configDigest,
    registryDigest: images[role].digest, platform: 'linux/amd64'}]));
  const proof = {recordSha256: '8'.repeat(64), ownerTaskCommitmentSha256: '9'.repeat(64),
    sourceInstructionIdentifier: 'OWNER_TASK_TEST_AUTHORIZATION', changedPaths: 'scripts/reconcile-deployment.mjs'};
  const originalEvidence = {schema: 1, candidateSha: sha, deploymentId: 7, deployRunId: '42', deployRunAttempt: '1',
    prepareRunId: '41', prepareRunAttempt: '1', prepareReceiptDigest: identity.prepareReceiptDigest,
    status: 'INSTALLATION_UNCONFIRMED', releaseState: 'INSTALLATION_UNCONFIRMED',
    executionFailure: {stage: 'POST_APPLY_READBACK', executionStatus: 'FAILED',
      diagnostic: 'Task authorization consumption failed (HTTP 403)'},
    authorizationMode: 'TASK_SCOPED_AUTO', authorizationId,
    authorizationConsumed: false, authorizationRecordSha256: proof.recordSha256,
    authorizationScopeSha256: proof.ownerTaskCommitmentSha256,
    authorizationSourceInstructionId: proof.sourceInstructionIdentifier,
    authorizationChangedPaths: proof.changedPaths, productionBaselineSha: 'd'.repeat(40),
    applicationVerification: 'VPS_READBACK_VERIFIED', serverDeployStatus: 'COMPLETE',
    ciReceipt: {ghcrManifestDigest: identity.prepareReceiptDigest, prepareReceiptSha256: receiptSha256,
      prepareReceiptJson: receiptJson, prismaHead: receipt.prismaHead, agataHead: receipt.agataHead,
      gsalesMigrationHead: receipt.gsalesMigrationHead, gsalesMigrationSha256: receipt.gsalesMigrationSha256},
    componentImages, dbHeadAfter: receipt.prismaHead, agataDbHeadAfter: receipt.agataHead,
    gsalesDbHeadAfter: receipt.gsalesMigrationHead, gsalesMigrationSha256After: receipt.gsalesMigrationSha256,
    workerOperationalStatus: 'HEALTHY', evidenceManifestDigest: `sha256:${'e'.repeat(64)}`,
    evidenceContentSha256: 'f'.repeat(64), ...evidenceOverrides};
  const taskIdentity = {...identity, authorizationMode: 'TASK_SCOPED_AUTO', authorizationId,
    route: 'FULL_RELEASE'};
  taskIdentity.prepareReceiptSha256 = receiptSha256;
  const taskDeployment = {...deployment, created_at: '2026-10-19T01:00:00Z',
    payload: JSON.stringify(deploymentPayload(taskIdentity))};
  let latestStatus = {state: statusState,
    description: statusDescription ?? (statusState === 'error'
      ? `stage=post_apply sha=${sha.slice(0, 12)} run=42/1`
      : `stage=complete sha=${sha.slice(0, 12)} run=42/1 reconciled=599/1`),
    log_url: identity.logUrl, environment_url: 'https://gpoint.one', created_at: '2026-10-19T01:00:00Z'};
  const writes = [];
  let statusReads = 0;
  const client = {
    getDeployment: async id => { assert.equal(id, 7); return taskDeployment; },
    listDeployments: async () => [taskDeployment, ...otherDeployments],
    listStatuses: async id => {
      if (id !== 7) return [{state: 'in_progress', created_at: '2026-10-19T00:00:00Z'}];
      statusReads += 1;
      if (driftStatus && statusReads === 2) return [{...latestStatus, description: 'stage=error changed'}];
      return [latestStatus];
    },
    publishStatus: async (target, targetIdentity, value) => {
      writes.push(value);
      latestStatus = {...value, log_url: value.logUrl, environment_url: 'https://gpoint.one',
        created_at: '2026-10-19T01:00:01Z'};
      return latestStatus;
    },
  };
  const factValues = facts.split('\n').map(line => {
    const replacements = {
      LIVE_API_IMAGE: componentImages.app.imageId,
      LIVE_WORKER_IMAGE: componentImages.app.imageId,
      LIVE_BOT_IMAGE: componentImages.app.imageId,
      LIVE_AGATA_API_IMAGE_ID: componentImages.agata.imageId,
      LIVE_AGATA_WORKER_IMAGE_ID: componentImages.agata.imageId,
      LIVE_GSALES_API_IMAGE_ID: componentImages.gsales.imageId,
      LIVE_GSALES_AGATA_API_IMAGE_ID: componentImages.gsales.imageId,
      LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID: componentImages.gsales.imageId,
    };
    const [key] = line.split('=');
    return key in replacements ? `${key}=${replacements[key]}` : line;
  });
  let inspection = 0;
  return {client, taskDeployment, originalEvidence, receipt, proof, writes, getStatus: () => latestStatus,
    acquireProductionLock: () => () => {},
    env: {GITHUB_ACTIONS: 'true', GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REPOSITORY: 'akhripunov/gpoint',
      OWNER_REPOSITORY: 'akhripunov', OWNER_APPROVAL_ACTOR: 'akhripunov', OWNER_TRIGGERING_ACTOR: 'akhripunov',
      GITHUB_REF: 'refs/heads/main', GITHUB_TOKEN: 'test-token', GITHUB_SHA: executorSha,
      GITHUB_RUN_ID: '600', GITHUB_RUN_ATTEMPT: '3', ...envOverrides},
    verifyAuthorization: async () => authorizationProof ?? ({proof, record: {allowed_paths: ['scripts/']}}),
    readAuthorizationState: async () => marker,
    currentMain: async () => mainSha,
    readDeployEvidence: async () => originalEvidence,
    inspectVps: () => {
      inspection += 1;
      return typeof inspectVpsOutput === 'function' ? inspectVpsOutput(inspection, factValues.join('\n'))
        : (inspectVpsOutput ?? factValues.join('\n'));
    },
    receiptValidator: () => receipt,
    statusReads: () => statusReads,
  };
}

test('authorization finalizer consumes only the exact marker and reconciles the same Deployment without Apply', async () => {
  const fixture = authorizationFinalizerFixture();
  let consumed = false;
  let consumeCalls = 0;
  let inspectionCalls = 0;
  const result = await finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
    assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
    readAuthorizationState: async () => ({claimed: true, revoked: false, consumed,
      ...(consumed ? {consumedSha: sha} : {})}),
    consumeAuthorization: async args => { consumeCalls += 1; assert.equal(args.candidateSha, sha); consumed = true; },
    checkAuthorization: async args => { assert.equal(args.consumedCandidateSha, sha); },
    currentMain: fixture.currentMain, readDeployEvidence: fixture.readDeployEvidence,
    inspectVps: () => { inspectionCalls += 1; return fixture.inspectVps(); },
    receiptValidator: fixture.receiptValidator, writeResult: false,
    acquireProductionLock: fixture.acquireProductionLock});
  assert.equal(result.operation, 'AUTHORIZATION_CONSUMPTION_RECONCILIATION');
  assert.equal(result.status, 'COMPLETE');
  assert.deepEqual(result.originalEvidence, {manifestDigest: fixture.originalEvidence.evidenceManifestDigest,
    contentSha256: fixture.originalEvidence.evidenceContentSha256, runId: '42', runAttempt: '1'});
  assert.deepEqual(result.reconciledFrom.executionFailure, fixture.originalEvidence.executionFailure);
  assert.equal(result.executorSha, executorSha);
  assert.equal(result.deploymentId, 7);
  assert.equal(consumeCalls, 1);
  assert.equal(inspectionCalls, 2);
  assert.equal(fixture.writes.length, 1);
  assert.match(fixture.writes[0].description, /reconciled=600\/3$/);
  assert.equal(fixture.writes[0].logUrl, identity.logUrl);
  assert.equal(Object.hasOwn(fixture.client, 'createDeployment'), false);
  assert.equal(Object.hasOwn(fixture.client, 'apply'), false);
});

test('authorization validation is read-only and emits original identities only after exact checks', async () => {
  const fixture = authorizationFinalizerFixture();
  const result = await validateAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
    assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
    readAuthorizationState: fixture.readAuthorizationState, currentMain: fixture.currentMain});
  assert.equal(result.identity.authorizationId, authorizationId);
  assert.equal(fixture.writes.length, 0);
});

test('pre-mutation authorization, identity, scope, receipt, status and VPS failures produce no Deployment writes', async () => {
  const cases = [
    ['wrong authorization marker', {marker: {claimed: true, revoked: false, consumed: true, consumedSha: 'c'.repeat(40)}}],
    ['revoked authorization', {marker: {claimed: true, revoked: true, consumed: false}}],
    ['wrong original HTTP failure', {evidenceOverrides: {executionFailure: {stage: 'POST_APPLY_READBACK',
      executionStatus: 'FAILED', diagnostic: 'Task authorization consumption failed (HTTP 500)'}}}],
    ['wrong current main', {mainSha: 'c'.repeat(40)}],
    ['wrong owner dispatch', {envOverrides: {OWNER_TRIGGERING_ACTOR: 'other'}}],
    ['non-main dispatch', {envOverrides: {GITHUB_REF: 'refs/heads/repair'}}],
    ['authorization proof mismatch', {authorizationProof: {proof: {recordSha256: '0'.repeat(64),
      ownerTaskCommitmentSha256: '9'.repeat(64), sourceInstructionIdentifier: 'OWNER_TASK_TEST_AUTHORIZATION'},
      record: {allowed_paths: ['scripts/']}}}],
    ['receipt content hash mismatch', {evidenceOverrides: {ciReceipt: {ghcrManifestDigest: identity.prepareReceiptDigest,
      prepareReceiptSha256: '0'.repeat(64), prepareReceiptJson: '{}'}}}],
    ['component image mismatch', {evidenceOverrides: {componentImages: {app: {sourceSha: 'c'.repeat(40),
      imageId: 'sha256:' + '1'.repeat(64), registryDigest: 'sha256:' + '1'.repeat(64), platform: 'linux/amd64'}}}}],
    ['database head mismatch', {evidenceOverrides: {dbHeadAfter: 'prisma_other'}}],
    ['VPS health mismatch', {inspectVpsOutput: (_count, output) => output.replace('LIVE_API_HEALTH=healthy', 'LIVE_API_HEALTH=unhealthy')}],
    ['worker not running', {inspectVpsOutput: (_count, output) => output.replace('LIVE_WORKER_RUNNING=true', 'LIVE_WORKER_RUNNING=false')}],
    ['live database drift', {inspectVpsOutput: (_count, output) => output.replace('LIVE_PRISMA_HEAD=prisma_001', 'LIVE_PRISMA_HEAD=prisma_other')}],
    ['wrong deployment status', {statusDescription: 'stage=pre_apply sha=' + sha.slice(0, 12) + ' run=42/1'}],
    ['deployment status drift', {driftStatus: true}],
  ];
  for (const [label, options] of cases) {
    const fixture = authorizationFinalizerFixture(options);
    let consumeCalls = 0;
    await assert.rejects(finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
      candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
      assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
      readAuthorizationState: async () => options.marker ?? {claimed: true, revoked: false,
        consumed: false}, currentMain: fixture.currentMain,
      consumeAuthorization: async () => { consumeCalls += 1; },
      readDeployEvidence: fixture.readDeployEvidence, inspectVps: fixture.inspectVps,
      receiptValidator: fixture.receiptValidator, writeResult: false,
      acquireProductionLock: fixture.acquireProductionLock}), label);
    assert.equal(consumeCalls, 0, label);
    assert.equal(fixture.writes.length, 0, label);
  }
});

test('exact already-consumed marker is idempotent and does not issue a second consume', async () => {
  const fixture = authorizationFinalizerFixture({marker: {claimed: true, revoked: false,
    consumed: true, consumedSha: sha}});
  let consumeCalls = 0;
  const result = await finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
    assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
    readAuthorizationState: fixture.readAuthorizationState,
    consumeAuthorization: async () => { consumeCalls += 1; }, checkAuthorization: async () => {},
    currentMain: fixture.currentMain, readDeployEvidence: fixture.readDeployEvidence,
    inspectVps: fixture.inspectVps, receiptValidator: fixture.receiptValidator, writeResult: false,
    acquireProductionLock: fixture.acquireProductionLock});
  assert.equal(result.status, 'COMPLETE');
  assert.equal(consumeCalls, 0);
  assert.equal(fixture.writes.length, 1);
});

test('successful status with consumed marker can be reverified and republished after attestation interruption', async () => {
  const fixture = authorizationFinalizerFixture({statusState: 'success',
    marker: {claimed: true, revoked: false, consumed: true, consumedSha: sha}});
  let consumeCalls = 0;
  const result = await finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
    assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
    readAuthorizationState: fixture.readAuthorizationState,
    consumeAuthorization: async () => { consumeCalls += 1; }, checkAuthorization: async () => {},
    currentMain: fixture.currentMain, readDeployEvidence: fixture.readDeployEvidence,
    inspectVps: fixture.inspectVps, receiptValidator: fixture.receiptValidator, writeResult: false,
    acquireProductionLock: fixture.acquireProductionLock});
  assert.equal(result.status, 'COMPLETE');
  assert.equal(consumeCalls, 0);
  assert.equal(fixture.writes.length, 1);
  assert.match(fixture.writes[0].description, /reconciled=600\/3$/);
});

test('post-consumption VPS drift blocks Deployment success publication', async () => {
  const fixture = authorizationFinalizerFixture({inspectVpsOutput: (count, output) => count === 1
    ? output : output.replace(`LIVE_API_SHA=${sha}`, `LIVE_API_SHA=${'c'.repeat(40)}`)});
  let consumed = false;
  await assert.rejects(finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline',
    assertCheckout: () => {}, verifyAuthorization: fixture.verifyAuthorization,
    readAuthorizationState: async () => ({claimed: true, revoked: false, consumed,
      ...(consumed ? {consumedSha: sha} : {})}),
    consumeAuthorization: async () => { consumed = true; }, checkAuthorization: async () => {},
    currentMain: fixture.currentMain, readDeployEvidence: fixture.readDeployEvidence,
    inspectVps: fixture.inspectVps, receiptValidator: fixture.receiptValidator, writeResult: false,
    acquireProductionLock: fixture.acquireProductionLock}),
  /baseline is mixed or unverified/);
  assert.equal(consumed, true);
  assert.equal(fixture.writes.length, 0);
});

test('authorization reconciliation fails closed on a shared lock or another active production Deployment', async () => {
  const fixture = authorizationFinalizerFixture();
  let consumes = 0;
  await assert.rejects(finalizeAuthorizationConsumption({client: fixture.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: fixture.env, root: '/offline', assertCheckout: () => {},
    acquireProductionLock: () => { throw new Error('lock held'); }, writeResult: false}), /lock held/);
  assert.equal(consumes, 0);
  const newer = {...fixture.taskDeployment, id: 8, created_at: '2026-10-19T02:00:00Z'};
  const withNewer = authorizationFinalizerFixture({otherDeployments: [newer]});
  await assert.rejects(finalizeAuthorizationConsumption({client: withNewer.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: withNewer.env, root: '/offline', assertCheckout: () => {},
    verifyAuthorization: withNewer.verifyAuthorization, readAuthorizationState: withNewer.readAuthorizationState,
    currentMain: withNewer.currentMain, readDeployEvidence: withNewer.readDeployEvidence,
    acquireProductionLock: withNewer.acquireProductionLock, writeResult: false}), /unique latest production Deployment/);
  assert.equal(withNewer.writes.length, 0);
  const active = {...fixture.taskDeployment, id: 9, created_at: '2026-10-18T23:00:00Z'};
  const withActive = authorizationFinalizerFixture({otherDeployments: [active]});
  await assert.rejects(finalizeAuthorizationConsumption({client: withActive.client, deploymentId: 7,
    candidateSha: sha, token: 'test-token', env: withActive.env, root: '/offline', assertCheckout: () => {},
    verifyAuthorization: withActive.verifyAuthorization, readAuthorizationState: withActive.readAuthorizationState,
    currentMain: withActive.currentMain, readDeployEvidence: withActive.readDeployEvidence,
    acquireProductionLock: withActive.acquireProductionLock, writeResult: false}), /unresolved status/);
  assert.equal(withActive.writes.length, 0);
});

test('reconciliation diagnostics expose only bounded trusted gate failures', () => {
  const gate = safeReconciliationFailure(new ReconciliationGateError('named gate\n' + 'x'.repeat(400)));
  assert.match(gate, /^GATE=named gate /);
  assert.ok(gate.length <= 245);
  assert.doesNotMatch(gate, /[\r\n]/);
  assert.equal(safeReconciliationFailure(Object.assign(new Error('private API response token=secret'),
    {name: 'FetchError'})), 'TYPE=FetchError');
});

test('candidate-to-executor scope rejects any path outside immutable authorization paths', () => {
  assert.doesNotThrow(() => assertCandidateExecutionScope({candidateSha: sha, executorSha,
    allowedPaths: ['scripts/'], changedPaths: ['scripts/reconcile-deployment.mjs']}));
  for (const changedPaths of [['src/app.ts'], ['../escape'], ['/absolute']]) {
    assert.throws(() => assertCandidateExecutionScope({candidateSha: sha, executorSha,
      allowedPaths: ['scripts/'], changedPaths}));
  }
});

test('authorization attestation resolver binds successful metadata run to original failure and same installation', async () => {
  const fixture = authorizationFinalizerFixture();
  const original = {...fixture.originalEvidence, deployRunConclusion: 'failure',
    evidenceManifestDigest: `sha256:${'a'.repeat(64)}`, evidenceContentSha256: 'b'.repeat(64)};
  const runId = '600', attempt = '3';
  const attestation = {...original, schema: 1, operation: 'AUTHORIZATION_CONSUMPTION_RECONCILIATION',
    status: 'COMPLETE', releaseState: 'COMPLETE', candidateSha: sha, executorSha,
    deployRunId: '42', deployRunAttempt: '1', automationRunId: runId,
    automationRunAttempt: attempt, deploymentId: 7, prepareRunId: '41', prepareRunAttempt: '1',
    authorizationMode: 'TASK_SCOPED_AUTO', authorizationId, authorizationConsumed: true,
    applicationVerification: 'VPS_READBACK_VERIFIED',
    originalEvidence: {manifestDigest: original.evidenceManifestDigest,
      contentSha256: original.evidenceContentSha256, runId: '42', runAttempt: '1'},
    reconciledFrom: {status: original.status, releaseState: original.releaseState,
      executionFailure: original.executionFailure},
    reconciliation: {operation: 'AUTHORIZATION_CONSUMPTION_RECONCILIATION', originalDeploymentId: 7,
      consumedCandidateSha: sha, statusVerified: true, liveVerified: true}};
  const resolved = await fetchAuthorizationReconciliationEvidence({runId, attempt, candidateSha: sha,
    deploymentId: 7, originalRunId: '42', originalRunAttempt: '1', prepareRunId: '41',
    prepareRunAttempt: '1', prepareReceiptDigest: identity.prepareReceiptDigest,
    prepareReceiptSha256: fixture.receipt && createHash('sha256').update(JSON.stringify(fixture.receipt)).digest('hex'),
    readOriginal: async () => original,
    readArtifact: async options => {
      assert.equal(options.publisherJob, 'reconcile-authorization');
      assert.equal(options.publisherStep, 'Publish reconciliation attestation as a GHCR OCI artifact');
      assert.equal(options.requireCandidateHead, false);
      return {evidence: attestation, deployRun: {head_sha: executorSha, conclusion: 'success'},
        digest: `sha256:${'c'.repeat(64)}`, contentSha256: 'd'.repeat(64)};
    }, receiptValidator: () => fixture.receipt});
  assert.equal(resolved.authorizationReconciliationVerified, true);
  assert.equal(resolved.deployRunConclusion, 'failure');
  assert.equal(resolved.reconciliationRunConclusion, 'success');
  assert.equal(resolved.authorizationConsumed, true);
  for (const [label, mutate, conclusion, headSha] of [
    ['original artifact digest', value => { value.originalEvidence.manifestDigest = `sha256:${'f'.repeat(64)}`; }],
    ['original content hash', value => { value.originalEvidence.contentSha256 = 'f'.repeat(64); }],
    ['deployment identity', value => { value.deploymentId = 8; }],
    ['Prepare receipt identity', value => { value.prepareRunAttempt = '2'; }],
    ['reconciliation conclusion', value => {}, 'failure'],
    ['executor provenance', value => {}, 'success', sha],
  ]) {
    const altered = structuredClone(attestation);
    mutate(altered);
    await assert.rejects(fetchAuthorizationReconciliationEvidence({runId, attempt, candidateSha: sha,
      deploymentId: 7, originalRunId: '42', originalRunAttempt: '1', prepareRunId: '41',
      prepareRunAttempt: '1', prepareReceiptDigest: identity.prepareReceiptDigest,
      prepareReceiptSha256: createHash('sha256').update(JSON.stringify(fixture.receipt)).digest('hex'),
      readOriginal: async () => original,
      readArtifact: async () => ({evidence: altered,
        deployRun: {head_sha: headSha ?? executorSha, conclusion: conclusion ?? 'success'},
        digest: `sha256:${'c'.repeat(64)}`, contentSha256: 'd'.repeat(64)}),
      receiptValidator: () => fixture.receipt}), label);
  }
});
