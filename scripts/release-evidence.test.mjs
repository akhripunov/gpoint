import test from 'node:test';
import assert from 'node:assert/strict';
import { withPreflightEvidence } from './release-evidence.mjs';

test('successful canonical dry-run is persisted as preflight PASS evidence', () => {
  const receipt = withPreflightEvidence({ status: 'PLANNED', releaseState: 'READY_FOR_OWNER' }, 'PLANNED', 'preflight');
  assert.deepEqual(receipt, {
    status: 'PLANNED', releaseState: 'READY_FOR_OWNER', preflight: 'PASS', productionChanged: false,
  });
});

test('failed canonical dry-run is not persisted as successful preflight evidence', () => {
  const receipt = withPreflightEvidence({ status: 'FAILED_PRE_SWITCH', releaseState: 'FAILED_PRE_SWITCH' }, 'FAILED', 'preflight');
  assert.equal(receipt.preflight, 'FAILED');
  assert.notEqual(receipt.preflight, 'PASS');
  assert.equal(receipt.productionChanged, false);
});

test('preflight that did not run remains blocked in evidence', () => {
  assert.equal(withPreflightEvidence({}, 'BLOCKED', 'preflight').preflight, 'BLOCKED');
  assert.equal(withPreflightEvidence({}, 'unexpected', 'preflight').preflight, 'BLOCKED');
});

test('production receipts do not claim that production was unchanged after apply', () => {
  assert.equal(Object.hasOwn(withPreflightEvidence({}, 'PLANNED', 'production'), 'productionChanged'), false);
});
