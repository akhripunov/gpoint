import assert from 'node:assert/strict';
import { test } from 'node:test';
import { stateFromLiveVps } from './deployment-run-state.mjs';
import { deriveV3ProductionParent, verifyComponentReceiptV4, verifyVPSNativeImages, verifyV5ProductionParent, buildV5ComponentImageInventory } from './release-component-bundle-v4.mjs';

const A = 'a'.repeat(40), B = 'b'.repeat(40);
const id = digit => 'sha256:' + digit.repeat(64);
const images = {
  app: { name: 'gpoint-runtime', sourceSha: A, imageId: id('1'), platform: 'linux/amd64' },
  agata: { name: 'gpoint-agata-runtime', sourceSha: A, imageId: id('2'), platform: 'linux/amd64' },
  gsales: { name: 'gpoint-g-sales-runtime', sourceSha: A, imageId: id('3'), platform: 'linux/amd64' },
};
const parent = { currentProductionSha: A, lastReleaseStatus: 'COMPLETE',
  serverDeployStatus: 'COMPLETE', recoveryRequired: false,
  releaseOrigin: 'durable-production-release', images };
const proof = { schema: 1, candidateSha: B, previousSourceSha: A,
  route: 'AGATA_CANDIDATE', images: {
    app: { ...images.app, kind: 'inherited' },
    agata: { ...images.agata, sourceSha: B, imageId: id('4'), kind: 'new' },
    gsales: { ...images.gsales, kind: 'inherited' },
  },
  static: null, mayDeploySelectively: false,
  immutableParentRequired: true, requiresLiveNativeImageProof: true,
  requiresV3ExactShaApproval: true, requiresV3PreflightAndRollback: true,
};
const artifact = { images: { agata: { id: id('4'), revision: B, platform: 'linux/amd64' } } };
const validate = (p = proof, old = parent, a = artifact) =>
  verifyComponentReceiptV4(p, { candidate: B, baseline: A, completeParent: old, candidateArtifact: a });

test('AGATA-only proves new image and inherits app + sales', () => {
  const result = validate();
  assert.deepEqual(result.newRoles, ['agata']);
  assert.deepEqual(result.inheritedRoles, ['app', 'gsales']);
});
test('invalid immutable baseline is rejected', () => {
  for (const changed of [{ ...parent, lastReleaseStatus: 'FAILED' },
    { ...parent, recoveryRequired: true }, { ...parent, currentProductionSha: B },
    { ...parent, images: {} }]) assert.throws(() => validate(proof, changed));
});
test('inherited digest/source alteration is rejected', () => {
  assert.throws(() => validate({ ...proof, images: { ...proof.images,
    app: { ...proof.images.app, imageId: id('8') } } }));
  assert.throws(() => validate({ ...proof, images: { ...proof.images,
    gsales: { ...proof.images.gsales, sourceSha: B } } }));
});
test('new image mismatch and bypass flag rejected', () => {
  assert.throws(() => validate(proof, parent, { images: { agata: { ...artifact.images.agata, id: id('9') } } }));
  assert.throws(() => validate({ ...proof, mayDeploySelectively: true }));
  assert.throws(() => validate({ ...proof, route: 'FULL' }));
});
test('UI-only inherits all three images and proves static bundle', () => {
  const staticRef = { kind: 'gpoint-miniapp-static', candidateSha: B };
  const ui = { ...proof, route: 'STATIC_UI_CANDIDATE', images:
    Object.fromEntries(Object.entries(images).map(([key, img]) => [key, { ...img, kind: 'inherited' }])),
    static: staticRef };
  assert.deepEqual(validate(ui, parent, { static: staticRef }).newRoles, []);
  assert.throws(() => validate({ ...ui, static: { ...staticRef, candidateSha: A } }, parent, { static: staticRef }));
});
test('VPS native exact image/label and no retag are enforced', () => {
  const observed = Object.fromEntries(Object.entries(proof.images).map(([role, img]) => [role, {
    nativeImageId: img.imageId, revisionLabel: img.sourceSha,
    name: img.name + ':' + img.sourceSha, os: 'linux', architecture: 'amd64',
  }]));
  assert.equal(verifyVPSNativeImages(proof, observed, { candidate: B, baseline: A }), true);
  assert.throws(() => verifyVPSNativeImages(proof, { ...observed,
    gsales: { ...observed.gsales, nativeImageId: id('9') } }, { candidate: B, baseline: A }));
  assert.throws(() => verifyVPSNativeImages(proof, { ...observed,
    app: { ...observed.app, wasRetagged: true } }, { candidate: B, baseline: A }));
});

test('V3 ledger bridge proves one role digest and shared GSales AGATA dependency', () => {
  const baseline = A;
  const make = (sourceSha, imageId, agataImageId) => ({
    sourceSha, adapter: { imageId, buildSha: sourceSha, ...(agataImageId ? { agataImageId } : {}) },
  });
  const ledger = {
    currentProductionSha: baseline, activeRuntimeSha: baseline, agataRuntimeSha: baseline,
    lastReleaseStatus: 'COMPLETE', serverDeployStatus: 'COMPLETE',
    releaseOrigin: 'durable-production-release', recoveryRequired: false,
    artifactIdentities: {
      api: make(A, id('1')), worker: make(A, id('1')), bot: make(A, id('1')),
      agataApi: make(A, id('2')), agataWorker: make(A, id('2')),
      gsalesApi: make(A, id('3')),
      gsalesAgataApi: make(A, id('3'), id('2')),
      gsalesAgataWorker: make(A, id('3'), id('2')),
    },
  };
  const derived = deriveV3ProductionParent(ledger);
  assert.equal(derived.images.app.imageId, id('1'));
  assert.equal(derived.images.agata.imageId, id('2'));
  assert.equal(derived.images.gsales.imageId, id('3'));
  assert.deepEqual(validate(proof, derived).newRoles, ['agata']);
  assert.throws(() => deriveV3ProductionParent({ ...ledger,
    artifactIdentities: { ...ledger.artifactIdentities, worker: make(A, id('9')) },
  }));
  assert.throws(() => deriveV3ProductionParent({ ...ledger,
    artifactIdentities: { ...ledger.artifactIdentities, gsalesAgataApi: make(A, id('3'), id('9')) },
  }));
  assert.throws(() => deriveV3ProductionParent({ ...ledger, recoveryRequired: true }));
});

test('V5 parent requires successful exact Deployment, Prepare digests, live identities and clean recovery state', () => {
  const v5 = {
    currentProductionSha: A, releaseOrigin: 'github-deployment',
    lastReleaseStatus: 'COMPLETE', serverDeployStatus: 'COMPLETE',
    activeRuntimeSha: A, agataRuntimeSha: A, miniappSourceSha: A,
    currentProductionDbHead: 'prisma-1', agataAlembicHead: 'agata-1',
    gsalesMigrationHead: 'gsales-1', gsalesMigrationSha256: 'c'.repeat(64),
    artifactIdentities: {}, componentImages: Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
      { ...image, registryDigest: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6') }])),
  };
  const registryImages = Object.fromEntries(Object.entries(images).map(([role, image]) => [role, {
    sourceSha: A, configDigest: image.imageId, manifestDigest: id(String(role === 'app' ? '4' : role === 'agata' ? '5' : '6')[0]),
    nativeId: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6'),
  }]));
  const evidence = {
    deployment: { source: 'github-api', sha: A, status: 'success', environment: 'production',
      task: 'gpoint-production-deploy', productionEnvironment: true, transientEnvironment: false,
      runId: '10', runAttempt: '1', prepareRunId: '9', prepareRunAttempt: '1',
      authorizationMode: 'TASK_SCOPED_AUTO', authorizationId: 'me42-owner-cycle-1',
      receiptDigest: id('7'), imageDigests: Object.fromEntries(Object.entries(registryImages).map(([role, item]) => [role, item.manifestDigest])) },
    prepare: { source: 'verified-ghcr-receipt', candidateSha: A, runId: '9', runAttempt: '1',
      receiptDigest: id('7'), prismaHead: 'prisma-1', agataHead: 'agata-1',
      gsalesMigrationHead: 'gsales-1', gsalesMigrationSha256: 'c'.repeat(64), images: registryImages },
    live: { source: 'read-only-vps', runtimeSha: A, miniappSha: A, prismaHead: 'prisma-1',
      agataHead: 'agata-1', gsalesHead: 'gsales-1', gsalesMigrationSha256: 'c'.repeat(64),
      apiHealth: 'healthy', agataApiHealth: 'healthy', gsalesApiHealth: 'healthy',
      gsalesAgataApiHealth: 'healthy', gsalesAgataWorkerHealth: 'healthy', gsalesRuntimeState: 'running',
      gsalesFacts: Object.fromEntries(['API', 'AGATA_API', 'AGATA_MEMORY_WORKER'].flatMap(role => {
        const p = `LIVE_GSALES_${role}`;
        return [[`${p}_STATUS`, 'running'], [`${p}_RUNNING`, 'true'], [`${p}_HEALTH`, 'healthy'],
          [`${p}_EXIT_CODE`, '0'], [`${p}_OOM_KILLED`, 'false']];
      })),
      workerRunning: true, botRunning: true, agataWorkerRunning: true, gsalesAgataWorkerRunning: true,
      nativeImages: Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
        { imageId: registryImages[role].nativeId, sourceSha: A, platform: 'linux/amd64' }])) },
    unresolvedRecovery: false,
  };
  const verifiedParent = verifyV5ProductionParent(v5, evidence);
  assert.equal(verifiedParent.currentProductionSha, A);
  assert.equal(verifiedParent.images.app.imageId, id('1'));
  assert.equal(verifiedParent.images.app.nativeId, id('4'));
  assert.deepEqual(verifyComponentReceiptV4(proof, {candidate: B, baseline: A,
    completeParent: verifiedParent, candidateArtifact: artifact}).inheritedRoles, ['app', 'gsales']);
  const stoppedGsales = structuredClone(evidence);
  stoppedGsales.live.gsalesRuntimeState = 'stopped';
  for (const role of ['API', 'AGATA_API', 'AGATA_MEMORY_WORKER']) {
    const p = `LIVE_GSALES_${role}`;
    stoppedGsales.live.gsalesFacts[`${p}_STATUS`] = 'exited';
    stoppedGsales.live.gsalesFacts[`${p}_RUNNING`] = 'false';
    stoppedGsales.live.gsalesFacts[`${p}_HEALTH`] = 'none';
    stoppedGsales.live.gsalesFacts[`${p}_EXIT_CODE`] = '0';
    stoppedGsales.live.gsalesFacts[`${p}_OOM_KILLED`] = 'false';
  }
  assert.equal(verifyV5ProductionParent(v5, stoppedGsales).currentProductionSha, A);
  stoppedGsales.live.gsalesFacts.LIVE_GSALES_API_EXIT_CODE = '137';
  assert.throws(() => verifyV5ProductionParent(v5, stoppedGsales), /unknown, damaged, or mixed/);
  assert.throws(() => verifyComponentReceiptV4(proof, {candidate: B, baseline: A,
    completeParent: {...verifiedParent}, candidateArtifact: artifact}), /immutable COMPLETE/);
  for (const changed of [
    { ...evidence, deployment: { ...evidence.deployment, status: 'failure' } },
    { ...evidence, deployment: { ...evidence.deployment, sha: B } },
    { ...evidence, deployment: { ...evidence.deployment, imageDigests: { ...evidence.deployment.imageDigests, app: id('8') } } },
    { ...evidence, prepare: { ...evidence.prepare, images: { ...registryImages, app: undefined } } },
    { ...evidence, prepare: { ...evidence.prepare, images: { ...registryImages, app: { ...registryImages.app, nativeId: images.app.imageId } } } },
    { ...evidence, live: { ...evidence.live, nativeImages: { ...evidence.live.nativeImages, app: { imageId: id('9'), sourceSha: A, platform: 'linux/amd64' } } } },
    { ...evidence, live: { ...evidence.live, prismaHead: 'wrong' } },
    { ...evidence, live: { ...evidence.live, apiHealth: 'unhealthy' } },
    { ...evidence, unresolvedRecovery: true },
    { ...evidence, deployment: { ...evidence.deployment, runAttempt: '' } },
    { ...evidence, deployment: { ...evidence.deployment, authorizationId: '' } },
  ]) assert.throws(() => verifyV5ProductionParent(v5, changed));
  assert.throws(() => verifyV5ProductionParent({ ...v5, recoveryRequired: true }, evidence));
  assert.throws(() => verifyV5ProductionParent(v5, null));
});

test('V5 inventory preserves manifest digest separately from Docker config identity', () => {
  const prepare = { candidateSha: A, selectedTransport: 'ghcr-oci', ghcrVerified: true,
    ghcrPostSwitchVerified: true, images: Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
      { digest: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6'),
        configDigest: image.imageId, id: image.imageId, sourceSha: A, platform: 'linux/amd64' }])) };
  const runtime = Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
    { imageId: image.imageId, buildSha: A }]));
  const inventory = buildV5ComponentImageInventory(prepare, runtime, A);
  assert.equal(inventory.app.imageId, id('1'));
  assert.equal(inventory.app.registryDigest, id('4'));
  assert.throws(() => buildV5ComponentImageInventory({ ...prepare, ghcrPostSwitchVerified: false }, runtime, A));
  assert.throws(() => buildV5ComponentImageInventory(prepare,
    { ...runtime, agata: { imageId: id('9'), buildSha: A } }, A));
});

test('fresh run-scoped VPS state maps GSales migration identity into the V5 parent verifier', () => {
  const facts = {LIVE_RELEASE_ENV_SHA: A, LIVE_API_SHA: A, LIVE_WORKER_SHA: A, LIVE_BOT_SHA: A,
    LIVE_MINIAPP_SHA: A, LIVE_AGATA_API_SHA: A, LIVE_AGATA_WORKER_SHA: A,
    LIVE_API_RUNNING: 'true', LIVE_API_HEALTH: 'healthy', LIVE_WORKER_RUNNING: 'true', LIVE_BOT_RUNNING: 'true',
    LIVE_API_IMAGE: id('1'), LIVE_WORKER_IMAGE: id('1'), LIVE_BOT_IMAGE: id('1'),
    LIVE_AGATA_API_RUNNING: 'true', LIVE_AGATA_API_HEALTH: 'healthy', LIVE_AGATA_API_IMAGE_ID: id('2'),
    LIVE_AGATA_WORKER_RUNNING: 'true', LIVE_AGATA_WORKER_HEALTH: 'healthy', LIVE_AGATA_WORKER_IMAGE_ID: id('2'),
    LIVE_PRISMA_HEAD: 'prisma-1', LIVE_AGATA_HEAD: 'agata-1', LIVE_GSALES_HEAD: 'gsales-1',
    LIVE_GSALES_MIGRATION_SHA256: 'c'.repeat(64)};
  for (const role of ['API', 'AGATA_API', 'AGATA_MEMORY_WORKER']) Object.assign(facts, {
    [`LIVE_GSALES_${role}_SHA`]: A, [`LIVE_GSALES_${role}_RUNNING`]: 'true',
    [`LIVE_GSALES_${role}_STATUS`]: 'running', [`LIVE_GSALES_${role}_HEALTH`]: 'healthy',
    [`LIVE_GSALES_${role}_EXIT_CODE`]: '0', [`LIVE_GSALES_${role}_OOM_KILLED`]: 'false',
    [`LIVE_GSALES_${role}_IMAGE_ID`]: id('3'),
  });
  const fresh = stateFromLiveVps(facts);
  const componentImages = Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
    {...image, registryDigest: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6')}]));
  const ledger = {...fresh, releaseOrigin: 'github-deployment', recoveryRequired: false,
    lastReleaseStatus: 'COMPLETE', serverDeployStatus: 'COMPLETE', miniappSourceSha: A,
    componentImages};
  const evidence = {
    deployment: {source: 'github-api', sha: A, status: 'success', environment: 'production',
      task: 'gpoint-production-deploy', productionEnvironment: true, transientEnvironment: false,
      runId: '10', runAttempt: '1', prepareRunId: '9', prepareRunAttempt: '1',
      authorizationMode: 'EXACT_SHA', receiptDigest: id('7'),
      imageDigests: {app: id('4'), agata: id('5'), gsales: id('6')}},
    prepare: {source: 'verified-ghcr-receipt', candidateSha: A, runId: '9', runAttempt: '1',
      receiptDigest: id('7'), prismaHead: 'prisma-1', agataHead: 'agata-1',
      gsalesMigrationHead: 'gsales-1', gsalesMigrationSha256: 'c'.repeat(64),
      images: Object.fromEntries(Object.entries(images).map(([role, image]) => [role, {
        sourceSha: A, configDigest: image.imageId, manifestDigest: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6'),
        nativeId: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6')}]))},
    live: {source: 'read-only-vps', runtimeSha: A, miniappSha: A, prismaHead: 'prisma-1',
      agataHead: 'agata-1', gsalesHead: 'gsales-1', gsalesMigrationSha256: 'c'.repeat(64),
      apiHealth: 'healthy', agataApiHealth: 'healthy', gsalesApiHealth: 'healthy',
      gsalesAgataApiHealth: 'healthy', gsalesAgataWorkerHealth: 'healthy', workerRunning: true,
      gsalesRuntimeState: 'running', gsalesFacts: Object.fromEntries(['API', 'AGATA_API', 'AGATA_MEMORY_WORKER'].flatMap(role => {
        const p = `LIVE_GSALES_${role}`;
        return [[`${p}_STATUS`, 'running'], [`${p}_RUNNING`, 'true'], [`${p}_HEALTH`, 'healthy'],
          [`${p}_EXIT_CODE`, '0'], [`${p}_OOM_KILLED`, 'false']];
      })),
      botRunning: true, agataWorkerRunning: true, gsalesAgataWorkerRunning: true,
      nativeImages: Object.fromEntries(Object.entries(images).map(([role, image]) => [role,
        {imageId: id(role === 'app' ? '4' : role === 'agata' ? '5' : '6'), sourceSha: A, platform: 'linux/amd64'}]))},
    unresolvedRecovery: false,
  };
  assert.equal(verifyV5ProductionParent(ledger, evidence).currentProductionSha, A);
});
