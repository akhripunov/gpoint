import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFileSync, spawnSync} from 'node:child_process';
import {verifyReleaseBundle, selectImageTransport, preparedImageParityCommand} from './release-bundle.mjs';
import {verifyOciReadAccess} from './release-oci.mjs';

const sha = 'a'.repeat(40);
const manifestDigest = `sha256:${'b'.repeat(64)}`;
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-ghcr-bundle-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const gates = Object.fromEntries(['unit', 'postgres', 'gsalesPostgres', 'gsalesImage',
    'gsalesMigrationChecksum', 'docker', 'migration', 'miniapp', 'agataHealth',
    'botImageStartup', 'deployRehearsal'].map(name => [name, 'PASS']));
  const images = Object.fromEntries(['app', 'agata', 'gsales'].map((role, index) => [role, {
    repository: `ghcr.io/akhripunov/${['gpoint-runtime', 'gpoint-agata-runtime', 'gpoint-g-sales-runtime'][index]}`,
    digest: `sha256:${String(index + 1).repeat(64)}`, configDigest: `sha256:${String(index + 4).repeat(64)}`,
    sourceSha: sha, platform: 'linux/amd64',
  }]));
  const receipt = {schema: 3, transport: 'ghcr-oci', candidateSha: sha, status: 'READY',
    runId: 123, runAttempt: 2, agataHead: 'agata0012', prismaHead: '20261001000000_candidate',
    gsalesMigrationHead: '001_gsales', gsalesMigrationSha256: 'c'.repeat(64), gates, images};
  fs.writeFileSync(path.join(dir, 'receipt.json'), JSON.stringify(receipt));
  fs.writeFileSync(path.join(dir, 'ghcr-evidence.json'), JSON.stringify({schema: 1,
    candidateSha: sha, runId: 123, runAttempt: 2, manifestDigest, receiptSha256: 'd'.repeat(64), runLogProvenance: 'PASS'}));
  process.env.GHCR_RECEIPT_DIGEST = manifestDigest;
  return {dir, receipt};
}

test('GHCR-only receipt requires exact Prepare manifest digest, run, attempt and publisher log provenance', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, sha);
  assert.equal(proof.ghcrVerified, true);
  assert.equal(proof.images.app.reference, `${f.receipt.images.app.repository}@${f.receipt.images.app.digest}`);
  assert.equal(selectImageTransport(proof, 'prepared'), 'ghcr-oci');
  assert.throws(() => selectImageTransport(proof, 'archive'), /GHCR OCI transport/);
  assert.throws(() => verifyReleaseBundle(f.dir, 'f'.repeat(40)), /not bound/);
});

test('optional stock Eros pilot is verified in the receipt but excluded from default production images', async t => {
  const f = fixture(t);
  const pilot = {repository: 'ghcr.io/akhripunov/gpoint-eros-pilot',
    digest: `sha256:${'e'.repeat(64)}`, configDigest: `sha256:${'f'.repeat(64)}`,
    sourceSha: sha, upstreamSha: 'd1a140083321687212fb7b17c09cc0c3c9639f5e',
    patchSetSha256: 'none', platform: 'linux/amd64'};
  f.receipt.pilotImage = {id: pilot.configDigest, sourceSha: sha,
    upstreamSha: pilot.upstreamSha, patchSetSha256: 'none', platform: 'linux/amd64'};
  f.receipt.pilotImages = {eros: pilot};
  f.receipt.gates.erosPilotImage = 'PASS';
  f.receipt.gates.erosPilotGhcrReadback = 'PASS';
  f.receipt.gates.erosStockMigrationApiE2E = 'PASS';
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  const proof = verifyReleaseBundle(f.dir, sha);
  assert.equal(proof.pilotImages.eros.reference,
    `${pilot.repository}@${pilot.digest}`);
  assert.equal(Object.keys(proof.images).join(','), 'app,agata,gsales');
  const requests = [];
  await verifyOciReadAccess(proof, 'akhripunov', 'read-token', async (url) => {
    requests.push(url);
    const digest = url.split('/manifests/')[1];
    return {ok: true, status: 200, url,
      headers: new Headers({'Docker-Content-Digest': digest})};
  });
  assert.equal(requests.length, 4);
  assert.ok(requests.some(url => url.includes('/gpoint-eros-pilot/manifests/')));
  f.receipt.pilotImages.eros.upstreamSha = 'f'.repeat(40);
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /Eros pilot image proof/);
});

test('Eros image receipt is rejected unless Prepare ran the stock migration/API E2E gate', t => {
  const f = fixture(t);
  f.receipt.pilotImage = {id: `sha256:${'f'.repeat(64)}`, sourceSha: sha,
    upstreamSha: 'd1a140083321687212fb7b17c09cc0c3c9639f5e', patchSetSha256: 'none', platform: 'linux/amd64'};
  f.receipt.pilotImages = {eros: {repository: 'ghcr.io/akhripunov/gpoint-eros-pilot',
    digest: `sha256:${'e'.repeat(64)}`, configDigest: `sha256:${'f'.repeat(64)}`, sourceSha: sha,
    upstreamSha: 'd1a140083321687212fb7b17c09cc0c3c9639f5e', patchSetSha256: 'none', platform: 'linux/amd64'}};
  f.receipt.gates.erosPilotImage = 'PASS';
  f.receipt.gates.erosPilotGhcrReadback = 'PASS';
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /stock Eros migration\/API E2E gate/);
});

test('legacy receipt, absent evidence, digest mismatch and untrusted provenance block', t => {
  const f = fixture(t);
  f.receipt.schema = 1;
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /legacy release receipts/);
  f.receipt.schema = 3;
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  fs.unlinkSync(path.join(f.dir, 'ghcr-evidence.json'));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /GHCR receipt transport evidence/);
  fs.writeFileSync(path.join(f.dir, 'ghcr-evidence.json'), JSON.stringify({schema: 1,
    candidateSha: sha, runId: 123, runAttempt: 2, manifestDigest: `sha256:${'e'.repeat(64)}`, runLogProvenance: 'PASS'}));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /not bound/);
});

test('OCI candidate image parity command is digest-only and archive transport is unavailable', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, sha);
  const command = preparedImageParityCommand(proof, sha);
  assert.match(command, /docker image inspect/);
  assert.match(command, /GPOINT_CI_PARITY_IDENTITY_MISMATCH=app/);
  assert.match(command, /exit 42/);
  assert.doesNotMatch(command, /images\.tar\.gz|unzip|docker load/);
  assert.throws(() => selectImageTransport({...proof, transport: 'actions-artifact'}, 'prepared'), /verified GHCR OCI/);
});

test('OCI parity inspection failure cannot masquerade as expected identity rejection', t => {
  const f = fixture(t), proof = verifyReleaseBundle(f.dir, sha);
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-parity-probe-'));
  t.after(() => fs.rmSync(scratch, {recursive: true, force: true}));
  for (const filename of ['gpoint-image-id-', 'gpoint-agata-image-id-', 'gpoint-g-sales-image-id-'])
    fs.writeFileSync(path.join(scratch, filename + sha), `sha256:${'f'.repeat(64)}\n`);
  const bin = path.join(scratch, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'python3'), '#!/bin/sh\nexit 0\n', {mode: 0o700});
  fs.writeFileSync(path.join(bin, 'docker'), '#!/bin/sh\nexit 7\n', {mode: 0o700});

  const result = spawnSync('bash', ['-c', preparedImageParityCommand(proof, sha, scratch)], {
    encoding: 'utf8', env: {...process.env, PATH: `${bin}:${process.env.PATH}`},
  });

  assert.equal(result.status, 43);
  assert.match(result.stdout, /GPOINT_CI_PARITY_INSPECT_FAILED=app/);
  assert.doesNotMatch(result.stdout, /GPOINT_CI_PARITY_IDENTITY_MISMATCH/);
});

test('unsupported and advisory static routes fail closed before any artifact use', t => {
  const f = fixture(t);
  f.receipt.route = 'STATIC_UI_ONLY';
  f.receipt.staticArtifactRoute = 'V4_ADVISORY_ONLY';
  f.receipt.staticArtifact = {repository: 'ghcr.io/akhripunov/gpoint-miniapp-artifacts',
    digest: `sha256:${'f'.repeat(64)}`, contentSha256: 'e'.repeat(64), sourceSha: sha, runId: 123, runAttempt: 2};
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  assert.throws(() => verifyReleaseBundle(f.dir, sha), /not eligible|V4_FAST_STATIC/);
});

test('V4_FAST_STATIC receipt verifies a separate trusted static OCI payload by content SHA', t => {
  const f = fixture(t), baseline = 'f'.repeat(40), staticDigest = `sha256:${'9'.repeat(64)}`;
  const dist = path.join(f.dir, 'dist');
  fs.mkdirSync(path.join(dist, 'assets'), {recursive: true});
  fs.writeFileSync(path.join(dist, 'index.html'), `<meta name="gpoint-build-sha" content="${sha}">`);
  fs.writeFileSync(path.join(dist, 'assets/app.js'), 'static bundle');
  const archive = path.join(f.dir, 'miniapp.zip');
  execFileSync('python3', ['-c', 'from pathlib import Path; from release_ui_artifact_v4 import create_bundle; import sys; create_bundle(Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3]))', dist, sha, archive],
    {cwd: path.dirname(new URL(import.meta.url).pathname)});
  const contentSha = cryptoHash(fs.readFileSync(archive));
  f.receipt.route = 'STATIC_UI_ONLY';
  f.receipt.productionBaselineSha = baseline;
  f.receipt.staticArtifactRoute = 'V4_FAST_STATIC';
  f.receipt.staticArtifactName = 'miniapp.zip';
  f.receipt.staticArtifactSha256 = contentSha;
  f.receipt.staticArtifact = {repository: 'ghcr.io/akhripunov/gpoint-miniapp-artifacts',
    digest: staticDigest, contentSha256: contentSha, sourceSha: sha, runId: 123, runAttempt: 2};
  f.receipt.impactV3 = {version: 1, candidateSha: sha, productionBaselineSha: baseline,
    baselineSource: 'github-deployment-vps-readback', classification: 'UI_ONLY',
    reason: 'narrow-unchanged-backend', changedPaths: ['apps/miniapp/src/ui.tsx'],
    checks: {agata_postgres_rehearsal: false, gsales_postgres_regression: false}};
  f.receipt.impactV4 = {classification: 'COMPONENT_CANDIDATE', component: 'ui', candidateSha: sha,
    productionBaselineSha: baseline, baselineProvenance: 'immutable-COMPLETE', mayDeploySelectively: false,
    maySkipV3Gates: false, changedPaths: ['apps/miniapp/src/ui.tsx']};
  f.receipt.gates = Object.fromEntries(['sourceDiff', 'miniappBuild', 'miniappTests', 'releaseTests',
    'staticArchive', 'migrationUnchanged'].map(key => [key, 'PASS']));
  fs.writeFileSync(path.join(f.dir, 'receipt.json'), JSON.stringify(f.receipt));
  fs.copyFileSync(archive, path.join(f.dir, 'miniapp.zip'));
  fs.writeFileSync(path.join(f.dir, 'ghcr-evidence.json'), JSON.stringify({schema: 1,
    candidateSha: sha, runId: 123, runAttempt: 2, manifestDigest, receiptSha256: 'd'.repeat(64),
    runLogProvenance: 'PASS', staticArtifactDigest: staticDigest, staticArtifactRunLogProvenance: 'PASS'}));
  const proof = verifyReleaseBundle(f.dir, sha);
  assert.equal(proof.staticOnly, true);
  assert.equal(proof.staticArtifactDigest, staticDigest);
});

function cryptoHash(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}
