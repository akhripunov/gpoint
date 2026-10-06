import test from 'node:test';
import assert from 'node:assert/strict';
import {validateRuntime, probeDeploymentNetwork, configuredDeploymentRunner, RunnerReadinessError, MIN_FREE_BYTES} from './release-runner-readiness.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const sha = 'a'.repeat(40);
const ready = overrides => validateRuntime({runnerName: 'gpoint-owner-mac', runnerOs: 'macOS', runnerArch: 'ARM64',
  freeBytes: MIN_FREE_BYTES, dockerAvailable: true, dockerOs: 'linux', mainSha: sha, candidateSha: sha,
  activeConflict: false, pathsReady: true, workerRuntimeReady: true, toolchainAvailable: true,
  networkReady: true, ...overrides});

test('runner readiness accepts the exact GPoint Mac lane with required runtime dependencies', () => {
  assert.equal(ready({}).docker, 'READY');
  assert.equal(ready({}).network, 'READY');
});

test('deployment network probe uses the configured absolute remote runner with a bounded read-only command', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-readiness-'));
  try {
    const runner = path.join(root, 'remote-runner');
    fs.writeFileSync(runner, '#!/bin/sh\nprintf GPOINT_DEPLOYMENT_NETWORK_READY\n', {mode: 0o700});
    const result = probeDeploymentNetwork({runner, env: {PATH: process.env.PATH}});
    assert.equal(result, true);
    assert.equal(probeDeploymentNetwork({runner: 'relative-runner'}), false);
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test('readiness resolves the deployment runner from canonical config without requiring a separate credential', () => {
  const runner = '/Users/test/.local/bin/gpoint-vps-runner';
  assert.equal(configuredDeploymentRunner({GPOINT_REMOTE_RUNNER: runner}), runner);
});

for (const overrides of [{runnerName: 'other'}, {runnerOs: 'Linux'}, {runnerArch: 'X64'},
  {freeBytes: MIN_FREE_BYTES - 1}, {dockerAvailable: false}, {dockerOs: 'windows'},
  {mainSha: 'b'.repeat(40)}, {activeConflict: true}, {pathsReady: false},
  {workerRuntimeReady: false}, {toolchainAvailable: false}, {networkReady: false}]) {
  test(`runner readiness fails closed for ${JSON.stringify(overrides)}`, () => {
    assert.throws(() => ready(overrides), RunnerReadinessError);
  });
}
