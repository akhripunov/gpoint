import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { verifyReleaseBundle, importBundleCommand, reuseBundleCommand, preparedImageParityCommand } from './release-bundle.mjs';
import { migrationHead } from './release-migrations.mjs';
import { acquireReleaseLock } from './release-lock.mjs';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-bundle-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const sha = 'a'.repeat(40), id = `sha256:${'b'.repeat(64)}`;
  const bytes = Buffer.from('a disposable image archive');
  fs.writeFileSync(path.join(dir, 'images.tar.gz'), bytes);
  const receipt = { schema: 1, candidateSha: sha, status: 'READY', runId: 1, agataHead: 'agata0008', prismaHead: '20260922090000_telegram_channel_avatar',
    gates: Object.fromEntries(['unit', 'postgres', 'docker', 'migration', 'miniapp', 'agataHealth', 'deployRehearsal'].map(g => [g, 'PASS'])),
    images: { app: { id, revision: sha, platform: 'linux/amd64' }, agata: { id, revision: sha, platform: 'linux/amd64' } },
    archiveSha256: crypto.createHash('sha256').update(bytes).digest('hex') };
  const save = () => fs.writeFileSync(path.join(dir, 'receipt.json'), JSON.stringify(receipt));
  save();
  return { dir, sha, receipt, save };
}

test('verified archive preserves exact image IDs in the import command', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, f.sha);
  const command = importBundleCommand(proof, f.sha);
  assert.ok(command.includes(proof.images.app.id));
  assert.ok(command.includes(proof.archiveSha256));
  assert.ok(command.includes('sha256sum -c -'));
  assert.equal(command.includes('docker build'), false);
});
test('archive corruption fails before upload or mutation', t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.dir, 'images.tar.gz'), 'corruption');
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /checksum mismatch/);
});
test('VPS reuse requires both exact image IDs, source labels and platform', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, f.sha);
  const docker = path.join(f.dir, 'docker');
  fs.writeFileSync(docker, '#!/bin/sh\nprintf \'[{"Id":"%s","Os":"linux","Architecture":"%s","Config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}]\' "$FAKE_ID" "$FAKE_ARCH" "$FAKE_SHA"\n', { mode: 0o700 });
  const run = changes => spawnSync('bash', ['-c', reuseBundleCommand(proof, f.sha)], {
    encoding: 'utf8', env: { ...process.env, PATH: `${f.dir}:${process.env.PATH}`,
      FAKE_ARCH: 'amd64', FAKE_SHA: f.sha, FAKE_ID: proof.images.app.id, ...changes },
  });
  assert.match(run({}).stdout, /GPOINT_CI_IMAGES_REUSABLE/);
  for (const changes of [{ FAKE_ID: `sha256:${'c'.repeat(64)}` }, { FAKE_ARCH: 'arm64' }, { FAKE_SHA: 'c'.repeat(40) }]) {
    assert.match(run(changes).stdout, /GPOINT_CI_IMAGES_IMPORT_REQUIRED/);
  }
});

test('containerd manifest IDs require exact hashed config, source, platform and rootfs', t => {
  const f = fixture(t);
  const hash = bytes => `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`;
  const layers = [`sha256:${'d'.repeat(64)}`];
  const config = Buffer.from(JSON.stringify({ os: 'linux', architecture: 'amd64', config: { Labels: { 'org.opencontainers.image.revision': f.sha } }, rootfs: { diff_ids: layers } }));
  const configId = hash(config);
  for (const role of ['app', 'agata']) f.receipt.images[role].id = configId;
  f.save();
  const proof = verifyReleaseBundle(f.dir, f.sha);
  const manifest = Buffer.from(JSON.stringify({ schemaVersion: 2, config: { digest: configId }, layers: [{ digest: layers[0] }] }));
  const native = hash(manifest);
  fs.writeFileSync(path.join(f.dir, 'manifest'), manifest);
  fs.writeFileSync(path.join(f.dir, 'config'), config);
  const image = { Id: native, Descriptor: { digest: native, mediaType: 'application/vnd.oci.image.manifest.v1+json' }, Os: 'linux', Architecture: 'amd64', Config: { Labels: { 'org.opencontainers.image.revision': f.sha } }, RootFS: { Layers: layers } };
  const imagePath = path.join(f.dir, 'inspect.json');
  fs.writeFileSync(imagePath, JSON.stringify([image]));
  fs.writeFileSync(path.join(f.dir, 'docker'), '#!/bin/sh\nif [ "$4" = "--format" ]; then printf "%s" "$FAKE_NATIVE"; else cat "$FAKE_INSPECT"; fi\n', { mode: 0o700 });
  fs.writeFileSync(path.join(f.dir, 'cat'), '#!/bin/sh\ncase "$1" in /tmp/gpoint-image-id-*|/tmp/gpoint-agata-image-id-*) printf "%s" "$FAKE_CAPTURED" ;; *) /bin/cat "$@" ;; esac\n', { mode: 0o700 });
  fs.writeFileSync(path.join(f.dir, 'ctr'), '#!/bin/sh\nif [ "$5" = "$FAKE_NATIVE" ]; then cat "$FAKE_MANIFEST"; else cat "$FAKE_CONFIG"; fi\n', { mode: 0o700 });
  const run = (command = reuseBundleCommand(proof, f.sha), changes = {}) => spawnSync('bash', ['-c', command], { encoding: 'utf8', env: { ...process.env, PATH: `${f.dir}:${process.env.PATH}`, FAKE_INSPECT: imagePath, FAKE_NATIVE: native, FAKE_CAPTURED: native, FAKE_MANIFEST: path.join(f.dir, 'manifest'), FAKE_CONFIG: path.join(f.dir, 'config'), ...changes } });
  assert.match(run().stdout, /GPOINT_CI_IMAGES_REUSABLE/);
  const parity = run(preparedImageParityCommand(proof, f.sha));
  assert.equal(parity.status, 0, parity.stderr);
  assert.match(parity.stdout, /GPOINT_CI_PARITY_OK/);
  assert.notEqual(run(preparedImageParityCommand(proof, f.sha), { FAKE_CAPTURED: configId }).status, 0);
  fs.appendFileSync(path.join(f.dir, 'config'), 'corruption');
  assert.match(run().stdout, /GPOINT_CI_IMAGES_IMPORT_REQUIRED/);
  fs.writeFileSync(path.join(f.dir, 'config'), config);
  fs.appendFileSync(path.join(f.dir, 'manifest'), 'corruption');
  assert.match(run().stdout, /GPOINT_CI_IMAGES_IMPORT_REQUIRED/);
  fs.writeFileSync(path.join(f.dir, 'manifest'), manifest);
  image.RootFS.Layers = [`sha256:${'e'.repeat(64)}`];
  fs.writeFileSync(imagePath, JSON.stringify([image]));
  assert.match(run().stdout, /GPOINT_CI_IMAGES_IMPORT_REQUIRED/);
  image.RootFS.Layers = layers;
  image.Descriptor.mediaType = 'application/vnd.oci.image.index.v1+json';
  fs.writeFileSync(imagePath, JSON.stringify([image]));
  assert.match(run().stdout, /GPOINT_CI_IMAGES_IMPORT_REQUIRED/);
});
test('different candidate and skipped gates fail closed', t => {
  const f = fixture(t);
  assert.throws(() => verifyReleaseBundle(f.dir, 'c'.repeat(40)), /exact candidate/);
  f.receipt.gates.postgres = 'SKIPPED'; f.save();
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /postgres/);
});
test('a receipt without the real containerd deployment rehearsal cannot authorize apply', t => {
  const f = fixture(t);
  delete f.receipt.gates.deployRehearsal; f.save();
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /deployRehearsal/);
});
test('rehearsal scratch paths cannot introduce shell commands or escape via parent traversal', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, f.sha);
  for (const scratch of ['/tmp/../opt/gpoint', '/tmp;touch /tmp/bypass', 'relative']) {
    assert.throws(() => importBundleCommand(proof, f.sha, scratch), /scratch path/);
    assert.throws(() => preparedImageParityCommand(proof, f.sha, scratch), /scratch path/);
  }
});
test('invalid image identity and another architecture fail closed', t => {
  const f = fixture(t);
  f.receipt.images.agata.id = 'latest; echo bypass'; f.save();
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /identity/);
  f.receipt.images.agata.id = `sha256:${'b'.repeat(64)}`;
  f.receipt.images.agata.platform = 'linux/arm64'; f.save();
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /identity/);
});
test('symlink archives cannot escape the bundle', t => {
  const f = fixture(t);
  fs.renameSync(path.join(f.dir, 'images.tar.gz'), path.join(f.dir, 'elsewhere.gz'));
  fs.symlinkSync('elsewhere.gz', path.join(f.dir, 'images.tar.gz'));
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /symlink/);
});
test('new migration heads are derived from the graph, not a fixed agata0008', () => {
  assert.deepEqual(migrationHead(['revision = "agata0008"\ndown_revision = None', 'revision = "agata0009"\ndown_revision = "agata0008"']),
    { head: 'agata0009', revisions: ['agata0008', 'agata0009'] });
});
test('branched, missing and disconnected migration graphs fail closed', () => {
  assert.throws(() => migrationHead(['revision = "next"\ndown_revision = "absent"']), /ancestor/);
  assert.throws(() => migrationHead(['revision = "a"\ndown_revision = None', 'revision = "b"\ndown_revision = None']), /one candidate/);
  assert.throws(() => migrationHead(['revision = "a"\ndown_revision = "b"', 'revision = "b"\ndown_revision = "a"']), /one candidate/);
});
test('shared lock blocks a second checkout and remains after unrelated checkout cleanup', t => {
  const f = fixture(t), lock = path.join(f.dir, 'outside-checkout.lock');
  const release = acquireReleaseLock(lock, 'first');
  assert.throws(() => acquireReleaseLock(lock, 'second'), /lock exists/);
  fs.rmSync(path.join(f.dir, 'images.tar.gz'));
  assert.ok(fs.existsSync(lock));
  release();
  assert.equal(fs.existsSync(lock), false);
});
test('legacy stale locks block mutation and are never removed silently', t => {
  const f = fixture(t), legacy = path.join(f.dir, 'old.lock');
  fs.writeFileSync(legacy, 'stale');
  assert.throws(() => acquireReleaseLock(path.join(f.dir, 'new.lock'), 'release', legacy), /legacy/);
  assert.equal(fs.readFileSync(legacy, 'utf8'), 'stale');
});
test('release cannot remove a lock whose ownership changed', t => {
  const f = fixture(t), lock = path.join(f.dir, 'lock');
  const release = acquireReleaseLock(lock, 'release');
  fs.writeFileSync(lock, JSON.stringify({ owner: 'another-operator' }));
  assert.throws(release, /owner changed/);
  assert.ok(fs.existsSync(lock));
});
test('canonical CLI executes from a Mac runner path containing spaces', t => {
  const f = fixture(t), root = path.join(f.dir, 'Application Support', 'GPoint');
  fs.mkdirSync(path.join(root, 'scripts'), { recursive: true });
  for (const name of ['release-gpoint.mjs', 'release-gpoint-vps-adapter.mjs', 'release-bundle.mjs', 'verify-loaded-images.py', 'release-lock.mjs', 'release-migrations.mjs']) {
    fs.copyFileSync(fileURLToPath(new URL(name, import.meta.url)), path.join(root, 'scripts', name));
  }
  fs.mkdirSync(path.join(root, 'docs/release/state'), { recursive: true });
  fs.mkdirSync(path.join(root, 'docs/release/manifests'), { recursive: true });
  fs.writeFileSync(path.join(root, 'docs/release/state/production.json'), JSON.stringify({ currentProductionSha: f.sha }));
  const run = spawnSync(process.execPath, [path.join(root, 'scripts/release-gpoint.mjs'), 'status'], { encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  assert.equal(JSON.parse(run.stdout).state.currentProductionSha, f.sha);
});
