import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { validateOciReceipt } from './release-oci.mjs';

// The workflow independently verifies the successful GitHub run before using
// this receipt. A receipt alone is not a GitHub provenance check.
export function verifyReleaseBundle(directory, candidate, expectedReceiptDigest = process.env.GHCR_RECEIPT_DIGEST) {
  if (!/^[a-f0-9]{40}$/.test(candidate)) throw new Error('invalid candidate SHA');
  const root = fs.realpathSync(directory);
  const proof = JSON.parse(fs.readFileSync(path.join(root, 'receipt.json'), 'utf8'));
  let evidence;
  try { evidence = JSON.parse(fs.readFileSync(path.join(root, 'ghcr-evidence.json'), 'utf8')); }
  catch { throw new Error('GHCR receipt transport evidence is missing or invalid'); }
  if (!/^sha256:[a-f0-9]{64}$/.test(expectedReceiptDigest ?? '') ||
      evidence.schema !== 1 || evidence.manifestDigest !== expectedReceiptDigest ||
      evidence.candidateSha !== candidate || String(evidence.runId) !== String(proof.runId) ||
      String(evidence.runAttempt) !== String(proof.runAttempt) ||
      evidence.runLogProvenance !== 'PASS')
    throw new Error('Prepare receipt is not bound to the verified GHCR manifest and provenance');
  if (proof.schema === 2) throw new Error('static-only GHCR receipt requires a separately verified Mini App OCI artifact');
  if (proof.schema !== 3) throw new Error('legacy release receipts are disabled; Prepare must publish the GHCR OCI receipt');
  if (proof.staticArtifactRoute === 'V4_FAST_STATIC') {
    const transportEvidence = evidence;
    if (transportEvidence.staticArtifactRunLogProvenance !== 'PASS' ||
        transportEvidence.staticArtifactDigest !== proof.staticArtifact?.digest)
      throw new Error('V4 static Mini App OCI provenance is not verified');
    return verifyV4StaticBundle(root, candidate, proof, transportEvidence);
  }
  if (proof.route === 'STATIC_UI_ONLY')
    throw new Error('static-only candidate is not eligible without the V4_FAST_STATIC OCI route');
  return verifyOciReleaseBundle(root, candidate, proof);
}

export function selectImageTransport(proof, requested = 'prepared') {
  if (!['prepared', 'ghcr-oci'].includes(requested) || proof?.transport !== 'ghcr-oci')
    throw new Error('GPoint Deploy requires the verified GHCR OCI transport');
  return 'ghcr-oci';
}

function verifyOciReleaseBundle(root, candidate, proof) {
  const validated = validateOciReceipt(proof, candidate,
    { runId: proof.runId, runAttempt: proof.runAttempt });
  const gates = ['unit', 'postgres', 'gsalesPostgres', 'gsalesImage', 'gsalesMigrationChecksum',
    'docker', 'migration', 'miniapp', 'agataHealth', 'botImageStartup', 'deployRehearsal'];
  const sourceGates = ['postgres', 'gsalesPostgres'];
  const covered = sourceGates.filter(g => proof.gates?.[g] === 'COVERED_UNCHANGED');
  if (covered.length) validateV3CoveredUnchanged(proof, candidate, sourceGates, covered);
  for (const gate of gates) {
    if (proof.gates?.[gate] !== 'PASS' &&
        !(sourceGates.includes(gate) && proof.gates?.[gate] === 'COVERED_UNCHANGED' && covered.length === sourceGates.length)) {
      throw new Error('missing OCI release gate: ' + gate);
    }
  }
  if (!/^[A-Za-z0-9_]{1,32}$/.test(proof.agataHead ?? '') ||
      !/^[A-Za-z0-9_]{1,128}$/.test(proof.prismaHead ?? '') ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(proof.gsalesMigrationHead ?? '') ||
      !/^[a-f0-9]{64}$/.test(proof.gsalesMigrationSha256 ?? '')) {
    throw new Error('invalid OCI candidate migration heads');
  }
  const images = Object.fromEntries(Object.entries(validated.images).map(([role, image]) => [role, {
    ...image, id: image.configDigest, revision: image.sourceSha,
  }]));
  return { ...validated, images, archive: null, ghcrVerified: true };
}

function validateV3CoveredUnchanged(proof, candidate, sourceGates, covered) {
  const impact = proof.impactV3;
  const safeUI = p => typeof p === 'string' &&
    (((p.startsWith('apps/miniapp/src/') || p.startsWith('apps/miniapp/public/')) &&
     p.split('/').length >= 4 && !p.split('/').some(x => x === '.' || x === '..' || !x) && !p.includes('\0')) ||
     p === 'apps/miniapp/index.html');
  if (covered.length !== sourceGates.length ||
      impact?.version !== 1 || impact?.candidateSha !== candidate ||
      impact?.classification !== 'UI_ONLY' ||
      impact?.baselineSource !== 'github-deployment-vps-readback' ||
      impact?.reason !== 'narrow-unchanged-backend' ||
      !/^[a-f0-9]{40}$/.test(impact?.productionBaselineSha ?? '') ||
      impact.productionBaselineSha === candidate ||
      impact.checks?.agata_postgres_rehearsal !== false ||
      impact.checks?.gsales_postgres_regression !== false ||
      !Array.isArray(impact.changedPaths) || impact.changedPaths.length === 0 ||
      !impact.changedPaths.every(safeUI) ||
      !Array.isArray(impact.invariantChecks) ||
      !['docker_images_exact_sha', 'artifact_migrations', 'deploy_rehearsal', 'canonical_preflight', 'production_approval']
        .every(k => impact.invariantChecks.includes(k))) {
    throw new Error('unverified V3 unchanged-source coverage');
  }
}

const SHA40_V4 = /^[a-f0-9]{40}$/;
const SHA64_V4 = /^[a-f0-9]{64}$/;

export function verifyV4StaticBundle(root, candidate, proof, transportEvidence = {}) {
  if (proof.schema !== 3 || proof.transport !== 'ghcr-oci' || proof.route !== 'STATIC_UI_ONLY' ||
      proof.staticArtifactRoute !== 'V4_FAST_STATIC' ||
      proof.status !== 'READY' || proof.candidateSha !== candidate ||
      !SHA40_V4.test(proof.productionBaselineSha ?? '') ||
      proof.productionBaselineSha === candidate ||
      !Number.isSafeInteger(proof.runId) || proof.runId < 1 ||
      !Number.isSafeInteger(proof.runAttempt) || proof.runAttempt < 1 ||
      proof.staticArtifactName !== 'miniapp.zip' ||
      !SHA64_V4.test(proof.staticArtifactSha256 ?? ''))
    throw new Error('invalid exact static V4 identity');
  const descriptor = proof.staticArtifact;
  if (descriptor?.repository !== 'ghcr.io/akhripunov/gpoint-miniapp-artifacts' ||
      descriptor?.digest !== transportEvidence.staticArtifactDigest ||
      descriptor?.contentSha256 !== proof.staticArtifactSha256 ||
      descriptor?.sourceSha !== candidate || String(descriptor?.runId) !== String(proof.runId) ||
      String(descriptor?.runAttempt) !== String(proof.runAttempt))
    throw new Error('V4 Mini App OCI descriptor does not match the attested Prepare receipt');
  const impact = proof.impactV3;
  const v4 = proof.impactV4;
  if (impact?.version !== 1 || impact?.candidateSha !== candidate ||
      impact?.productionBaselineSha !== proof.productionBaselineSha ||
      impact?.classification !== 'UI_ONLY' ||
      impact?.reason !== 'narrow-unchanged-backend' ||
      impact?.baselineSource !== 'github-deployment-vps-readback' ||
      impact?.checks?.agata_postgres_rehearsal !== false ||
      impact?.checks?.gsales_postgres_regression !== false ||
      !Array.isArray(impact.changedPaths) || !impact.changedPaths.length ||
      impact.changedPaths.some(p => typeof p !== 'string' ||
        !((p.startsWith('apps/miniapp/src/') || p.startsWith('apps/miniapp/public/')) &&
          p.split('/').length >= 4 && p.split('/').every(s => s && s !== '.' && s !== '..') ||
          p === 'apps/miniapp/index.html')) ||
      v4?.classification !== 'COMPONENT_CANDIDATE' ||
      v4?.component !== 'ui' || v4?.candidateSha !== candidate ||
      v4?.productionBaselineSha !== proof.productionBaselineSha ||
      v4?.baselineProvenance !== 'immutable-COMPLETE' ||
      v4?.mayDeploySelectively !== false || v4?.maySkipV3Gates !== false ||
      JSON.stringify([...v4.changedPaths].sort()) !== JSON.stringify([...impact.changedPaths].sort()))
    throw new Error('unverified static source scope');
  for (const gate of ['sourceDiff', 'miniappBuild', 'miniappTests', 'releaseTests',
                      'staticArchive', 'migrationUnchanged']) {
    if (proof.gates?.[gate] !== 'PASS') throw new Error('missing V4 source gate: ' + gate);
  }
  if (!/^[A-Za-z0-9_]{1,32}$/.test(proof.agataHead ?? '') ||
      !/^[A-Za-z0-9_]{1,128}$/.test(proof.prismaHead ?? '') ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(proof.gsalesMigrationHead ?? '') ||
      !SHA64_V4.test(proof.gsalesMigrationSha256 ?? ''))
    throw new Error('missing static candidate migration source identity');
  const archive = path.join(root, 'miniapp.zip');
  if (!fs.existsSync(archive) || fs.realpathSync(archive) !== archive ||
      !fs.statSync(archive).isFile()) throw new Error('untrusted static archive');
  const hash = crypto.createHash('sha256');
  const fd = fs.openSync(archive, 'r');
  try {
    const block = Buffer.alloc(65536);
    let count;
    while ((count = fs.readSync(fd, block, 0, block.length, null)) > 0)
      hash.update(block.subarray(0, count));
  } finally { fs.closeSync(fd); }
  if (hash.digest('hex') !== proof.staticArtifactSha256)
    throw new Error('static artifact checksum mismatch');
  // Verify inner ZIP inventory and every member's SHA256 before release.
  const script = "import sys; from release_ui_artifact_v4 import verify_bundle; verify_bundle(__import__('pathlib').Path(sys.argv[1]),sys.argv[2])";
  try {
    execFileSync('python3', ['-c', script, archive, candidate],
      { cwd: path.dirname(new URL(import.meta.url).pathname), stdio: 'pipe', timeout: 30_000 });
  } catch {
    throw new Error('static ZIP member identity invalid');
  }
  return { ...proof, archive, staticOnly: true,
    ghcrManifestDigest: transportEvidence.manifestDigest,
    staticArtifactDigest: descriptor.digest };
}

function temporaryPath(root, name) {
  if (!/^\/[A-Za-z0-9_./-]+$/.test(root) || root.split('/').includes('..')) throw new Error('invalid release scratch path');
  return `${root.replace(/\/$/, '')}/${name}`;
}

export function loadedImageVerificationCommand(proof, candidate) {
  if (proof.candidateSha !== candidate || !/^[a-f0-9]{40}$/.test(candidate) ||
      ['app', 'agata', 'gsales'].some(role => !/^sha256:[a-f0-9]{64}$/.test(proof.images?.[role]?.id))) throw new Error('invalid image identity command');
  const code = fs.readFileSync(new URL('./verify-loaded-images.py', import.meta.url)).toString('base64');
  return `python3 -c "import base64;exec(compile(base64.b64decode('${code}'), 'verify-loaded-images.py', 'exec'))" '${candidate}' '${proof.images.app.id}' '${proof.images.agata.id}' '${proof.images.gsales.id}'`;
}

export function preparedImageCaptureCommand(proof, candidate, scratch = '/tmp') {
  const verification = loadedImageVerificationCommand(proof, candidate);
  return `set -e; ${verification}; docker image inspect gpoint-runtime:${candidate} --format '{{.Id}}' > ${temporaryPath(scratch, `gpoint-image-id-${candidate}`)}; docker image inspect gpoint-agata-runtime:${candidate} --format '{{.Id}}' > ${temporaryPath(scratch, `gpoint-agata-image-id-${candidate}`)}; docker image inspect gpoint-g-sales-runtime:${candidate} --format '{{.Id}}' > ${temporaryPath(scratch, `gpoint-g-sales-image-id-${candidate}`)}; echo GPOINT_CI_IMAGE_REUSED`;
}

export function preparedImageParityCommand(proof, candidate, scratch = '/tmp') {
  const verification = loadedImageVerificationCommand(proof, candidate);
  const identityChecks = [
    ['app', 'gpoint-runtime'], ['agata', 'gpoint-agata-runtime'], ['gsales', 'gpoint-g-sales-runtime'],
  ].map(([role, repository]) => {
    const prefix = repository === 'gpoint-runtime' ? 'gpoint' : repository === 'gpoint-agata-runtime' ? 'gpoint-agata' : 'gpoint-g-sales';
    const imageIdFile = temporaryPath(scratch, `${prefix}-image-id-${candidate}`);
    return `actual="$(docker image inspect ${repository}:${candidate} --format '{{.Id}}')" || { echo GPOINT_CI_PARITY_INSPECT_FAILED=${role}; exit 43; }; recorded="$(cat ${imageIdFile})" || { echo GPOINT_CI_PARITY_RECORD_READ_FAILED=${role}; exit 44; }; [[ "$actual" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo GPOINT_CI_PARITY_INSPECT_INVALID=${role}; exit 45; }; [[ "$recorded" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo GPOINT_CI_PARITY_RECORD_INVALID=${role}; exit 46; }; test "$recorded" = "$actual" || { echo GPOINT_CI_PARITY_IDENTITY_MISMATCH=${role}; exit 42; }`;
  }).join('; ');
  // Recorded runtime proofs use the VPS store's native IDs. Re-prove their
  // exact config identity and then compare native IDs within that same store.
  return `set -e; ${verification}; ${identityChecks}; echo GPOINT_CI_PARITY_OK`;
}
