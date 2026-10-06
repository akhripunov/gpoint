import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import {checkRunnerReadiness, GIB} from './release-runner-readiness.mjs';

function environment(home, patch = {}) {
  return {RUNNER_NAME: 'gpoint-owner-mac', RUNNER_OS: 'macOS', RUNNER_ARCH: 'ARM64', HOME: home, ...patch};
}

test('dedicated GPoint runner passes Docker, free disk and persistent cache checks', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-readiness-'));
  try {
    const result = checkRunnerReadiness({env: environment(home), freeBytes: 15 * GIB,
      command: () => ({status: 0, stdout: '29.1.3'})});
    assert.equal(result.RUNNER_READY, 'YES');
    assert.equal(result.DOCKER_READY, 'YES');
    assert.equal(result.DISK_READY, 'YES');
    assert.ok(fs.statSync(result.CACHE_PATH).isDirectory());
  } finally { fs.rmSync(home, {recursive: true, force: true}); }
});

test('wrong runner identity, unavailable Docker or low disk fail closed', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-readiness-'));
  try {
    assert.throws(() => checkRunnerReadiness({env: environment(home, {RUNNER_NAME: 'other'}),
      freeBytes: 15 * GIB, command: () => ({status: 0, stdout: '29'})}), /runner_name_mismatch/);
    assert.throws(() => checkRunnerReadiness({env: environment(home), freeBytes: 15 * GIB,
      command: () => ({status: 1, stdout: ''})}), /docker_unavailable/);
    assert.throws(() => checkRunnerReadiness({env: environment(home), freeBytes: 9 * GIB,
      command: () => ({status: 0, stdout: '29'})}), /disk_below_hard_minimum/);
  } finally { fs.rmSync(home, {recursive: true, force: true}); }
});
