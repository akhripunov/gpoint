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

test('public unread total excludes inaccessible creators and includes every eligible dialogue', async () => {
  const { publicUnreadSummary }=await import('../src/messages.js');
  const rows=Array.from({length:35},(_,i)=>({gpointCharacterId:`creator-${i}`,unreadCount:2}));
  rows.push({gpointCharacterId:'hidden',unreadCount:50});
  const summary=publicUnreadSummary(rows,new Set(rows.slice(0,35).map(r=>r.gpointCharacterId)));
  assert.equal(summary.totalUnread,70);
  assert.equal(summary.items.length,35);
  assert.equal(summary.items.some(r=>r.creatorId==='hidden'),false);
});

test('read batch validation rejects malformed, empty, oversized and client-selected identity', async () => {
  const { validateReadBatch }=await import('../src/messages.js');
  const id='2396112c-8425-440b-890a-14ebc49e94c2';
  assert.deepEqual(validateReadBatch({messageIds:[id,id]}),[id]);
  for(const body of [null,{}, {messageIds:[]},{messageIds:['invalid']},{messageIds:Array(101).fill(id)},{messageIds:[id],userId:'foreign'}]) assert.throws(()=>validateReadBatch(body),HttpException);
});

test('unread and acknowledgement require a GPoint session before touching upstream state', async () => {
  const { MessagesController }=await import('../src/messages.js');
  const controller=new MessagesController();
  await assert.rejects(controller.unread({}), (e:any)=>e.getStatus()===401);
  await assert.rejects(controller.read('creator',{}, {messageIds:[]}), (e:any)=>e.getStatus()===401);
});
