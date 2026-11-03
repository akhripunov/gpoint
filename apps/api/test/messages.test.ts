import assert from 'node:assert/strict';
import test from 'node:test';
import { HttpException } from '@nestjs/common';
import { publicUnreadSummary, validateReadBatch } from '../src/messages.js';

test('public unread total excludes inaccessible creators and includes every eligible dialogue', () => {
  const rows = Array.from({ length: 35 }, (_, i) => ({ gpointCharacterId: `creator-${i}`, unreadCount: 2 }));
  rows.push({ gpointCharacterId: 'hidden', unreadCount: 50 });
  const summary = publicUnreadSummary(rows, new Set(rows.slice(0, 35).map((row) => row.gpointCharacterId)));
  assert.equal(summary.totalUnread, 70);
  assert.equal(summary.items.length, 35);
  assert.equal(summary.items.some((row) => row.creatorId === 'hidden'), false);
});

test('read batch validation rejects malformed, empty, oversized and client-selected identity', () => {
  const id = '2396112c-8425-440b-890a-14ebc49e94c2';
  assert.deepEqual(validateReadBatch({ messageIds: [id, id] }), [id]);
  for (const body of [null, {}, { messageIds: [] }, { messageIds: ['invalid'] }, { messageIds: Array(101).fill(id) }, { messageIds: [id], userId: 'foreign' }]) {
    assert.throws(() => validateReadBatch(body), HttpException);
  }
});

test('read batch accepts bounded stock Eros IDs and rejects path-like values', () => {
  const stockErosId = '01a1630d-4127-0726-2b87-9b2624548592';
  assert.throws(() => validateReadBatch({ messageIds: [stockErosId] }), HttpException);
  assert.deepEqual(validateReadBatch({ messageIds: [stockErosId] }, true), [stockErosId]);
  assert.throws(() => validateReadBatch({ messageIds: ['../../etc/passwd'] }, true), HttpException);
});
