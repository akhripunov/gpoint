import test from 'node:test';
import assert from 'node:assert/strict';
import { assertInstalledWorkerMatches } from './worker-runtime-guard.mjs';

const mismatch = [{ sourceExists: true, installedExists: true, matches: false }];
const match = [{ sourceExists: true, installedExists: true, matches: true }];

test('read-only preflight does not require installing candidate Mac worker assets', () => {
  assert.doesNotThrow(() => assertInstalledWorkerMatches('preflight', mismatch));
});

test('mutating release modes still require exact installed worker assets', () => {
  for (const mode of ['production', 'task-scoped-auto']) {
    assert.throws(() => assertInstalledWorkerMatches(mode, mismatch), /Installed Mac worker differs from candidate/);
    assert.doesNotThrow(() => assertInstalledWorkerMatches(mode, match));
  }
});
