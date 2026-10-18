import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {validateGhcrReceipt, verifyReceiptBytes, receiptTag} from './ghcr-receipt.mjs';

const sha = 'a'.repeat(40);
const runId = '12345';
const attempt = '2';
const bytesFor = value => Buffer.from(JSON.stringify(value) + '\n');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const ready = {
  schema: 3, transport: 'ghcr-oci', status: 'READY', candidateSha: sha,
  runId: Number(runId), runAttempt: Number(attempt),
  gates: {unit: 'PASS', docker: 'PASS'},
};
test('GHCR receipt binds source, exact Prepare run and attempt', () => {
  const validated = validateGhcrReceipt(ready, {candidateSha: sha, runId, runAttempt: attempt,
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'c'.repeat(64)});
  assert.equal(validated.receiptDigest, `sha256:${'b'.repeat(64)}`);
  assert.equal(receiptTag(sha, runId, attempt), `prepare-${sha}-${runId}-${attempt}`);
});

test('receipt is rejected when run, source, gate, digest or bytes differ', () => {
  assert.throws(() => validateGhcrReceipt(ready, {candidateSha: sha, runId: '9', runAttempt: attempt,
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'c'.repeat(64)}));
  assert.throws(() => validateGhcrReceipt(ready, {candidateSha: sha, runId, runAttempt: '1',
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'c'.repeat(64)}));
  assert.throws(() => validateGhcrReceipt(ready, {candidateSha: 'd'.repeat(40), runId, runAttempt: attempt,
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'c'.repeat(64)}));
  assert.throws(() => validateGhcrReceipt(ready, {candidateSha: sha, runId, runAttempt: attempt,
    receiptDigest: 'latest', receiptSha256: 'c'.repeat(64)}));
  assert.throws(() => validateGhcrReceipt(ready, {candidateSha: sha, runId, runAttempt: attempt,
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'bad'}));
  assert.throws(() => validateGhcrReceipt({...ready, gates: {unit: 'FAIL'}}, {candidateSha: sha, runId, runAttempt: attempt,
    receiptDigest: `sha256:${'b'.repeat(64)}`, receiptSha256: 'c'.repeat(64)}));
  const bytes = bytesFor(ready);
  assert.deepEqual(verifyReceiptBytes(bytes, digest(bytes)), ready);
  assert.throws(() => verifyReceiptBytes(bytes, 'f'.repeat(64)));
});
