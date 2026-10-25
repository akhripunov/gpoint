import fs from 'node:fs';
import crypto from 'node:crypto';
import {assertGsalesRuntimeState} from './deployment-run-state.mjs';

const SHA = /^[0-9a-f]{40}$/;
const ID = /^sha256:[0-9a-f]{64}$/;
const roles = ['app', 'agata', 'gsales'];
const routes = { FULL: roles, AGATA_CANDIDATE: ['agata'], STATIC_UI_CANDIDATE: [] };
const names = { app: 'gpoint-runtime', agata: 'gpoint-agata-runtime', gsales: 'gpoint-g-sales-runtime' };
const checkSHA = (v) => { if (!SHA.test(v ?? '')) throw new Error('invalid source SHA'); };
const checkID = (v) => { if (!ID.test(v ?? '')) throw new Error('invalid image digest'); };
const verifiedV5Parents = new WeakSet();

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

/** Construct a V3/V4 parent for V5 only from independently verified evidence.
 * The caller must fetch the Deployment from GitHub, GHCR Prepare receipt and
 * current VPS proof; a state-file origin/status flag is deliberately insufficient.
 */
export function verifyV5ProductionParent(ledger, evidence) {
  const sha = ledger?.currentProductionSha;
  checkSHA(sha);
  if (ledger.releaseOrigin !== 'github-deployment' || ledger.recoveryRequired === true ||
      ledger.lastReleaseStatus !== 'COMPLETE' || ledger.serverDeployStatus !== 'COMPLETE' ||
      ledger.activeRuntimeSha !== sha || ledger.agataRuntimeSha !== sha || ledger.miniappSourceSha !== sha ||
      !evidence || evidence.unresolvedRecovery !== false)
    throw new Error('V5 parent lacks complete non-recovery deployment evidence');
  const deployment = evidence.deployment;
  if (deployment?.source !== 'github-api' || deployment.status !== 'success' ||
      deployment.sha !== sha || deployment.environment !== 'production' ||
      deployment.task !== 'gpoint-production-deploy' || deployment.productionEnvironment !== true ||
      deployment.transientEnvironment !== false || !/^[1-9][0-9]*$/.test(String(deployment.runId ?? '')) ||
      !/^[1-9][0-9]*$/.test(String(deployment.runAttempt ?? '')) ||
      !/^[1-9][0-9]*$/.test(String(deployment.prepareRunId ?? '')) ||
      !/^[1-9][0-9]*$/.test(String(deployment.prepareRunAttempt ?? '')) ||
      !['EXACT_SHA', 'TASK_SCOPED_AUTO'].includes(deployment.authorizationMode) ||
      (deployment.authorizationMode === 'TASK_SCOPED_AUTO' &&
        !/^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/.test(deployment.authorizationId ?? '')) ||
      !ID.test(deployment.receiptDigest ?? ''))
    throw new Error('V5 parent has no successful exact-SHA GitHub Deployment');
  const prepare = evidence.prepare;
  if (prepare?.source !== 'verified-ghcr-receipt' || prepare.candidateSha !== sha ||
      String(prepare.runId) !== String(deployment.prepareRunId) ||
      String(prepare.runAttempt) !== String(deployment.prepareRunAttempt) ||
      prepare.receiptDigest !== deployment.receiptDigest ||
      !ID.test(prepare.receiptDigest ?? ''))
    throw new Error('V5 parent Prepare receipt provenance does not match the Deployment');
  const stateImages = ledger.componentImages;
  if (!stateImages || Object.keys(stateImages).sort().join(',') !== roles.slice().sort().join(','))
    throw new Error('V5 parent component image inventory is incomplete');
  const images = {};
  for (const role of roles) {
    const receiptImage = prepare.images?.[role];
    const stateImage = stateImages[role];
    checkID(receiptImage?.manifestDigest); checkID(receiptImage?.configDigest); checkID(receiptImage?.nativeId);
    checkSHA(receiptImage?.sourceSha);
    if (receiptImage.sourceSha !== sha || receiptImage.nativeId === receiptImage.configDigest ||
        stateImage?.sourceSha !== sha ||
        stateImage.imageId !== receiptImage.configDigest ||
        stateImage.registryDigest !== receiptImage.manifestDigest ||
        stateImage.platform !== 'linux/amd64' ||
        deployment.imageDigests?.[role] !== receiptImage.manifestDigest)
      throw new Error('V5 parent GHCR digest/config identity mismatch ' + role);
    images[role] = { name: names[role], sourceSha: sha,
      imageId: receiptImage.configDigest, nativeId: receiptImage.nativeId,
      registryDigest: receiptImage.manifestDigest, platform: 'linux/amd64' };
  }
  if (Object.keys(deployment.imageDigests ?? {}).sort().join(',') !== roles.slice().sort().join(','))
    throw new Error('V5 parent Deployment image digest set is incomplete');
  const live = evidence.live;
  let gsalesMode;
  try { gsalesMode = assertGsalesRuntimeState(live?.gsalesFacts ?? {}); }
  catch { throw new Error('V5 parent GSales runtime state is unknown, damaged, or mixed'); }
  if (live?.source !== 'read-only-vps' || live.runtimeSha !== sha || live.miniappSha !== sha ||
      ledger.currentProductionDbHead !== prepare.prismaHead ||
      ledger.agataAlembicHead !== prepare.agataHead ||
      (ledger.gsalesDbHead ?? ledger.gsalesMigrationHead) !== prepare.gsalesMigrationHead ||
      live.prismaHead !== prepare.prismaHead || live.agataHead !== prepare.agataHead ||
      live.gsalesHead !== prepare.gsalesMigrationHead ||
      live.gsalesMigrationSha256 !== prepare.gsalesMigrationSha256 ||
      !live.prismaHead || !live.agataHead || !live.gsalesHead ||
      live.apiHealth !== 'healthy' || live.agataApiHealth !== 'healthy' ||
      live.workerRunning !== true ||
      live.botRunning !== true || live.agataWorkerRunning !== true ||
      live.gsalesRuntimeState !== gsalesMode)
    throw new Error('V5 parent migration or service health proof is stale or mismatched');
  for (const role of roles) {
    const actual = live.nativeImages?.[role];
    if (actual?.imageId !== images[role].nativeId || actual?.sourceSha !== sha ||
        actual?.platform !== 'linux/amd64')
      throw new Error('V5 parent VPS native image identity mismatch ' + role);
  }
  const parent = { currentProductionSha: sha, lastReleaseStatus: 'COMPLETE',
    serverDeployStatus: 'COMPLETE', releaseOrigin: 'verified-github-deployment',
    recoveryRequired: false, images };
  verifiedV5Parents.add(parent);
  return parent;
}

export function buildV5ComponentImageInventory(prepare, runtimeIdentities, candidate) {
  checkSHA(candidate);
  if (prepare?.selectedTransport !== 'ghcr-oci' || prepare.ghcrVerified !== true ||
      prepare.ghcrPostSwitchVerified !== true || prepare.candidateSha !== candidate ||
      !prepare.images || !runtimeIdentities)
    throw new Error('V5 Prepare and post-switch GHCR identity proof is incomplete');
  return Object.fromEntries(roles.map(role => {
    const image = prepare.images[role];
    const runtime = runtimeIdentities[role];
    checkID(image?.digest); checkID(image?.configDigest); checkSHA(image?.sourceSha);
    if (image.sourceSha !== candidate || image.configDigest !== image.id ||
        image.platform !== 'linux/amd64' || runtime?.imageId !== image.configDigest ||
        runtime?.buildSha !== candidate)
      throw new Error('V5 GHCR and post-switch Docker identity mismatch ' + role);
    return [role, {name: names[role], sourceSha: candidate, imageId: image.configDigest,
      registryDigest: image.digest, platform: 'linux/amd64'}];
  }));
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
  const verifiedV5Parent = completeParent?.releaseOrigin === 'verified-github-deployment' &&
    verifiedV5Parents.has(completeParent);
  if (completeParent?.currentProductionSha !== baseline ||
      completeParent?.lastReleaseStatus !== 'COMPLETE' ||
      completeParent?.serverDeployStatus !== 'COMPLETE' ||
      !(completeParent?.releaseOrigin === 'durable-production-release' || verifiedV5Parent) ||
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
    if (actual?.nativeImageId !== (expected.nativeId ?? expected.imageId) ||
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
