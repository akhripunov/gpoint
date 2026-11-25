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
  process.env.DEV_BROWSER_AUTH = 'true';
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

test('local browser dev auth issues a normal session only for loopback origin', async () => {
  const response = await fetch(`${base}/auth/browser`, { method: 'POST', headers: { origin: 'http://localhost:5184' } });
  const data = await response.json() as { accessToken?: string; user?: { telegramUsername?: string } };
  assert.equal(response.status, 201); assert.ok(data.accessToken); assert.equal(data.user?.telegramUsername, 'dev-browser-user');
  const me = await call('/me', data.accessToken!);
  assert.equal(me.response.status, 200); assert.equal(me.data.telegramUsername, 'dev-browser-user');
  const external = await fetch(`${base}/auth/browser`, { method: 'POST', headers: { origin: 'https://example.com' } });
  assert.equal(external.status, 401);
});

after(async () => { await app?.close(); await prisma.$disconnect(); });

test('creator onboarding persists ownership and foreign media is rejected', async () => {
  const me = await call('/creator/me', creatorToken);
  assert.equal(me.response.status, 200); assert.equal(me.data.creator.status, 'DRAFT');
  const intent = await call('/creator/me/media/intent', otherToken, 'POST', { kind: 'IMAGE', mimeType: 'image/jpeg', fileSize: 4 });
  assert.equal(intent.response.status, 201); assert.match(intent.data.uploadUrl, new RegExp(`^/creator/me/media/${intent.data.mediaId}/data$`));
  const put = await call(intent.data.uploadUrl, otherToken, 'POST', { dataBase64: Buffer.from([255, 216, 255, 217]).toString('base64') });
  assert.equal(put.response.status, 201); assert.equal(put.data.status, 'UPLOADED');
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

test('Telegram channel connection challenge is short-lived, single-use and cross-user safe', async () => {
  const previousFetch = globalThis.fetch;
  process.env.TELEGRAM_BOT_TOKEN = 'test-token'; process.env.TELEGRAM_WEBHOOK_SECRET = 'test-webhook-secret';
  let preparedRequestBody: any;
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.startsWith('https://api.telegram.org/')) return previousFetch(input, init);
    const method = url.split('/').pop();
    if (method === 'savePreparedKeyboardButton') { preparedRequestBody = JSON.parse(String(init?.body)); return new Response(JSON.stringify({ ok: true, result: { id: 'prepared-native-1' } }), { status: 200 }); }
    if (method === 'sendMessage') return new Response(JSON.stringify({ ok: true, result: { message_id: 1 } }), { status: 200 });
    if (method === 'getMe') return new Response(JSON.stringify({ ok: true, result: { id: 999 } }), { status: 200 });
    if (method === 'getChat') return new Response(JSON.stringify({ ok: true, result: { id: 123, type: 'channel', title: 'Test Channel' } }), { status: 200 });
    if (method === 'getChatMember') return new Response(JSON.stringify({ ok: true, result: { status: 'administrator', can_post_messages: true } }), { status: 200 });
    return new Response(JSON.stringify({ ok: false, description: 'unexpected mock method' }), { status: 400 });
  };
  const created: string[] = [];
  try {
    const prepared = await call('/creator/channel/selection/prepare', creatorToken, 'POST');
    assert.equal(prepared.response.status, 201); assert.equal(prepared.data.requestId, 'prepared-native-1');
    const requestChat = preparedRequestBody.button.request_chat;
    assert.equal(requestChat.chat_is_channel, true);
    assert.equal(requestChat.user_administrator_rights.can_manage_chat, true);
    assert.equal(requestChat.user_administrator_rights.can_promote_members, false);
    assert.equal(requestChat.bot_administrator_rights, undefined);
    assert.equal(requestChat.bot_is_member, undefined);
    created.push(prepared.data.challengeId); const preparedChallenge = await prisma.telegramConnectionChallenge.findUnique({ where: { id: prepared.data.challengeId } });
    assert.equal(preparedChallenge?.preparedButtonId, 'prepared-native-1');
    const startChannel = await call('/creator/channel/start', creatorToken, 'POST');
    assert.equal(startChannel.response.status, 201); assert.equal(startChannel.data.status, 'WAITING_FOR_CHANNEL'); created.push(startChannel.data.challengeId);
    const wrongActorStart = await fetch(`${base}/internal/telegram/my-chat-member`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': 'test-webhook-secret' }, body: JSON.stringify({ updateId: 9001, from: { id: 'wrong-user' }, chat: { id: 123, type: 'channel' }, new_chat_member: { status: 'administrator', can_post_messages: true } }) });
    assert.equal(wrongActorStart.status, 401);
    const actor = (await prisma.user.findUniqueOrThrow({ where: { id: (await prisma.creatorOwner.findFirstOrThrow({ where: { creatorId }, select: { userId: true } })).userId }, select: { telegramUserId: true } })).telegramUserId.toString();
    const installed = await fetch(`${base}/internal/telegram/my-chat-member`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': 'test-webhook-secret' }, body: JSON.stringify({ updateId: 9002, from: { id: actor }, chat: { id: 123, type: 'channel', title: 'Test Channel' }, new_chat_member: { status: 'administrator', can_post_messages: true } }) });
    assert.equal(installed.status, 201); assert.equal((await installed.json() as { status?: string }).status, 'CONNECTED');
    const connectedProfile = await prisma.creatorProfile.findUnique({ where: { creatorId } });
    assert.equal(connectedProfile?.status, 'APPROVED');
    const autoApprovalEvent = await prisma.productEvent.findFirst({ where: { creatorId, eventType: 'CREATOR_AUTO_APPROVED', entityType: 'TELEGRAM_CHANNEL' } });
    assert.ok(autoApprovalEvent);
    assert.equal((autoApprovalEvent?.metadataJson as { userAdministratorVerified?: boolean })?.userAdministratorVerified, true);
    const started = await call('/creator/channel/connect/start', creatorToken, 'POST');
    assert.equal(started.response.status, 201); assert.ok(started.data.requestId); created.push(started.data.challengeId);
    const wrongUser = await fetch(`${base}/internal/telegram/chat-shared`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': 'test-webhook-secret' }, body: JSON.stringify({ from: { id: 'wrong-user' }, chat_shared: { request_id: started.data.requestId, chat_id: 123 } }) });
    assert.equal(wrongUser.status, 401);
    const expired = await prisma.telegramConnectionChallenge.create({ data: { requestId: Number(started.data.requestId) + 1, userId: (await prisma.creatorOwner.findFirstOrThrow({ where: { creatorId }, select: { userId: true } })).userId, expiresAt: new Date(Date.now() - 1) } });
    const expiredResponse = await fetch(`${base}/internal/telegram/chat-shared`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': 'test-webhook-secret' }, body: JSON.stringify({ from: { id: (await prisma.user.findFirstOrThrow({ where: { id: (await prisma.creatorOwner.findFirstOrThrow({ where: { creatorId }, select: { userId: true } })).userId }, select: { telegramUserId: true } })).telegramUserId.toString() }, chat_shared: { request_id: expired.requestId, chat_id: 123 } }) });
    assert.equal(expiredResponse.status, 401);
    await prisma.telegramConnectionChallenge.update({ where: { id: started.data.challengeId }, data: { usedAt: new Date() } });
    const replay = await fetch(`${base}/internal/telegram/chat-shared`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': 'test-webhook-secret' }, body: JSON.stringify({ from: { id: (await prisma.user.findFirstOrThrow({ where: { id: (await prisma.creatorOwner.findFirstOrThrow({ where: { creatorId }, select: { userId: true } })).userId }, select: { telegramUserId: true } })).telegramUserId.toString() }, chat_shared: { request_id: started.data.requestId, chat_id: 123 } }) });
    assert.equal(replay.status, 401);
  } finally {
    if (created.length) await prisma.telegramConnectionChallenge.deleteMany({ where: { id: { in: created } } });
    const owners = await prisma.creatorOwner.findMany({ where: { creatorId: { in: [creatorId, otherId] } }, select: { userId: true } });
    await prisma.telegramConnectionChallenge.deleteMany({ where: { userId: { in: owners.map(x => x.userId) } } });
    await prisma.telegramChannelInstallEvidence.deleteMany({ where: { attempt: { gpointUserId: { in: owners.map(x => x.userId) } } } });
    await prisma.creatorChannelInstallAttempt.deleteMany({ where: { gpointUserId: { in: owners.map(x => x.userId) } } });
    await prisma.telegramChannel.deleteMany({ where: { creatorId: { in: [creatorId, otherId] } } });
    await prisma.productEvent.deleteMany({ where: { creatorId: { in: [creatorId, otherId] } } });
    globalThis.fetch = previousFetch; delete process.env.TELEGRAM_BOT_TOKEN; delete process.env.TELEGRAM_WEBHOOK_SECRET;
  }
});
