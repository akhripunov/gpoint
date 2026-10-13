import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

// The workflow independently verifies the successful GitHub run before using
// this receipt. A receipt alone is not a GitHub provenance check.
export function verifyReleaseBundle(directory, candidate) {
  if (!/^[a-f0-9]{40}$/.test(candidate)) throw new Error('invalid candidate SHA');
  const root = fs.realpathSync(directory);
  const proof = JSON.parse(fs.readFileSync(path.join(root, 'receipt.json'), 'utf8'));
  if (proof.schema === 2) return verifyV4StaticBundle(root, candidate, proof);
  if (proof.schema !== 1 || proof.candidateSha !== candidate || proof.status !== 'READY') {
    throw new Error('release bundle is not READY for the exact candidate');
  }
  if (!Number.isSafeInteger(proof.runId) || proof.runId < 1) throw new Error('invalid CI run ID');
  if (!/^[A-Za-z0-9_]{1,32}$/.test(proof.agataHead ?? '') || !/^[A-Za-z0-9_]{1,128}$/.test(proof.prismaHead ?? '')) {
    throw new Error('invalid candidate migration heads');
  }
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(proof.gsalesMigrationHead ?? '') || !/^[a-f0-9]{64}$/.test(proof.gsalesMigrationSha256 ?? '')) {
    throw new Error('invalid G Sales migration identity');
  }
  // V3 proves unchanged backend source on the exact candidate in trusted CI.
  // Generic SKIPPED must never satisfy the immutable receipt gate.
  const sourceGates = ['postgres', 'gsalesPostgres'];
  const covered = sourceGates.filter(g => proof.gates?.[g] === 'COVERED_UNCHANGED');
  if (covered.length) {
    const impact = proof.impactV3;
    const safeUI = p => typeof p === 'string' &&
      (((p.startsWith('apps/miniapp/src/') || p.startsWith('apps/miniapp/public/')) &&
       p.split('/').length >= 4 && !p.split('/').some(x => x === '.' || x === '..' || !x) && !p.includes('\0')) ||
       p === 'apps/miniapp/index.html');
    if (covered.length !== sourceGates.length ||
        impact?.version !== 1 || impact?.candidateSha !== candidate ||
        impact?.classification !== 'UI_ONLY' ||
        impact?.baselineSource !== 'gpoint-release-ledger' ||
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
  for (const gate of ['unit', 'postgres', 'gsalesPostgres', 'gsalesImage', 'gsalesMigrationChecksum', 'docker', 'migration', 'miniapp', 'agataHealth', 'deployRehearsal']) {
    if (proof.gates?.[gate] !== 'PASS' &&
        !(sourceGates.includes(gate) && proof.gates?.[gate] === 'COVERED_UNCHANGED' && covered.length === 2)) {
      throw new Error('missing release gate: ' + gate);
    }
  }
  for (const role of ['app', 'agata', 'gsales']) {
    const image = proof.images?.[role];
    if (!image || !/^sha256:[a-f0-9]{64}$/.test(image.id) || image.revision !== candidate || image.platform !== 'linux/amd64') {
      throw new Error(`invalid ${role} image identity`);
    }
  }
  if (!/^[a-f0-9]{64}$/.test(proof.archiveSha256)) throw new Error('invalid archive checksum');
  const archive = path.join(root, 'images.tar.gz');
  if (fs.realpathSync(archive) !== archive) throw new Error('archive must not be a symlink');
  const hash = crypto.createHash('sha256');
  const fd = fs.openSync(archive, 'r');
  try {
    const chunk = Buffer.alloc(1024 * 1024);
    let count;
    while ((count = fs.readSync(fd, chunk, 0, chunk.length, null))) hash.update(chunk.subarray(0, count));
  } finally { fs.closeSync(fd); }
  if (hash.digest('hex') !== proof.archiveSha256) throw new Error('release archive checksum mismatch');
  return { ...proof, archive };
}

const SHA40_V4 = /^[a-f0-9]{40}$/;
const SHA64_V4 = /^[a-f0-9]{64}$/;

export function verifyV4StaticBundle(root, candidate, proof) {
  if (proof.schema !== 2 || proof.route !== 'STATIC_UI_ONLY' ||
      proof.staticArtifactRoute !== 'V4_FAST_STATIC' ||
      proof.status !== 'READY' || proof.candidateSha !== candidate ||
      !SHA40_V4.test(proof.productionBaselineSha ?? '') ||
      proof.productionBaselineSha === candidate ||
      !Number.isSafeInteger(proof.runId) || proof.runId < 1 ||
      !Number.isSafeInteger(proof.runAttempt) || proof.runAttempt < 1 ||
      proof.staticArtifactName !== 'miniapp.zip' ||
      !SHA64_V4.test(proof.staticArtifactSha256 ?? ''))
    throw new Error('invalid exact static V4 identity');
  const impact = proof.impactV3;
  const v4 = proof.impactV4;
  if (impact?.version !== 1 || impact?.candidateSha !== candidate ||
      impact?.productionBaselineSha !== proof.productionBaselineSha ||
      impact?.classification !== 'UI_ONLY' ||
      impact?.reason !== 'narrow-unchanged-backend' ||
      impact?.baselineSource !== 'gpoint-release-ledger' ||
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
  return { ...proof, archive, staticOnly: true };
}

function temporaryPath(root, name) {
  if (!/^\/[A-Za-z0-9_./-]+$/.test(root) || root.split('/').includes('..')) throw new Error('invalid release scratch path');
  return `${root.replace(/\/$/, '')}/${name}`;
}

export function importBundleCommand(proof, candidate, scratch = '/tmp') {
  // Only validated values may enter the remote shell.
  if (proof.candidateSha !== candidate || !/^[a-f0-9]{40}$/.test(candidate) || !/^[a-f0-9]{64}$/.test(proof.archiveSha256)) throw new Error('invalid bundle command');
  for (const role of ['app', 'agata', 'gsales']) {
    const id = proof.images[role].id;
    if (!/^sha256:[a-f0-9]{64}$/.test(id)) throw new Error('invalid image ID');
  }
  const archive = temporaryPath(scratch, `gpoint-images-${candidate}.tar.gz`);
  return `set -e; printf '%s  %s\\n' '${proof.archiveSha256}' '${archive}' | sha256sum -c -; gzip -dc ${archive} | docker load; ${loadedImageVerificationCommand(proof, candidate)}; rm ${archive}; echo GPOINT_CI_IMAGES_VERIFIED`;
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
  // Recorded runtime proofs use the VPS store's native IDs. Re-prove their
  // exact config identity and then compare native IDs within that same store.
  return `set -e; ${verification}; test "$(cat ${temporaryPath(scratch, `gpoint-image-id-${candidate}`)})" = "$(docker image inspect gpoint-runtime:${candidate} --format '{{.Id}}')"; test "$(cat ${temporaryPath(scratch, `gpoint-agata-image-id-${candidate}`)})" = "$(docker image inspect gpoint-agata-runtime:${candidate} --format '{{.Id}}')"; test "$(cat ${temporaryPath(scratch, `gpoint-g-sales-image-id-${candidate}`)})" = "$(docker image inspect gpoint-g-sales-runtime:${candidate} --format '{{.Id}}')"; echo GPOINT_CI_PARITY_OK`;
}

export function reuseBundleCommand(proof, candidate) {
  // Validate command inputs using the same gate as a fresh import.
  importBundleCommand(proof, candidate);
  return `if ${loadedImageVerificationCommand(proof, candidate)}; then echo GPOINT_CI_IMAGES_REUSABLE; else echo GPOINT_CI_IMAGES_IMPORT_REQUIRED; fi`;
}
