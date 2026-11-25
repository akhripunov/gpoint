import assert from 'node:assert/strict';
import test from 'node:test';
import { HttpException } from '@nestjs/common';
import { ErosAdapterError } from '../src/eros-adapter.js';
import { assertMessageAgeAssurance, mapErosError, MessagesController } from '../src/messages.js';
import { createSession } from '../src/session.js';

test('Eros failures map to stable public codes without exposing internal messages', () => {
  const unavailable = mapErosError(new Error('secret internal detail'));
  assert.equal(unavailable.getStatus(), 503);
  assert.deepEqual(unavailable.getResponse(), { code: 'EROS_UNAVAILABLE' });
  const conflict = mapErosError(new ErosAdapterError('eros_relationship_mismatch', 409));
  assert.equal(conflict.getStatus(), 409);
  assert.deepEqual(conflict.getResponse(), { code: 'EROS_UNAVAILABLE', reason: 'eros_relationship_mismatch' });
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

test('Eros read batch accepts bounded stock message IDs', async () => {
  const { validateReadBatch }=await import('../src/messages.js');
  const stockErosId='01a1630d-4127-0726-2b87-9b2624548592';
  assert.throws(()=>validateReadBatch({messageIds:[stockErosId]}),HttpException);
  assert.deepEqual(validateReadBatch({messageIds:[stockErosId]}, true),[stockErosId]);
  assert.throws(()=>validateReadBatch({messageIds:['../../etc/passwd']}, true),HttpException);
});

test('unread and acknowledgement require a GPoint session before touching upstream state', async () => {
  const controller=new MessagesController();
  await assert.rejects(controller.unread({}), (e:any)=>e.getStatus()===401);
  await assert.rejects(controller.read('creator',{}, {messageIds:[]}), (e:any)=>e.getStatus()===401);
});

test('Messages state uses GPoint state and never calls a configured legacy AGATA service', async () => {
  const previousFetch = globalThis.fetch;
  const env = {
    erosEnabled: process.env.EROS_ENABLED,
    agataEnabled: process.env.AGATA_ENABLED,
    agataUrl: process.env.AGATA_SERVICE_URL,
    agataToken: process.env.AGATA_INTERNAL_API_TOKEN,
  };
  process.env.EROS_ENABLED = 'false';
  process.env.AGATA_ENABLED = 'true';
  process.env.AGATA_SERVICE_URL = 'https://agata.invalid';
  process.env.AGATA_INTERNAL_API_TOKEN = 'disposable-test-token';
  let legacyRequests = 0;
  globalThis.fetch = async () => {
    legacyRequests += 1;
    throw new Error('legacy AGATA service must not be called');
  };

  try {
    const userId = 'authenticated-gpoint-user';
    const state = await new MessagesController().state({ authorization: `Bearer ${createSession(userId)}` });
    assert.equal(state.enabled, false);
    assert.equal(state.aiAvailable, false);
    assert.equal(state.ageAssuranceRequired, true);
    assert.equal(legacyRequests, 0);
  } finally {
    globalThis.fetch = previousFetch;
    for (const [key, value] of Object.entries({
      EROS_ENABLED: env.erosEnabled,
      AGATA_ENABLED: env.agataEnabled,
      AGATA_SERVICE_URL: env.agataUrl,
      AGATA_INTERNAL_API_TOKEN: env.agataToken,
    })) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});
