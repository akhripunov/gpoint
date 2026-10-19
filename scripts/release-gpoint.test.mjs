import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {runRemoteShaList, remoteProof, classifyReleaseFailure, evaluateSafetyGates,
  checkArtifactParity, checkEffectiveConfig, checkAgataArtifactParity, checkGsalesArtifactParity,
  currentApplyDeploymentContext,
} from './release-gpoint.mjs';
import {validateAgataRuntimeIdentity, planReleaseRetention, validateRuntimeIdentity} from './release-gpoint-vps-adapter.mjs';

const sha = 'a'.repeat(40);
const artifacts = Object.fromEntries(['api', 'worker', 'bot', 'miniapp', 'agataApi', 'agataWorker', 'gsalesApi']
  .map(key => [key, {sourceSha: sha}]));
const config = {api: sha, worker: sha, bot: sha, miniapp: sha, agataApi: sha, agataWorker: sha,
  agataProvider: 'disabled', enabled: true};

test('current Apply context binds child log URL, full image digests, receipt, and exact attempt', () => {
  const env = {GPOINT_CURRENT_DEPLOYMENT_CONTEXT: JSON.stringify({deploymentId: 7, identity: {
    candidateSha: sha, runId: '42', runAttempt: '2', prepareRunId: '41', prepareRunAttempt: '1',
    prepareReceiptDigest: `sha256:${'b'.repeat(64)}`, prepareReceiptSha256: 'c'.repeat(64),
    imageDigests: {app: `sha256:${'d'.repeat(64)}`, agata: `sha256:${'e'.repeat(64)}`,
      gsales: `sha256:${'f'.repeat(64)}`}, authorizationMode: 'EXACT_SHA', authorizationId: null,
    route: 'FULL_RELEASE', logUrl: 'https://github.com/akhripunov/gpoint/actions/runs/42/attempts/2',
  }}), GITHUB_RUN_ID: '42', GITHUB_RUN_ATTEMPT: '2', PREPARE_RUN_ID: '41', PREPARE_RUN_ATTEMPT: '1',
    GPOINT_PREPARE_RECEIPT_DIGEST: `sha256:${'b'.repeat(64)}`, GPOINT_PREPARE_RECEIPT_SHA256: 'c'.repeat(64)};
  const bundle = {staticOnly: false, images: {app: {digest: `sha256:${'d'.repeat(64)}`},
    agata: {digest: `sha256:${'e'.repeat(64)}`}, gsales: {digest: `sha256:${'f'.repeat(64)}`}}};
  assert.equal(currentApplyDeploymentContext(sha, bundle, env).deploymentId, 7);
  assert.equal(currentApplyDeploymentContext(sha, bundle, {}), null);
  const taskEnv = {...env, TASK_AUTHORIZATION_ID: 'gpoint-current-task-001'};
  const taskContext = JSON.parse(taskEnv.GPOINT_CURRENT_DEPLOYMENT_CONTEXT);
  taskContext.identity.authorizationMode = 'TASK_SCOPED_AUTO';
  taskContext.identity.authorizationId = taskEnv.TASK_AUTHORIZATION_ID;
  taskEnv.GPOINT_CURRENT_DEPLOYMENT_CONTEXT = JSON.stringify(taskContext);
  assert.equal(currentApplyDeploymentContext(sha, bundle, taskEnv).deploymentId, 7);
  assert.throws(() => currentApplyDeploymentContext(sha, bundle,
    {...env, GPOINT_CURRENT_DEPLOYMENT_CONTEXT: '{'}), /malformed/);
  for (const mutation of [
    context => { context.identity.candidateSha = 'f'.repeat(40); },
    context => { context.identity.runId = '43'; },
    context => { context.identity.runAttempt = '3'; },
    context => { context.identity.prepareRunId = '40'; },
    context => { context.identity.prepareRunAttempt = '2'; },
    context => { context.identity.prepareReceiptDigest = `sha256:${'0'.repeat(64)}`; },
    context => { context.identity.prepareReceiptSha256 = '0'.repeat(64); },
    context => { context.identity.authorizationMode = 'TASK_SCOPED_AUTO'; },
    context => { context.identity.authorizationId = 'gpoint-other-task-002'; },
    context => { context.identity.route = 'STATIC_UI_ONLY'; },
    context => { context.identity.logUrl = 'https://github.com/attacker/gpoint/actions/runs/42/attempts/2'; },
    context => { context.identity.imageDigests.app = `sha256:${'0'.repeat(64)}`; },
    context => { context.identity.imageDigests.gsales = undefined; },
  ]) {
    const changed = structuredClone(env);
    const context = JSON.parse(changed.GPOINT_CURRENT_DEPLOYMENT_CONTEXT);
    mutation(context);
    changed.GPOINT_CURRENT_DEPLOYMENT_CONTEXT = JSON.stringify(context);
    assert.throws(() => currentApplyDeploymentContext(sha, bundle, changed), /does not match/);
  }
});

test('source verification requests only exact current main and never production tags', () => {
  let called;
  const token = process.env.GITHUB_TOKEN;
  process.env.GITHUB_TOKEN = 'unit-test-token';
  let output;
  try {
    output = runRemoteShaList({runner: (file, args) => {
      called = [file, args];
      return `${sha}\trefs/heads/main\n`;
    }});
  } finally {
    if (token === undefined) delete process.env.GITHUB_TOKEN;
    else process.env.GITHUB_TOKEN = token;
  }
  assert.equal(called[0], 'git');
  assert.deepEqual(called[1], ['ls-remote', 'https://github.com/akhripunov/gpoint.git', 'refs/heads/main']);
  assert.equal(output, `${sha}\trefs/heads/main`);
  assert.deepEqual(remoteProof(sha, {runRemote: () => output}).directRef, 'refs/heads/main');
});

test('post-switch uncertainty is blocked pending verified VPS readback', () => {
  assert.equal(classifyReleaseFailure({switchStarted: true}).releaseState, 'INSTALLATION_UNCONFIRMED');
  assert.equal(classifyReleaseFailure({migrationStarted: true}).releaseState, 'INSTALLATION_UNCONFIRMED');
  assert.equal(classifyReleaseFailure({}).releaseState, 'FAILED_PRE_APPLY');
});

test('release safety gates remain exact-SHA and artifact-parity based without production tags', () => {
  const failures = evaluateSafetyGates({candidateSha: sha, remoteSha: sha, artifacts, config,
    recordedProductionSha: sha, liveProductionSha: sha});
  assert.deepEqual(failures, []);
  assert.ok(evaluateSafetyGates({candidateSha: sha, remoteSha: '0'.repeat(40), artifacts, config,
    recordedProductionSha: sha, liveProductionSha: sha}).some(message => /remote candidate SHA/.test(message)));
});

test('release safety gates reject a dirty candidate and every mismatched component artifact', () => {
  assert.ok(evaluateSafetyGates({candidateSha: sha, remoteSha: sha, clean: false, artifacts, config,
    recordedProductionSha: sha, liveProductionSha: sha}).some(message => /dirty/.test(message)));
  for (const role of ['api', 'worker', 'bot', 'miniapp']) {
    const changed = {...artifacts, [role]: {sourceSha: '0'.repeat(40)}};
    assert.ok(checkArtifactParity(sha, changed).length > 0, `${role} mismatch must block`);
  }
  for (const role of ['agataApi', 'agataWorker'])
    assert.ok(checkAgataArtifactParity(sha, {...artifacts, [role]: {sourceSha: '0'.repeat(40)}}).length > 0,
      `${role} mismatch must block`);
  for (const role of ['gsalesApi', 'gsalesAgataApi', 'gsalesAgataWorker'])
    assert.ok(checkGsalesArtifactParity(sha, {...artifacts, [role]: {sourceSha: '0'.repeat(40)}}).length > 0,
      `${role} mismatch must block`);
});

test('post-switch failures remain installation-unconfirmed and are not pre-Apply failures', () => {
  const switched = classifyReleaseFailure({switchStarted: true});
  assert.equal(switched.failureStage, 'POST_SWITCH');
  assert.equal(switched.releaseState, 'INSTALLATION_UNCONFIRMED');
  const migration = classifyReleaseFailure({migrationStarted: true});
  assert.equal(migration.failureStage, 'PRE_SWITCH_DB_OR_AGATA');
  assert.equal(migration.releaseState, 'INSTALLATION_UNCONFIRMED');
});

test('operational Deploy workflow uses Deployments API and contains no Ledger, recovery finalizer or production tag path', () => {
  const workflow = fs.readFileSync(new URL('../.github/workflows/gpoint-deploy.yml', import.meta.url), 'utf8');
  const runner = fs.readFileSync(new URL('./automated-release.mjs', import.meta.url), 'utf8');
  const execution = fs.readFileSync(new URL('./deployment-execution.mjs', import.meta.url), 'utf8');
  const release = fs.readFileSync(new URL('./release-gpoint.mjs', import.meta.url), 'utf8');
  assert.match(workflow, /deployments: write/);
  assert.match(runner, /createGitHubDeploymentsClient/);
  assert.match(runner, /beginProductionDeployment/);
  assert.match(execution, /state: 'success'/);
  assert.doesNotMatch(workflow + runner + release, /gpoint-release-ledger|--finalize-recovery|gpoint-production-\d/);
  assert.doesNotMatch(workflow, /^\s+environment:\s*production\s*$/m);
});

test('read-only VPS preflight does not bootstrap Buildx', () => {
  const adapter = fs.readFileSync(new URL('./release-gpoint-vps-adapter.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(adapter, /docker buildx inspect --bootstrap/);
  assert.match(adapter, /docker buildx inspect >\/dev\/null/);
});

test('Prepare artifact parity still requires all exact source SHAs', () => {
  assert.deepEqual(checkArtifactParity(sha, artifacts), []);
  for (const role of ['api', 'worker', 'bot', 'miniapp']) {
    assert.ok(checkArtifactParity(sha, {...artifacts, [role]: {sourceSha: '0'.repeat(40)}}).length,
      `${role} mismatch must block`);
  }
});

test('effective API and Mini App identities remain exact candidate-bound', () => {
  assert.deepEqual(checkEffectiveConfig(sha, config), []);
  assert.ok(checkEffectiveConfig(sha, {...config, api: '0'.repeat(40)}).some(value => value.includes('api')));
  assert.ok(checkEffectiveConfig(sha, {...config, miniapp: '0'.repeat(40)}).some(value => value.includes('miniapp')));
});

test('backup and migration failures block before runtime mutation', () => {
  const common = {candidateSha: sha, remoteSha: sha, artifacts, config,
    recordedProductionSha: sha, liveProductionSha: sha};
  assert.ok(evaluateSafetyGates({...common, backupReady: false}).includes('backup gate failed'));
  assert.ok(evaluateSafetyGates({...common, migrationOk: false}).includes('migration gate failed'));
});

test('active release lock and GitHub/VPS baseline drift block', () => {
  const common = {candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: sha,
    liveProductionSha: sha};
  assert.ok(evaluateSafetyGates({...common, concurrent: true}).includes('concurrent release lock is held'));
  assert.ok(evaluateSafetyGates({...common, recordedProductionSha: '0'.repeat(40)}).includes('live production SHA drift'));
});

test('runtime completion requires exact candidate image and source identities', () => {
  const imageId = `sha256:${'a'.repeat(64)}`;
  assert.deepEqual(validateRuntimeIdentity(sha, {imageId, buildSha: sha,
    apiImage: imageId, workerImage: imageId, botImage: imageId}), []);
  assert.ok(validateRuntimeIdentity(sha, {imageId, buildSha: sha,
    apiImage: `sha256:${'b'.repeat(64)}`, workerImage: imageId, botImage: imageId}).length);
});

test('AGATA health and worker identity remain mandatory', () => {
  const imageId = `sha256:${'c'.repeat(64)}`;
  const good = {imageId, buildSha: sha, apiImage: imageId, workerImage: imageId,
    apiRunning: 'true', apiHealth: 'healthy', workerRunning: 'true', workerHealth: 'healthy'};
  assert.deepEqual(validateAgataRuntimeIdentity(sha, good), []);
  assert.ok(validateAgataRuntimeIdentity(sha, {...good, workerHealth: 'starting'}).length);
});

test('retention and cleanup never remove production data volumes', () => {
  const current = 'b'.repeat(40);
  const rollback = 'c'.repeat(40);
  const plan = planReleaseRetention({currentSha: current, rollbackSha: rollback,
    candidates: [{sha, status: 'INSTALLATION_UNCONFIRMED'}],
    imageTags: [`gpoint-runtime:${sha}`, `gpoint-runtime:${current}`], volumes: ['postgres-data']});
  assert.deepEqual(plan.removableShas, []);
  assert.deepEqual(plan.volumesToRemove, []);
});
