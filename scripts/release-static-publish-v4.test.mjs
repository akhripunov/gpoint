import test from 'node:test';
import assert from 'node:assert/strict';
import { staticPublishFailureDecision } from './release-gpoint.mjs';

test('no runtime mutation and no immutable tag can fail pre-switch safely', () => {
  assert.equal(staticPublishFailureDecision({switchStarted:false,tagPublishAttempted:false}),'FAILED_PRE_SWITCH');
});
test('failed static switch that is demonstrably rolled back is terminal failure', () => {
  assert.equal(staticPublishFailureDecision({switchStarted:true,rollbackProven:true}),'FAILED_ROLLED_BACK');
});
test('once immutable remote tag publish is attempted, never declare rollback terminal success', () => {
  for (const rollbackProven of [true,false]) {
    assert.equal(staticPublishFailureDecision({
      switchStarted:true,rollbackProven,tagPublishAttempted:true}), 'RECOVERY_REQUIRED');
  }
});
test('uncertain site switch without rollback evidence forces recovery', () => {
  assert.equal(staticPublishFailureDecision({switchStarted:true,rollbackProven:false}),'RECOVERY_REQUIRED');
});
