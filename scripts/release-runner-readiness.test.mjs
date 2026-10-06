import test from 'node:test';
import assert from 'node:assert/strict';
import {validateRuntime, RunnerReadinessError, MIN_FREE_BYTES} from './release-runner-readiness.mjs';

const sha = 'a'.repeat(40);
const ready = overrides => validateRuntime({runnerName: 'gpoint-owner-mac', runnerOs: 'macOS', runnerArch: 'ARM64',
  freeBytes: MIN_FREE_BYTES, dockerAvailable: true, dockerOs: 'linux', mainSha: sha, candidateSha: sha,
  activeConflict: false, pathsReady: true, ...overrides});

test('runner readiness accepts the exact GPoint Mac lane with required runtime dependencies', () => {
  assert.equal(ready({}).docker, 'READY');
});

for (const overrides of [{runnerName: 'other'}, {runnerOs: 'Linux'}, {runnerArch: 'X64'},
  {freeBytes: MIN_FREE_BYTES - 1}, {dockerAvailable: false}, {dockerOs: 'windows'},
  {mainSha: 'b'.repeat(40)}, {activeConflict: true}, {pathsReady: false}]) {
  test(`runner readiness fails closed for ${JSON.stringify(overrides)}`, () => {
    assert.throws(() => ready(overrides), RunnerReadinessError);
  });
}
