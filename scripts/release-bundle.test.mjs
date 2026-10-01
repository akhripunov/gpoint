import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { verifyReleaseBundle, importBundleCommand } from './release-bundle.mjs';
import { migrationHead } from './release-migrations.mjs';

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-bundle-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const sha = 'a'.repeat(40), id = `sha256:${'b'.repeat(64)}`;
  const bytes = Buffer.from('a disposable image archive');
  fs.writeFileSync(path.join(dir, 'images.tar.gz'), bytes);
  const receipt = { schema: 1, candidateSha: sha, status: 'READY', runId: 1,
    gates: Object.fromEntries(['unit', 'postgres', 'docker', 'migration', 'miniapp', 'agataHealth'].map(g => [g, 'PASS'])),
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
test('different candidate and skipped gates fail closed', t => {
  const f = fixture(t);
  assert.throws(() => verifyReleaseBundle(f.dir, 'c'.repeat(40)), /exact candidate/);
  f.receipt.gates.postgres = 'SKIPPED'; f.save();
  assert.throws(() => verifyReleaseBundle(f.dir, f.sha), /postgres/);
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
