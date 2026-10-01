import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

// The workflow independently verifies the successful GitHub run before using
// this receipt. A receipt alone is not a GitHub provenance check.
export function verifyReleaseBundle(directory, candidate) {
  if (!/^[a-f0-9]{40}$/.test(candidate)) throw new Error('invalid candidate SHA');
  const root = fs.realpathSync(directory);
  const proof = JSON.parse(fs.readFileSync(path.join(root, 'receipt.json'), 'utf8'));
  if (proof.schema !== 1 || proof.candidateSha !== candidate || proof.status !== 'READY') {
    throw new Error('release bundle is not READY for the exact candidate');
  }
  if (!Number.isSafeInteger(proof.runId) || proof.runId < 1) throw new Error('invalid CI run ID');
  if (!/^[A-Za-z0-9_]{1,32}$/.test(proof.agataHead ?? '') || !/^[A-Za-z0-9_]{1,128}$/.test(proof.prismaHead ?? '')) {
    throw new Error('invalid candidate migration heads');
  }
  for (const gate of ['unit', 'postgres', 'docker', 'migration', 'miniapp', 'agataHealth']) {
    if (proof.gates?.[gate] !== 'PASS') throw new Error(`missing release gate: ${gate}`);
  }
  for (const role of ['app', 'agata']) {
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

export function importBundleCommand(proof, candidate) {
  // Only validated values may enter the remote shell.
  if (proof.candidateSha !== candidate || !/^[a-f0-9]{40}$/.test(candidate) || !/^[a-f0-9]{64}$/.test(proof.archiveSha256)) throw new Error('invalid bundle command');
  const checks = ['app', 'agata'].map(role => {
    const id = proof.images[role].id;
    if (!/^sha256:[a-f0-9]{64}$/.test(id)) throw new Error('invalid image ID');
    const tag = `${role === 'app' ? 'gpoint-runtime' : 'gpoint-agata-runtime'}:${candidate}`;
    return `test "$(docker image inspect ${tag} --format '{{.Id}}')" = '${id}'; test "$(docker image inspect ${tag} --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = '${candidate}'`;
  });
  return `set -e; printf '%s  %s\\n' '${proof.archiveSha256}' '/tmp/gpoint-images-${candidate}.tar.gz' | sha256sum -c -; gzip -dc /tmp/gpoint-images-${candidate}.tar.gz | docker load; ${checks.join('; ')}; rm /tmp/gpoint-images-${candidate}.tar.gz; echo GPOINT_CI_IMAGES_VERIFIED`;
}

export function reuseBundleCommand(proof, candidate) {
  // Validate command inputs using the same gate as a fresh import.
  importBundleCommand(proof, candidate);
  const checks = ['app', 'agata'].map(role => {
    const tag = `${role === 'app' ? 'gpoint-runtime' : 'gpoint-agata-runtime'}:${candidate}`;
    return `test "$(docker image inspect ${tag} --format '{{.Id}}' 2>/dev/null)" = '${proof.images[role].id}' && test "$(docker image inspect ${tag} --format '{{.Os}}/{{.Architecture}}')" = linux/amd64 && test "$(docker image inspect ${tag} --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = '${candidate}'`;
  });
  return `if ${checks.join(' && ')}; then echo GPOINT_CI_IMAGES_REUSABLE; else echo GPOINT_CI_IMAGES_IMPORT_REQUIRED; fi`;
}
