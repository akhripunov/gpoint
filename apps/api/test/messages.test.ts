import assert from 'node:assert/strict';
import test from 'node:test';
import { AgataServiceError } from '@gpoint/agata-client';
import { HttpException } from '@nestjs/common';
import { assertMessageAgeAssurance, buildAgataCharacterProfile, mapAgataError } from '../src/messages.js';

test('starter profile is deterministic and contains only approved public creator facts', () => {
  const profile = buildAgataCharacterProfile({ displayName: 'Anna', profile: { bio: 'Public bio' } });
  assert.deepEqual(profile, {
    name: 'Anna',
    personality: { identity_core: { source: 'gpoint_creator' } },
    speech_style: {},
    backstory: { public_bio: 'Public bio' },
    boundaries: {},
  });
  assert.equal(JSON.stringify(profile).includes('creator-id'), false);
  assert.deepEqual(buildAgataCharacterProfile({ displayName: 'Anna' }).backstory, { public_bio: '' });
});

test('AGATA errors map to stable public codes without exposing internal messages', () => {
  const unavailable = mapAgataError(new AgataServiceError('secret internal detail', 0, 'transport_error'));
  assert.equal(unavailable.getStatus(), 503);
  assert.deepEqual(unavailable.getResponse(), { code: 'MESSAGES_TEMPORARILY_UNAVAILABLE' });
  const conflict = mapAgataError(new AgataServiceError('raw detail', 409, 'idempotency_conflict'));
  assert.equal(conflict.getStatus(), 409);
  assert.deepEqual(conflict.getResponse(), { code: 'CHAT_IDEMPOTENCY_CONFLICT' });
  for (const code of ['provider_unavailable', 'inference_retry_exhausted']) {
    const workerUnavailable = mapAgataError(new AgataServiceError('internal worker detail', 503, code));
    assert.equal(workerUnavailable.getStatus(), 503);
    assert.deepEqual(workerUnavailable.getResponse(), { code: 'AI_UNAVAILABLE' });
  }
});

test('message send is blocked with a stable age-assurance error when authoritative state requires it', () => {
  assert.throws(
    () => assertMessageAgeAssurance({ ageAssuranceRequired: true }),
    (error: unknown) => error instanceof HttpException
      && error.getStatus() === 403
      && (error.getResponse() as { code?: string }).code === 'AGE_ASSURANCE_REQUIRED',
  );
  assert.doesNotThrow(() => assertMessageAgeAssurance({ ageAssuranceRequired: false }));
});
