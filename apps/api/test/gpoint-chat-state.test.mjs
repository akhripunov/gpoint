import assert from 'node:assert/strict';
import test from 'node:test';
import { buildGpointMessagesState, isGpointChatPermitted } from '../src/gpoint-chat-state.js';
import { gpointAgeAssuranceState } from '../src/gpoint-chat-state.js';

const build = (input) => buildGpointMessagesState({ ...input, erosEnabled: true });

const now = new Date('2026-11-03T12:00:00.000Z');
const policy = {
  id: 'policy-1', sourceTenantId: 'tenant-1', termsVersion: 'gpoint-terms-v1', privacyVersion: 'gpoint-privacy-v1', version: 1, status: 'active',
  effectiveFrom: new Date('2026-01-01T00:00:00.000Z'), effectiveTo: null,
  rules: { minimum_age: 18, ai_disclosure_required: true, self_attestation_allowed: false, adult_only_features: ['romantic', 'sexual'] },
};
const adult = {
  id: 'age-1', ageBand: 'adult', status: 'verified', method: 'document',
  verifiedAt: new Date('2026-10-01T00:00:00.000Z'), assertedAt: null, expiresAt: null, version: 1,
};
const consents = [
  { id: 'consent-chat', purpose: 'chat_history', grantedAt: new Date('2026-10-01T00:00:00.000Z'), revokedAt: null, policyVersion: 'gpoint-consent-v1' },
  { id: 'consent-memory', purpose: 'long_term_memory', grantedAt: new Date('2026-10-01T00:00:00.000Z'), revokedAt: null, policyVersion: 'gpoint-consent-v1' },
  { id: 'consent-terms', purpose: 'terms_of_use', grantedAt: new Date('2026-10-01T00:00:00.000Z'), revokedAt: null, policyVersion: 'gpoint-terms-v1' },
  { id: 'consent-privacy', purpose: 'privacy_policy', grantedAt: new Date('2026-10-01T00:00:00.000Z'), revokedAt: null, policyVersion: 'gpoint-privacy-v1' },
];
const receipt = {
  id: 'receipt-1', policyProfileId: policy.id, policyVersion: policy.version,
  displayedAt: new Date('2026-10-01T00:00:00.000Z'), memoryOptIn: true,
  memoryOptInAt: new Date('2026-10-01T00:00:00.000Z'),
};

test('GPoint message state requires an exact active disclosure and current consent', () => {
  const state = build({ policies: [policy], consents, ages: [adult], receipts: [receipt], now });
  assert.equal(state.enabled, true);
  assert.equal(state.chatHistory, true);
  assert.equal(state.longTermMemory, true);
  assert.equal(state.termsAccepted, true);
  assert.equal(state.privacyAccepted, true);
  assert.equal(state.termsVersion, 'gpoint-terms-v1');
  assert.equal(state.privacyVersion, 'gpoint-privacy-v1');
  assert.equal(state.memoryConsentRequired, true);
  assert.equal(state.disclosureRequired, false);
  assert.equal(state.disclosureVersion, 1);
  assert.equal(state.ageAssuranceRequired, false);
  assert.equal(state.assertedAt, null);
  assert.equal(isGpointChatPermitted(state), true);
  assert.equal(gpointAgeAssuranceState(state).assertedAt, null);
});

test('missing or revoked chat consent fails closed even when age and disclosure are valid', () => {
  const missing = build({ policies: [policy], consents: [], ages: [adult], receipts: [receipt], now });
  const revoked = build({
    policies: [policy],
    consents: [{ ...consents[0], revokedAt: new Date('2026-11-02T00:00:00.000Z') }],
    ages: [adult], receipts: [receipt], now,
  });
  assert.equal(isGpointChatPermitted(missing), false);
  assert.equal(isGpointChatPermitted(revoked), false);
});

test('missing disclosure receipt, minor age, and expired age never authorize chat', () => {
  const missingReceipt = build({ policies: [policy], consents, ages: [adult], receipts: [], now });
  const minor = build({ policies: [policy], consents, ages: [{ ...adult, ageBand: 'minor' }], receipts: [receipt], now });
  const expired = build({ policies: [policy], consents, ages: [{ ...adult, expiresAt: new Date('2026-11-03T11:59:59.000Z') }], receipts: [receipt], now });
  assert.equal(isGpointChatPermitted(missingReceipt), false);
  assert.equal(isGpointChatPermitted(minor), false);
  assert.equal(isGpointChatPermitted(expired), false);
  assert.equal(missingReceipt.disclosureRequired, true);
  assert.equal(minor.ageAssuranceRequired, true);
});

test('self-attestation only passes when the active global policy explicitly allows it', () => {
  const selfAttested = {
    ...adult, status: 'self_attested', method: 'self_attested', verifiedAt: null,
    assertedAt: new Date('2026-11-03T11:00:00.000Z'),
  };
  const denied = build({ policies: [policy], consents, ages: [selfAttested], receipts: [receipt], now });
  const allowed = build({
    policies: [{ ...policy, rules: { ...policy.rules, self_attestation_allowed: true } }],
    consents, ages: [selfAttested], receipts: [receipt], now,
  });
  assert.equal(isGpointChatPermitted(denied), false);
  assert.equal(isGpointChatPermitted(allowed), true);
  assert.equal(gpointAgeAssuranceState(allowed).assertedAt, selfAttested.assertedAt.toISOString());
});

test('revoking memory permission prevents Eros routing until memory consent is explicitly restored', () => {
  const memoryRevoked = build({
    policies: [policy],
    consents: [consents[0], { ...consents[1], revokedAt: now }],
    ages: [adult], receipts: [receipt], now,
  });
  assert.equal(memoryRevoked.chatHistory, true);
  assert.equal(memoryRevoked.longTermMemory, false);
  assert.equal(isGpointChatPermitted(memoryRevoked), false);
});

test('missing or malformed active policy never enables the chat gate', () => {
  const absent = build({ policies: [], consents, ages: [adult], receipts: [receipt], now });
  const malformed = build({ policies: [{ ...policy, rules: null }], consents, ages: [adult], receipts: [receipt], now });
  assert.equal(absent.enabled, false);
  assert.equal(malformed.enabled, false);
  assert.equal(isGpointChatPermitted(absent), false);
  assert.equal(isGpointChatPermitted(malformed), false);
});

test('ambiguous GPoint tenant lineage and non-adult age fail closed', () => {
  const ambiguous = build({
    policies: [policy, { ...policy, id: 'policy-other-tenant', sourceTenantId: 'tenant-2' }],
    consents, ages: [adult], receipts: [receipt], now,
  });
  const mismatchedAge = build({
    policies: [policy], consents,
    ages: [{ ...adult, status: 'self_attested', ageBand: 'minor', verifiedAt: null, assertedAt: now }],
    receipts: [receipt], now,
  });
  assert.equal(ambiguous.enabled, false);
  assert.equal(isGpointChatPermitted(ambiguous), false);
  assert.equal(isGpointChatPermitted(mismatchedAge), false);
});

test('GPoint Terms and Privacy Policy versions require current explicit acceptance', () => {
  const missingTerms = build({ policies: [policy], consents: consents.filter((row) => row.purpose !== 'terms_of_use'), ages: [adult], receipts: [receipt], now });
  const stalePrivacy = build({ policies: [policy], consents: consents.map((row) => row.purpose === 'privacy_policy' ? { ...row, policyVersion: 'gpoint-privacy-old' } : row), ages: [adult], receipts: [receipt], now });
  assert.equal(missingTerms.termsAccepted, false);
  assert.equal(stalePrivacy.privacyAccepted, false);
  assert.equal(isGpointChatPermitted(missingTerms), false);
  assert.equal(isGpointChatPermitted(stalePrivacy), false);
  assert.equal(Object.hasOwn(build({ policies: [policy], consents, ages: [adult], receipts: [receipt], now }), 'jurisdiction'), false);
});

test('duplicate active global policy disagreement fails closed', () => {
  const conflicting = { ...policy, id: 'policy-conflicting-rules',
    rules: { ...policy.rules, self_attestation_allowed: true } };
  const ambiguousPolicy = build({
    policies: [policy, conflicting],
    consents, ages: [adult], receipts: [receipt], now,
  });
  assert.equal(ambiguousPolicy.enabled, false);
  assert.equal(isGpointChatPermitted(ambiguousPolicy), false);
});
