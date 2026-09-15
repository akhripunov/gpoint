import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import { PrismaClient } from '@prisma/client';
import { bootstrap } from '../src/main.js';
import { createSession } from '../src/session.js';

const prisma = new PrismaClient();
const port = 3012;
const base = `http://127.0.0.1:${port}`;
let app: Awaited<ReturnType<typeof bootstrap>>;
let creatorToken = '';
let otherToken = '';
let creatorId = '';
let otherId = '';

async function call(path: string, token: string, method = 'GET', body?: unknown) {
  const headers: Record<string, string> = { authorization: `Bearer ${token}` }; if (body !== undefined) headers['content-type'] = 'application/json';
  const response = await fetch(`${base}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data: any = null; try { data = await response.json(); } catch { /* empty */ }
  return { response, data };
}

before(async () => {
  process.env.API_PORT = String(port);
  app = await bootstrap();
  const stamp = BigInt(Date.now());
  const a = await prisma.user.create({ data: { telegramUserId: stamp + 101n, firstName: 'E2E Creator A' } });
  const b = await prisma.user.create({ data: { telegramUserId: stamp + 102n, firstName: 'E2E Creator B' } });
  creatorToken = createSession(a.id); otherToken = createSession(b.id);
  const ca = await call('/creator/onboarding', creatorToken, 'POST', { displayName: 'E2E Creator A', slug: `e2e-a-${stamp}`, bio: 'integration' });
  const cb = await call('/creator/onboarding', otherToken, 'POST', { displayName: 'E2E Creator B', slug: `e2e-b-${stamp}`, bio: 'integration' });
  assert.equal(ca.response.status, 201); assert.equal(cb.response.status, 201);
  creatorId = ca.data.creator.id; otherId = cb.data.creator.id;
});

after(async () => { await app?.close(); await prisma.$disconnect(); });

test('creator onboarding persists ownership and foreign media is rejected', async () => {
  const me = await call('/creator/me', creatorToken);
  assert.equal(me.response.status, 200); assert.equal(me.data.creator.status, 'DRAFT');
  const intent = await call('/creator/me/media/intent', otherToken, 'POST', { kind: 'IMAGE', mimeType: 'image/jpeg', fileSize: 4 });
  assert.equal(intent.response.status, 201); assert.match(intent.data.uploadUrl, /^https?:/);
  const put = await fetch(intent.data.uploadUrl, { method: 'PUT', headers: { 'content-type': 'image/jpeg' }, body: new Uint8Array([255, 216, 255, 217]) });
  assert.equal(put.status, 200);
  const ready = await call(`/creator/me/media/${intent.data.mediaId}/finalize`, otherToken, 'POST');
  assert.equal(ready.response.status, 201, JSON.stringify(ready.data)); assert.equal(ready.data.status, 'READY');
  const avatar = await call('/creator/me/profile', creatorToken, 'PATCH', { avatarMediaId: intent.data.mediaId });
  const post = await call('/creator/me/posts', creatorToken, 'POST', { type: 'IMAGE', caption: 'foreign', mediaIds: [intent.data.mediaId] });
  const reel = await call('/creator/me/reels', creatorToken, 'POST', { caption: 'foreign', mediaIds: [intent.data.mediaId] });
  const foreignDelete = await call(`/creator/me/media/${intent.data.mediaId}`, creatorToken, 'DELETE');
  assert.equal(avatar.response.status, 400); assert.equal(post.response.status, 400); assert.equal(reel.response.status, 400); assert.equal(foreignDelete.response.status, 404);
  const persisted = await prisma.mediaAsset.findUnique({ where: { id: intent.data.mediaId } });
  assert.equal(persisted?.creatorId, otherId); assert.equal(persisted?.status, 'READY'); assert.notEqual(creatorId, otherId);
});

test('unsupported and oversized media are rejected by the real API', async () => {
  const badMime = await call('/creator/me/media/intent', creatorToken, 'POST', { kind: 'IMAGE', mimeType: 'application/pdf', fileSize: 4 });
  const hugeImage = await call('/creator/me/media/intent', creatorToken, 'POST', { kind: 'IMAGE', mimeType: 'image/png', fileSize: 10 * 1024 * 1024 + 1 });
  const hugeVideo = await call('/creator/me/media/intent', creatorToken, 'POST', { kind: 'VIDEO', mimeType: 'video/mp4', fileSize: 50 * 1024 * 1024 + 1 });
  assert.equal(badMime.response.status, 400); assert.equal(hugeImage.response.status, 400); assert.equal(hugeVideo.response.status, 400);
});
