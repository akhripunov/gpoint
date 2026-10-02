import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { persistReleaseBookkeeping, loadReleaseBookkeeping, LEDGER_REF } from './release-bookkeeping.mjs';

function fixture(t) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-ledger-test-'));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const root = path.join(base, 'checkout'), remote = path.join(base, 'remote.git');
  fs.mkdirSync(root);
  const git = (args, input) => execFileSync('git', args, { cwd: root, input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
  git(['init', '--bare', remote]); git(['init', '-b', 'main']);
  git(['config', 'user.name', 'Test']); git(['config', 'user.email', 'test@example.invalid']);
  fs.mkdirSync(path.join(root, 'docs/release/state'), { recursive: true });
  fs.mkdirSync(path.join(root, 'docs/release/manifests'), { recursive: true });
  fs.writeFileSync(path.join(root, 'docs/release/state/production.json'), JSON.stringify({ currentProductionSha: 'a'.repeat(40), recoveryRequired: false }));
  fs.writeFileSync(path.join(root, 'app.txt'), 'application candidate');
  git(['add', '.']); git(['commit', '-m', 'candidate']);
  git(['remote', 'add', 'origin', remote]); git(['push', 'origin', 'main']);
  return { base, root, git, candidateSha: git(['rev-parse', 'HEAD']) };
}

for (const status of ['COMPLETE', 'RECOVERY_REQUIRED', 'UNKNOWN']) {
  test(`${status} persists external state/history without changing source main or checkout`, t => {
    const f = fixture(t);
    let ledger = loadReleaseBookkeeping({ ...f, allowMissing: true, destination: path.join(f.base, 'first') });
    assert.equal(ledger.ledgerSha, null);
    const name = 'gpoint-release-first.json';
    fs.writeFileSync(path.join(ledger.manifestDir, name), JSON.stringify({ status, candidateSha: f.candidateSha }));
    fs.writeFileSync(ledger.stateFile, JSON.stringify({ currentProductionSha: f.candidateSha, recoveryRequired: status !== 'COMPLETE', lastReleaseStatus: status }));
    assert.equal(persistReleaseBookkeeping({ mode: 'production', records: [{ name }], evidence: { status, candidateSha: f.candidateSha }, ...f, ...ledger }), 'PERSISTED_TO_LEDGER');
    assert.equal(f.git(['rev-parse', 'HEAD']), f.candidateSha);
    assert.equal(f.git(['ls-remote', 'origin', 'refs/heads/main']).split(/\s/)[0], f.candidateSha);
    assert.equal(f.git(['status', '--porcelain']), '');
    ledger = loadReleaseBookkeeping({ ...f, allowMissing: true, destination: path.join(f.base, 'second') });
    assert.equal(JSON.parse(fs.readFileSync(ledger.stateFile)).lastReleaseStatus, status);
    assert.equal(JSON.parse(fs.readFileSync(path.join(ledger.manifestDir, name))).status, status);
    const second = 'gpoint-release-second.json';
    fs.writeFileSync(path.join(ledger.manifestDir, second), '{}');
    persistReleaseBookkeeping({ mode: 'production', records: [{ name: second }], evidence: { status, candidateSha: f.candidateSha }, ...f, ...ledger });
    const head = f.git(['ls-remote', 'origin', LEDGER_REF]).split(/\s/)[0];
    assert.equal(f.git(['show', `${head}:docs/release/manifests/${name}`]), JSON.stringify({ status, candidateSha: f.candidateSha }));
    assert.equal(f.git(['status', '--porcelain']), '');
  });
}

test('preflight and safe failures never push; transport/persistence errors stay visible', () => {
  for (const status of ['BLOCKED', 'FAILED_PRE_SWITCH']) {
    assert.equal(persistReleaseBookkeeping({ mode: 'production', records: [{ name: 'gpoint-test.json' }], evidence: { status }, git: () => assert.fail('no Git mutation') }), 'ARTIFACT_AND_LINEAR_ONLY');
  }
  assert.equal(persistReleaseBookkeeping({ mode: 'preflight', records: [{}], evidence: {}, git: () => assert.fail('no Git mutation') }), 'NOT_REQUIRED');
  assert.throws(() => loadReleaseBookkeeping({ root: '.', git: () => { throw Error('network unavailable'); }, destination: '/nonexistent-denied-test' }));
});

test('a conflicting writer cannot overwrite remote ledger', t => {
  const f = fixture(t), ledger = loadReleaseBookkeeping({ ...f, allowMissing: true, destination: path.join(f.base, 'evidence') });
  const name = 'gpoint-release-test.json'; fs.writeFileSync(path.join(ledger.manifestDir, name), '{}');
  let conflict = false;
  const git = (args, input) => {
    if (args[0] === 'push') { conflict = true; throw Error('non-fast-forward'); }
    return f.git(args, input);
  };
  assert.throws(() => persistReleaseBookkeeping({ mode: 'production', records: [{ name }], evidence: { status: 'COMPLETE', candidateSha: f.candidateSha }, ...ledger, git }), /non-fast-forward/);
  assert.equal(conflict, true);
  assert.equal(f.git(['ls-remote', 'origin', 'refs/heads/main']).split(/\s/)[0], f.candidateSha);
});
