import crypto from 'node:crypto';

const SHA40 = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const OCI_DIGEST = /^sha256:[a-f0-9]{64}$/;
export const RECEIPT_REPOSITORY = 'ghcr.io/akhripunov/gpoint-prepare-receipts';
export const RECEIPT_ARTIFACT_TYPE = 'application/vnd.gpoint.prepare.receipt.v1';

function fail(message) { throw new Error(message); }

export function validateGhcrReceipt(receipt, {candidateSha, runId, runAttempt, receiptDigest, receiptSha256}) {
  if (!SHA40.test(candidateSha ?? '') || receipt?.schema !== 3 || receipt?.transport !== 'ghcr-oci' ||
      receipt?.status !== 'READY' || receipt?.candidateSha !== candidateSha ||
      String(receipt.runId) !== String(runId) || String(receipt.runAttempt) !== String(runAttempt)) {
    fail('GHCR receipt source or Prepare identity mismatch');
  }
  if (!OCI_DIGEST.test(receiptDigest ?? '') || !SHA256.test(receiptSha256 ?? '')) {
    fail('GHCR receipt digest identity is incomplete');
  }
  if (!receipt.gates || Object.values(receipt.gates).some(value => !['PASS', 'COVERED_UNCHANGED'].includes(value))) {
    fail('GHCR receipt has a failed or unknown gate');
  }
  return {...receipt, receiptDigest, receiptSha256};
}

export function verifyReceiptBytes(bytes, expectedSha256) {
  if (!Buffer.isBuffer(bytes) || !SHA256.test(expectedSha256 ?? '') ||
      crypto.createHash('sha256').update(bytes).digest('hex') !== expectedSha256) {
    fail('GHCR receipt content checksum mismatch');
  }
  let receipt;
  try { receipt = JSON.parse(bytes.toString('utf8')); }
  catch { fail('GHCR receipt is not valid JSON'); }
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) fail('GHCR receipt must be a JSON object');
  return receipt;
}

export function receiptTag(candidateSha, runId, runAttempt) {
  if (!SHA40.test(candidateSha ?? '') || !/^\d+$/.test(String(runId)) || !/^\d+$/.test(String(runAttempt))) {
    fail('Invalid Prepare receipt locator');
  }
  return `prepare-${candidateSha}-${runId}-${runAttempt}`;
}
