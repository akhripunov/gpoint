import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/** Add canonical preflight and no-mutation facts to release-result evidence. */
export function withPreflightEvidence(receipt, status, mode) {
  const preflight = status === 'PLANNED' || status === 'PASS' ? 'PASS' : status === 'FAILED' ? 'FAILED' : 'BLOCKED';
  return {
    ...receipt,
    preflight,
    ...(mode === 'preflight' ? { productionChanged: false } : {}),
  };
}

/** Resolve only the recovery manifest named and hash-bound by read-back ledger state. */
export function recoveryEvidenceFromReadback(state, manifestDir, candidateSha) {
  const recovering = state?.recoveryRequired === true;
  const expectedStatus = recovering ? 'RECOVERY_REQUIRED' : 'COMPLETE';
  const reference = recovering ? state?.recoveryReconciliationManifest : state?.recoveryFinalizationManifest;
  const digestField = recovering ? state?.recoveryReconciliationManifestSha256 : state?.recoveryFinalizationManifestSha256;
  const prefix = recovering ? 'reconcile' : 'finalization';
  const match = new RegExp(`^docs/release/manifests/(gpoint-recovery-${prefix}-[0-9]{8}T[0-9]{9}Z\\.json)$`).exec(reference ?? '');
  if (!match || !/^[a-f0-9]{40}$/.test(candidateSha ?? '') || !/^[a-f0-9]{64}$/.test(digestField ?? ''))
    throw new Error('Recovery ledger does not reference a valid incident manifest');
  const bytes = fs.readFileSync(path.join(manifestDir, match[1]));
  if (crypto.createHash('sha256').update(bytes).digest('hex') !== digestField)
    throw new Error('Recovery manifest digest differs from read-back ledger state');
  const manifest = JSON.parse(bytes.toString('utf8'));
  if (manifest.candidateSha !== candidateSha || manifest.status !== expectedStatus
      || manifest.eventType !== (recovering ? 'RECOVERY_STATE_RECONCILIATION' : 'RECOVERY_FINALIZATION'))
    throw new Error('Recovery manifest identity differs from read-back ledger state');
  return manifest;
}
