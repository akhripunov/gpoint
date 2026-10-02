import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { persistReleaseBookkeeping } from './release-bookkeeping.mjs';

test('two safe failed attempts preserve remote main and the prepared SHA; recovery persists', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-bookkeeping-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const remote = path.join(root, 'remote.git'), checkout = path.join(root, 'checkout');
  fs.mkdirSync(checkout);
  const git = args => execFileSync('git', args, { cwd: checkout, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  git(['init', '--bare', remote]);
  git(['init', '-b', 'main']);
  git(['config', 'user.name', 'Test']); git(['config', 'user.email', 'test@example.invalid']);
  fs.mkdirSync(path.join(checkout, 'docs/release/state'), { recursive: true });
  fs.mkdirSync(path.join(checkout, 'docs/release/manifests'), { recursive: true });
  const state = path.join(checkout, 'docs/release/state/production.json');
  fs.writeFileSync(state, '{"recoveryRequired":false}\n');
  fs.writeFileSync(path.join(checkout, 'application.txt'), 'immutable candidate');
  git(['add', '.']); git(['commit', '-m', 'application candidate']);
  git(['remote', 'add', 'origin', remote]); git(['push', 'origin', 'main']);
  const candidateSha = git(['rev-parse', 'HEAD']);
  for (let attempt = 1; attempt <= 2; attempt++) {
    const name = `gpoint-release-attempt-${attempt}.json`;
    const evidence = { candidateSha, status: 'FAILED_PRE_SWITCH', attempt };
    fs.writeFileSync(path.join(checkout, 'docs/release/manifests', name), JSON.stringify(evidence));
    fs.writeFileSync(state, JSON.stringify({ recoveryRequired: false, lastReleaseStatus: evidence.status }));
    assert.equal(persistReleaseBookkeeping({ mode: 'production', records: [{ name }], evidence, git }), 'ARTIFACT_AND_LINEAR_ONLY');
    assert.equal(git(['rev-parse', 'HEAD']), candidateSha);
    assert.equal(git(['ls-remote', 'origin', 'refs/heads/main']).split(/\s/)[0], candidateSha);
    assert.equal(JSON.parse(fs.readFileSync(path.join(checkout, 'docs/release/manifests', name))).attempt, attempt);
  }
  // A partial mutation must retain durable recovery evidence and never take the safe fast path.
  const name = 'gpoint-release-recovery.json';
  fs.writeFileSync(state, '{"recoveryRequired":true}\n');
  fs.writeFileSync(path.join(checkout, 'docs/release/manifests', name), '{}');
  assert.equal(persistReleaseBookkeeping({ mode: 'production', records: [{ name }], evidence: { candidateSha, status: 'RECOVERY_REQUIRED' }, git }), 'PERSISTED_TO_MAIN');
  assert.notEqual(git(['rev-parse', 'HEAD']), candidateSha);
  assert.equal(JSON.parse(git(['show', 'origin/main:docs/release/state/production.json'])).recoveryRequired, true);
  assert.equal(git(['show', 'HEAD:application.txt']), 'immutable candidate');
});

test('preflight never pushes; unknown outcomes fail closed and persistence errors remain visible', () => {
  const base = { records: [{ name: 'gpoint-release-test.json' }], evidence: { candidateSha: 'a'.repeat(40), status: 'UNKNOWN' } };
  assert.equal(persistReleaseBookkeeping({ ...base, mode: 'preflight', git: () => { throw new Error('must not run'); } }), 'NOT_REQUIRED');
  assert.throws(() => persistReleaseBookkeeping({ ...base, mode: 'production', git: () => { throw new Error('push blocked'); } }), /push blocked/);
});
