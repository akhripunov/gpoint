import assert from 'node:assert/strict';
import test from 'node:test';
import { buildGpointMessagesState, gpointAgeAssuranceState, isGpointChatPermitted } from '../src/gpoint-chat-state.js';

const now = new Date('2026-10-28T10:00:00.000Z');
const policy = { id: 'policy-1', sourceTenantId: 'tenant-1', jurisdiction: 'GLOBAL', market: 'GLOBAL', version: 4,
  status: 'active', effectiveFrom: new Date('2026-01-01T00:00:00.000Z'), effectiveTo: null,
  rules: { ai_disclosure_required: true, self_attestation_allowed: true } };

function state(ages: unknown[]) {
  return buildGpointMessagesState({ policies: [policy], policyScope: { jurisdiction: 'GLOBAL', market: 'GLOBAL' }, consents: [], ages: ages as never[], receipts: [], now, erosEnabled: true });
}

test('REG-085 — GPoint age assurance accepts only explicit, current, jurisdiction-matched evidence', () => {
  const required = state([]);
  assert.equal(required.ageAssuranceRequired, true);
  assert.equal(isGpointChatPermitted(required), false);
  assert.deepEqual(gpointAgeAssuranceState(required, true), {
    required: true, eligible: false, status: 'pending', method: 'unknown', jurisdiction: 'GLOBAL',
    assertedAt: null, expiresAt: null, selfAttestationAllowed: true,
  });
  const accepted = state([{
    id: 'age-1', version: 1, ageBand: 'adult', status: 'self_attested', method: 'self_attested',
    jurisdiction: 'GLOBAL', verifiedAt: null, assertedAt: now, expiresAt: null,
  }]);
  assert.equal(accepted.ageAssuranceRequired, false);
  assert.equal(gpointAgeAssuranceState(accepted, true).assertedAt, now.toISOString());
  assert.equal(gpointAgeAssuranceState(accepted, false).selfAttestationAllowed, false);
});

test('REG-085 — absent, minor, expired or cross-jurisdiction age evidence remains blocked', () => {
  const denied = [
    { id: 'minor', version: 1, ageBand: 'minor', status: 'verified', method: 'document', jurisdiction: 'GLOBAL', verifiedAt: now, assertedAt: null, expiresAt: null },
    { id: 'wrong-jurisdiction', version: 2, ageBand: 'adult', status: 'verified', method: 'document', jurisdiction: 'US', verifiedAt: now, assertedAt: null, expiresAt: null },
    { id: 'future', version: 3, ageBand: 'adult', status: 'verified', method: 'document', jurisdiction: 'GLOBAL', verifiedAt: new Date('2027-01-01T00:00:00Z'), assertedAt: null, expiresAt: null },
  ];
  for (const record of denied) assert.equal(state([record]).ageAssuranceRequired, true);
});
