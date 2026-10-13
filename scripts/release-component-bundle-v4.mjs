import fs from 'node:fs';
import crypto from 'node:crypto';

const SHA = /^[0-9a-f]{40}$/;
const ID = /^sha256:[0-9a-f]{64}$/;
const roles = ['app', 'agata', 'gsales'];
const routes = { FULL: roles, AGATA_CANDIDATE: ['agata'], STATIC_UI_CANDIDATE: [] };
const names = { app: 'gpoint-runtime', agata: 'gpoint-agata-runtime', gsales: 'gpoint-g-sales-runtime' };
const checkSHA = (v) => { if (!SHA.test(v ?? '')) throw new Error('invalid source SHA'); };
const checkID = (v) => { if (!ID.test(v ?? '')) throw new Error('invalid image digest'); };

// Bootstrap the current V3 ledger without guessing which GSales lane's
// "imageId" represents AGATA. This must be supplied from authenticated,
// immutable canonical ledger; the caller separately checks live VPS digests.
export function deriveV3ProductionParent(ledger) {
  const baseline = ledger?.currentProductionSha;
  checkSHA(baseline);
  if (ledger.lastReleaseStatus !== 'COMPLETE' ||
      ledger.serverDeployStatus !== 'COMPLETE' ||
      ledger.releaseOrigin !== 'durable-production-release' ||
      ledger.recoveryRequired !== false ||
      ledger.activeRuntimeSha !== baseline ||
      ledger.agataRuntimeSha !== baseline)
    throw new Error('V3 baseline not immutable COMPLETE or out of sync');
  const ident = ledger.artifactIdentities;
  if (!ident || typeof ident !== 'object') throw new Error('V3 artifact identities missing');
  const roleChecks = {
    app: ['api', 'worker', 'bot'],
    agata: ['agataApi', 'agataWorker'],
    gsales: ['gsalesApi'],
  };
  const images = {};
  for (const role of roles) {
    const members = roleChecks[role];
    const ids = members.map(name => {
      const value = ident[name];
      if (value?.sourceSha !== baseline || value?.adapter?.buildSha !== baseline)
        throw new Error('V3 recorded role source diverges ' + name);
      checkID(value.adapter.imageId);
      return value.adapter.imageId;
    });
    if (ids.some(id => id !== ids[0]))
      throw new Error('V3 role images diverge ' + role);
    images[role] = { imageId: ids[0], sourceSha: baseline, platform: 'linux/amd64' };
  }
  for (const lane of ['gsalesAgataApi', 'gsalesAgataWorker']) {
    const value = ident[lane];
    if (value?.sourceSha !== baseline || value?.adapter?.buildSha !== baseline ||
        value?.adapter?.agataImageId !== images.agata.imageId)
      throw new Error('GSales AGATA image does not match AGATA role');
  }
  return {
    currentProductionSha: baseline, lastReleaseStatus: 'COMPLETE',
    serverDeployStatus: 'COMPLETE', releaseOrigin: 'durable-production-release',
    recoveryRequired: false, images,
  };
}

export function verifyComponentReceiptV4(manifest, {
  candidate, baseline, completeParent, candidateArtifact,
} = {}) {
  checkSHA(candidate); checkSHA(baseline);
  if (candidate === baseline) throw new Error('candidate equals baseline');
  if (manifest?.schema !== 1 || manifest?.candidateSha !== candidate ||
      manifest?.previousSourceSha !== baseline || manifest?.mayDeploySelectively !== false ||
      manifest?.immutableParentRequired !== true ||
      manifest?.requiresLiveNativeImageProof !== true ||
      manifest?.requiresV3ExactShaApproval !== true ||
      manifest?.requiresV3PreflightAndRollback !== true)
    throw new Error('missing immutable V4 guards');
  const rebuilt = routes[manifest.route];
  if (!rebuilt) throw new Error('unknown V4 route');
  if (completeParent?.currentProductionSha !== baseline ||
      completeParent?.lastReleaseStatus !== 'COMPLETE' ||
      completeParent?.serverDeployStatus !== 'COMPLETE' ||
      completeParent?.releaseOrigin !== 'durable-production-release' ||
      completeParent?.recoveryRequired !== false)
    throw new Error('parent release is not immutable COMPLETE');
  const parentImages = completeParent.images;
  const entries = manifest.images;
  if (!parentImages || !entries ||
      roles.some(r => !parentImages[r] || !entries[r]) ||
      Object.keys(parentImages).sort().join(',') !== roles.slice().sort().join(',') ||
      Object.keys(entries).sort().join(',') !== roles.slice().sort().join(','))
    throw new Error('untrusted image inventory');
  for (const role of roles) {
    const img = entries[role];
    const before = parentImages[role];
    if (img.name !== names[role] || img.platform !== 'linux/amd64')
      throw new Error('bad role image platform/name');
    checkID(img.imageId); checkSHA(img.sourceSha);
    if (rebuilt.includes(role)) {
      if (img.kind !== 'new' || img.sourceSha !== candidate ||
          candidateArtifact?.images?.[role]?.id !== img.imageId ||
          candidateArtifact.images[role].revision !== candidate ||
          candidateArtifact.images[role].platform !== 'linux/amd64')
        throw new Error('unverified new candidate image');
    } else if (img.kind !== 'inherited' || img.sourceSha === candidate ||
               img.sourceSha !== before.sourceSha ||
               img.imageId !== before.imageId || img.platform !== before.platform)
      throw new Error('unverified inherited image');
  }
  if (manifest.route === 'STATIC_UI_CANDIDATE') {
    if (manifest.static?.candidateSha !== candidate ||
        manifest.static?.kind !== 'gpoint-miniapp-static' ||
        candidateArtifact?.static?.candidateSha !== candidate ||
        candidateArtifact?.static?.kind !== 'gpoint-miniapp-static')
      throw new Error('unverified static artifact');
  } else if (manifest.static !== null) {
    throw new Error('unexpected UI artifact');
  }
  return { route: manifest.route, candidate, baseline, newRoles: rebuilt,
    inheritedRoles: roles.filter(r => !rebuilt.includes(r)),
    requiresV3Approval: true, requiresLiveImageParity: true };
}

export function verifyVPSNativeImages(manifest, observed, { candidate, baseline } = {}) {
  checkSHA(candidate); checkSHA(baseline);
  if (manifest.candidateSha !== candidate || manifest.previousSourceSha !== baseline)
    throw new Error('VPS native image proof has wrong SHA');
  for (const role of roles) {
    const expected = manifest.images?.[role];
    const actual = observed?.[role];
    checkID(expected?.imageId);
    if (actual?.nativeImageId !== expected.imageId ||
        actual?.revisionLabel !== expected.sourceSha ||
        actual?.name !== expected.name + ':' + expected.sourceSha ||
        actual?.os !== 'linux' || actual?.architecture !== 'amd64' ||
        (expected.kind === 'inherited' && actual?.wasRetagged === true))
      throw new Error('VPS native image mismatch ' + role);
  }
  return true;
}

export function verifyComponentArchive(archive, expectedSha256) {
  if (!/^[a-f0-9]{64}$/.test(expectedSha256 ?? '') ||
      fs.lstatSync(archive).isSymbolicLink() || !fs.statSync(archive).isFile())
    throw new Error('untrusted component archive');
  const hash = crypto.createHash('sha256');
  const f = fs.openSync(archive, 'r');
  try {
    const block = Buffer.alloc(65536);
    let n;
    while ((n = fs.readSync(f, block, 0, block.length, null)) > 0)
      hash.update(block.subarray(0, n));
  } finally { fs.closeSync(f); }
  if (hash.digest('hex') !== expectedSha256)
    throw new Error('component artifact hash mismatch');
  return true;
}
