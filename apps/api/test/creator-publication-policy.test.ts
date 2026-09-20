import test from 'node:test';
import assert from 'node:assert/strict';
import { canAutoPublishVerifiedCreatorProfile, verifiedCreatorProfileDecision } from '../src/creator-publication-policy.js';

test('verified channel auto-publication only advances initial profile states', () => {
  assert.equal(canAutoPublishVerifiedCreatorProfile('DRAFT'), true);
  assert.equal(canAutoPublishVerifiedCreatorProfile('PENDING'), true);
  assert.equal(canAutoPublishVerifiedCreatorProfile('APPROVED'), false);
  assert.equal(canAutoPublishVerifiedCreatorProfile('REJECTED'), false);
  assert.equal(canAutoPublishVerifiedCreatorProfile('SUSPENDED'), false);
});

test('auto-publication fails closed without complete channel verification evidence', () => {
  const base = { status: 'PENDING', channelVerified: true, userAdministratorVerified: true, botAdministratorVerified: true };
  assert.deepEqual(verifiedCreatorProfileDecision({ ...base, channelVerified: false }).publish, false);
  assert.deepEqual(verifiedCreatorProfileDecision({ ...base, userAdministratorVerified: false }).publish, false);
  assert.deepEqual(verifiedCreatorProfileDecision({ ...base, botAdministratorVerified: false }).publish, false);
  assert.deepEqual(verifiedCreatorProfileDecision({ ...base }).publish, true);
});

test('administrative enforcement states remain non-public', () => {
  const verified = { channelVerified: true, userAdministratorVerified: true, botAdministratorVerified: true };
  assert.equal(verifiedCreatorProfileDecision({ ...verified, status: 'REJECTED' }).publish, false);
  assert.equal(verifiedCreatorProfileDecision({ ...verified, status: 'SUSPENDED' }).publish, false);
});
