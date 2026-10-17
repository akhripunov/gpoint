import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {recoveryEvidenceFromReadback} from './release-evidence.mjs';

const candidate = 'd'.repeat(40);
const id = '20261017T064101334Z';

function fixture(status) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'recovery-evidence-test-'));
  const reconcile = status === 'RECOVERY_REQUIRED';
  const name = `gpoint-recovery-${reconcile ? 'reconcile' : 'finalization'}-${id}.json`;
  const bytes = Buffer.from(JSON.stringify({eventType: reconcile ? 'RECOVERY_STATE_RECONCILIATION' : 'RECOVERY_FINALIZATION',
    candidateSha: candidate, status}));
  fs.writeFileSync(path.join(directory, name), bytes);
  const state = reconcile ? {recoveryRequired: true,
    recoveryReconciliationManifest: `docs/release/manifests/${name}`,
    recoveryReconciliationManifestSha256: crypto.createHash('sha256').update(bytes).digest('hex')}
    : {recoveryRequired: false, lastReleaseStatus: 'COMPLETE',
      recoveryFinalizationManifest: `docs/release/manifests/${name}`,
      recoveryFinalizationManifestSha256: crypto.createHash('sha256').update(bytes).digest('hex')};
  return {directory, name, state};
}

for (const status of ['RECOVERY_REQUIRED', 'COMPLETE']) {
  test(`read-back ${status} resolves only the manifest named and hash-bound by ledger state`, () => {
    const {directory, state} = fixture(status);
    try {
      assert.equal(recoveryEvidenceFromReadback(state, directory, candidate).status, status);
      assert.throws(() => recoveryEvidenceFromReadback(state, directory, 'f'.repeat(40)), /identity/);
      const referenceKey = status === 'RECOVERY_REQUIRED' ? 'recoveryReconciliationManifest' : 'recoveryFinalizationManifest';
      assert.throws(() => recoveryEvidenceFromReadback({...state,
        [referenceKey]: 'docs/release/manifests/untrusted.json'}, directory, candidate), /reference/);
      assert.throws(() => recoveryEvidenceFromReadback({...state,
        recoveryReconciliationManifestSha256: 'a'.repeat(64),
        recoveryFinalizationManifestSha256: 'a'.repeat(64)}, directory, candidate), /digest/);
    } finally { fs.rmSync(directory, {recursive: true, force: true}); }
  });
}
