import test from 'node:test';
import assert from 'node:assert/strict';
import {withPreflightEvidence} from './release-evidence.mjs';

test('preflight status is captured without claiming production mutation', () => {
  const result = withPreflightEvidence({status: 'PLANNED'}, 'PLANNED', 'preflight');
  assert.equal(result.preflight, 'PASS');
  assert.equal(result.productionChanged, false);
});

test('production evidence preserves a blocked preflight state', () => {
  assert.equal(withPreflightEvidence({status: 'BLOCKED'}, 'BLOCKED', 'production').preflight, 'BLOCKED');
});
