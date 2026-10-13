import assert from 'node:assert/strict';
import { test } from 'node:test';
import { deriveV3ProductionParent, verifyComponentReceiptV4, verifyVPSNativeImages } from './release-component-bundle-v4.mjs';

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
