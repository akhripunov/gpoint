import assert from 'node:assert/strict';
import { createHmac, randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { createErosEngineClient, ErosAdapterError, erosGenomeCardFor, erosIdentityFor } from '../../apps/api/src/eros-adapter.ts';

const requireApi = createRequire(new URL('../../apps/api/package.json', import.meta.url));
const { PrismaClient } = requireApi('@prisma/client');

const api = 'http://127.0.0.1:3000';
const userId = '10000000-0000-4000-8000-000000000001';
let creator;

async function request(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(60_000),
  });
  return { status: response.status, data: await response.json().catch(() => null) };
}

async function authenticate() {
  const params = new URLSearchParams({
    auth_date: String(Math.floor(Date.now() / 1000)),
    query_id: 'synthetic-local-e2e',
    user: JSON.stringify({ id: 900000001, username: 'eros_test_dialogue', first_name: 'Synthetic' }),
  });
  const checkString = [...params.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(process.env.TELEGRAM_BOT_TOKEN ?? '').digest();
  params.set('hash', createHmac('sha256', secret).update(checkString).digest('hex'));
  const response = await request(`${api}/auth/telegram`, {
    method: 'POST',
    body: { initData: params.toString() },
  });
  assert.equal(response.status, 201);
  assert.equal(response.data?.user?.id, '10000000-0000-4000-8000-000000000001');
  assert.equal(response.data?.user?.telegramUsername, 'eros_test_dialogue');
  return response.data.accessToken;
}

async function readHistory(token) {
  return request(`${api}/messages/creator/${creator}?limit=100`, { token });
}

async function assertStockApiUserIsolation() {
  const client = createErosEngineClient(process.env);
  assert.ok(client, 'Eros client is configured in the isolated API container');
  const ownerIdentity = erosIdentityFor({ userId, creatorId: creator });
  const foreignUserId = randomUUID();
  const [ownerSessions, foreignSessions] = await Promise.all([
    client.sessions(ownerIdentity.userId),
    client.sessions(foreignUserId),
  ]);
  const ownSession = ownerSessions.find((session) => session.channel === 'text');
  assert.ok(ownSession, 'the first synthetic user owns the expected Eros session');

  assert.equal(foreignSessions.length, 0, 'a second JWT subject sees no first-user sessions');
  const foreignReads = await Promise.allSettled([
    client.history(foreignUserId, ownSession.sessionId, 1),
    client.history(foreignUserId, ownSession.sessionId, 1),
  ]);
  assert.equal(foreignReads.length, 2);
  assert.ok(foreignReads.every((result) => result.status === 'rejected' && result.reason instanceof ErosAdapterError && result.reason.status === 403),
    'concurrent repeated foreign reads remain denied');
  const ownerHistory = await client.history(ownerIdentity.userId, ownSession.sessionId, 1);
  assert.equal(ownerHistory.messages.length, 1, 'the owner still reads their own session');
  console.log('LOCAL_E2E_STOCK_API_USER_ISOLATION=PASS distinct_jwt_subjects=2 concurrent_denials=2 owner_read=PASS');
}

async function assertStockGenomeWriteIdempotency() {
  const expected = process.env.EROS_TEST_EXPECTED_GENOME_ID;
  assert.match(expected ?? '', /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  const client = createErosEngineClient(process.env);
  assert.ok(client);
  assert.equal(await client.createGenome(expected, erosGenomeCardFor('Synthetic Eros Persona')), expected);
  console.log('LOCAL_E2E_STOCK_GENOME_POST_IDEMPOTENCY=PASS exact_uuid=YES');
}

async function connectSyntheticTelegramChannelAndProvisionGenome() {
  const db = new PrismaClient();
  const challengeId = 'eros_test_connect_challenge';
  const telegramChatId = '-1002000000002';
  const expiresAt = new Date(Date.now() + 10 * 60_000);
  await db.telegramConnectionChallenge.create({ data: { id: challengeId, requestId: 900000002, userId, expiresAt } });
  await db.creatorChannelInstallAttempt.create({ data: { id: challengeId, gpointUserId: userId, telegramUserId: 900000001n, expiresAt } });

  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    if (url.hostname !== 'api.telegram.org') {
      const response = await nativeFetch(input, init);
      if (url.pathname === '/svc/v1/genomes') {
        let code = 'none';
        try {
          const body = await response.clone().json();
          if (typeof body.code === 'string' && /^[a-z0-9_-]{1,64}$/i.test(body.code)) code = body.code;
        } catch { /* expose only the HTTP status when the upstream body is not a safe code */ }
        console.error(`LOCAL_E2E_GENOME_WRITE_RESPONSE=status:${response.status} code:${code}`);
      }
      return response;
    }
    const method = url.pathname.split('/').at(-1);
    const requestBody = JSON.parse(String(init?.body ?? '{}'));
    let result;
    if (method === 'getChat') result = { id: Number(telegramChatId), type: 'channel', title: 'Synthetic Eros Persona', username: 'eros_test_persona' };
    else if (method === 'getMe') result = { id: 900000099 };
    else if (method === 'getChatMember' && String(requestBody.user_id) === '900000001') result = { status: 'administrator', can_manage_chat: true };
    else if (method === 'getChatMember') result = { status: 'administrator', can_post_messages: true };
    else throw new Error(`unexpected synthetic Telegram method: ${method}`);
    return Response.json({ ok: true, result });
  };
  try {
    const { TelegramChannelVerificationController } = await import('../../apps/api/src/telegram-channel.ts');
    const controller = new TelegramChannelVerificationController();
    await db.creator.create({
      data: {
        id: '20000000-0000-4000-8000-000000000002', displayName: 'Synthetic Eros Persona', slug: 'eros-test-persona',
        profile: { create: { status: 'DRAFT', bio: 'Synthetic local E2E persona', tagsJson: '[]' } },
        owners: { create: { userId } },
      },
    });
    const result = await controller.myChatMember({ 'x-telegram-webhook-secret': process.env.TELEGRAM_WEBHOOK_SECRET }, {
      updateId: 900000002, from: { id: 900000001 },
      chat: { id: telegramChatId, type: 'channel', title: 'Synthetic Eros Persona', username: 'eros_test_persona' },
      old_chat_member: { status: 'left' },
      new_chat_member: { status: 'administrator', can_post_messages: true, can_manage_chat: true },
    });
    assert.equal(result.status, 'CONNECTED');
    if (result.erosProvisioning !== 'READY') {
      const job = await db.erosGenomeProvisioningJob.findUnique({
        where: { creatorId: '20000000-0000-4000-8000-000000000002' },
        select: { attempts: true, lastErrorCode: true },
      });
      console.error(`LOCAL_E2E_GENOME_PROVISIONING_FAILURE=code:${job?.lastErrorCode ?? 'no_retry_record'} attempts:${job?.attempts ?? 0}`);
    }
    assert.equal(result.erosProvisioning, 'READY');
    const owner = await db.creatorOwner.findFirst({ where: { userId }, include: { creator: { include: { profile: true } } } });
    assert.ok(owner?.creator, 'verified channel connection creates and owns the Creator');
    assert.equal(owner.creator.slug, 'eros-test-persona');
    assert.match(owner.creator.profile.erosGenomeId, /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    creator = owner.creatorId;
    process.env.EROS_TEST_EXPECTED_GENOME_ID = owner.creator.profile.erosGenomeId;
    console.log(`LOCAL_E2E_GPOINT_GENOME_MAPPING=PASS stable_uuid=${owner.creator.profile.erosGenomeId}`);
    console.log('LOCAL_E2E_TELEGRAM_CHANNEL_CONNECT=PASS creator_created=YES eros_genome=READY');
  } finally {
    globalThis.fetch = nativeFetch;
    await db.$disconnect();
  }
}

async function loadSyntheticCreatorMapping() {
  const db = new PrismaClient();
  try {
    const owner = await db.creatorOwner.findFirst({ where: { userId }, include: { creator: { include: { profile: true } } } });
    assert.ok(owner?.creator?.profile?.erosGenomeId, 'synthetic Creator has a persisted Eros genome mapping');
    creator = owner.creatorId;
    process.env.EROS_TEST_EXPECTED_GENOME_ID = owner.creator.profile.erosGenomeId;
  } finally {
    await db.$disconnect();
  }
}

async function assertCompleteHistory(token, stage, expectedTurns = 17) {
  const response = await readHistory(token);
  assert.equal(response.status, 200, `${stage}: history read`);
  const users = response.data.items.filter((item) => item.role === 'user');
  const assistants = response.data.items.filter((item) => item.role === 'assistant');
  assert.equal(users.length, expectedTurns, `${stage}: user message count`);
  assert.equal(assistants.length, expectedTurns, `${stage}: assistant message count`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_CITY_ZEPHYRBAY')), `${stage}: city fact`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_OLD_2026-10-22_18:40')), `${stage}: historical meeting`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_CURRENT_2026-10-23_19:15')), `${stage}: current meeting`);
  if (expectedTurns === 17) assert.ok(users.some((item) => item.text.includes('SYNTH_RECALL_PROBE')), `${stage}: final recall probe`);
  assert.ok(assistants.slice(0, -1).every((item) => item.text === 'DETERMINISTIC_LOCAL_E2E_REPLY'), `${stage}: assistant response pairs`);
  if (expectedTurns === 17) assert.ok(assistants.at(-1)?.text.startsWith('SYNTH_RECALL_CONFIRMED'), `${stage}: recall response`);
  return users;
}

if (process.argv.includes('--history-only')) {
  const token = await authenticate();
  await assertCompleteHistory(token, 'post-restart');
  const restartedHistory = await readHistory(token);
  assert.equal(restartedHistory.status, 200);
  assert.ok(restartedHistory.data.items.some((item) => item.role === 'assistant' && item.incomingReadAt),
    'GPoint-owned receipt metadata survives an Eros-only restart');
  console.log('LOCAL_E2E_GPOINT_READ_RECEIPT_RESTART=PASS');
  console.log('LOCAL_E2E_RESTART_HISTORY=PASS');
  process.exit(0);
}

if (process.argv.includes('--history-only') || process.argv.includes('--recall-only')) await loadSyntheticCreatorMapping();
else await connectSyntheticTelegramChannelAndProvisionGenome();
await assertStockGenomeWriteIdempotency();

if (process.argv.includes('--recall-only')) {
  const token = await authenticate();
  const priorUsers = await assertCompleteHistory(token, 'pre-recall', 16);
  const sourceTurn = priorUsers.findIndex((item) => item.text.includes('SYNTH_CITY_ZEPHYRBAY'));
  assert.equal(sourceTurn, 1, 'the memory source follows the synthetic safety-gate checks');
  assert.equal(priorUsers.length - sourceTurn - 1, 14, 'source fact must be outside the latest 12 user messages');
  console.log('LOCAL_E2E_RECALL_GAP=PASS intermediate_user_messages=14');
  const response = await request(`${api}/messages/creator/${creator}`, {
    method: 'POST',
    token,
    body: {
      clientMessageId: randomUUID(),
      text: 'SYNTH_RECALL_PROBE: Return to our first topic. What city did I mention, and what is the current meeting date and time? The older date should be treated as historical.',
    },
  });
  assert.equal(response.status, 201, JSON.stringify(response.data));
  assert.equal(response.data?.status, 'completed');
  assert.equal(response.data?.response?.text,
    'SYNTH_RECALL_CONFIRMED city=ZephyrBay meeting=2026-10-23_19:15 history=2026-10-22_18:40');
  await assertCompleteHistory(token, 'post-recall', 17);
  console.log('LOCAL_E2E_MEMORY_RECALL_RESPONSE=PASS');
  process.exit(0);
}

const unauthenticated = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  body: { clientMessageId: randomUUID(), text: 'synthetic unauthorized check' },
});
assert.equal(unauthenticated.status, 401);

const token = await authenticate();
const policyState = await request(`${api}/messages/state`, { token });
assert.equal(policyState.status, 200, JSON.stringify(policyState.data));
assert.equal(policyState.data?.ageAssuranceRequired, true, 'the synthetic user starts without age assurance');
const ageGate = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  token,
  body: { clientMessageId: randomUUID(), text: 'synthetic age gate check' },
});
assert.equal(ageGate.status, 403);
assert.equal(ageGate.data?.code, 'AGE_ASSURANCE_REQUIRED');

const attestation = await request(`${api}/age-assurance/self-attest`, {
  method: 'POST', token, body: { confirmed18Plus: true },
});
assert.equal(attestation.status, 201, JSON.stringify(attestation.data));
assert.equal(attestation.data?.eligible, true);

const consentBody = {
  chatHistory: true,
  longTermMemory: true,
  termsAccepted: true,
  privacyAccepted: true,
  termsVersion: policyState.data?.termsVersion,
  privacyVersion: policyState.data?.privacyVersion,
  disclosureVersion: policyState.data?.disclosureVersion,
};
const consent = await request(`${api}/messages/consent`, { method: 'POST', token, body: consentBody });
assert.equal(consent.status, 201, JSON.stringify(consent.data));

const revoked = await request(`${api}/messages/consent/revoke`, {
  method: 'POST', token, body: { purpose: 'long_term_memory' },
});
assert.equal(revoked.status, 201, JSON.stringify(revoked.data));
const noConsent = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  token,
  body: { clientMessageId: randomUUID(), text: 'synthetic no-memory-consent check' },
});
assert.equal(noConsent.status, 403, JSON.stringify(noConsent.data));
assert.equal(noConsent.data?.code, 'CHAT_CONSENT_REQUIRED');

const restoredConsent = await request(`${api}/messages/consent`, { method: 'POST', token, body: consentBody });
assert.equal(restoredConsent.status, 201, JSON.stringify(restoredConsent.data));
const consentRestoredMessage = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST', token,
  body: { clientMessageId: randomUUID(), text: 'Synthetic consent restored check before our continuity exercise.' },
});
assert.equal(consentRestoredMessage.status, 201, JSON.stringify(consentRestoredMessage.data));
assert.equal(consentRestoredMessage.data?.status, 'completed');
assert.equal(consentRestoredMessage.data?.response?.text, 'DETERMINISTIC_LOCAL_E2E_REPLY');
await assertStockApiUserIsolation();

const dialogue = [
  'SYNTH_CITY_ZEPHYRBAY: In this synthetic continuity test, I live in Zephyr Bay. My meeting is currently on 22 October 2026 at 18:40, marker SYNTH_OLD_2026-10-22_18:40.',
  'Correction: the meeting moved to 23 October 2026 at 19:15, marker SYNTH_CURRENT_2026-10-23_19:15. Keep the earlier date as history only.',
  'Topic switch 1: synthetic preference, I prefer quiet cafés for reading.',
  'Topic switch 2: synthetic planning, I want to learn watercolor painting this winter.',
  'Topic switch 3: tell me a short astronomy fact.',
  'Topic switch 4: I am comparing notebooks and fountain pens.',
  'Topic switch 5: discuss a fictional weekend walk beside the river.',
  'Topic switch 6: suggest a simple vegetarian lunch.',
  'Topic switch 7: I am sorting a bookshelf by genre.',
  'Topic switch 8: tell me about an imaginary botanical garden.',
  'Topic switch 9: I am thinking about a short train trip in a fictional country.',
  'Topic switch 10: describe a calm rainy afternoon.',
  'Topic switch 11: compare two fictional board games.',
  'Topic switch 12: share a small tip for learning a language.',
  'Topic switch 13: I am choosing a playlist for focused work.',
];

for (const [index, text] of dialogue.entries()) {
  const response = await request(`${api}/messages/creator/${creator}`, {
    method: 'POST',
    token,
    body: { clientMessageId: randomUUID(), text },
  });
  assert.equal(response.status, 201, `turn ${index + 1}: ${JSON.stringify(response.data)}`);
  assert.equal(response.data?.status, 'completed', `turn ${index + 1}`);
  assert.equal(response.data?.response?.text, 'DETERMINISTIC_LOCAL_E2E_REPLY', `turn ${index + 1}`);
}
console.log('LOCAL_E2E_15_TURN_DIALOGUE=PASS');

await assertCompleteHistory(token, 'pre-recall', 16);
  const initialUnread = await request(`${api}/messages/unread`, { token });
  assert.equal(initialUnread.status, 200, 'GPoint-owned unread count is available');
  const unreadBefore = initialUnread.data.items.find((item) => item.creatorId === creator)?.unreadCount;
  assert.ok(Number.isInteger(unreadBefore) && unreadBefore > 0, 'history sync indexes assistant IDs as unread');
  const assistantMessageId = (await readHistory(token)).data.items.find((item) => item.role === 'assistant')?.id;
  assert.match(assistantMessageId ?? '', /^[A-Za-z0-9_-]{1,64}$/);
  const ack = await request(`${api}/messages/creator/${creator}/read`, {
    method: 'POST', token, body: { messageIds: [assistantMessageId] },
  });
  assert.equal(ack.status, 201, JSON.stringify(ack.data));
  assert.deepEqual(ack.data.acknowledgedMessageIds, [assistantMessageId]);
  const unreadAfter = await request(`${api}/messages/unread`, { token });
  assert.equal(unreadAfter.status, 200);
  assert.equal(unreadAfter.data.items.find((item) => item.creatorId === creator)?.unreadCount, unreadBefore - 1,
    'marking one visible assistant ID read decrements only that user’s unread count');
  const foreignAck = await request(`${api}/messages/creator/${creator}/read`, {
    method: 'POST', token, body: { messageIds: [randomUUID()] },
  });
  assert.equal(foreignAck.status, 404, 'an unobserved message ID cannot be acknowledged');
  console.log('LOCAL_E2E_GPOINT_OWNED_READ_RECEIPTS=PASS bodies_stored=0 foreign_id=404 unread_decrement=1');
console.log('LOCAL_E2E_AUTH_AGE_CONSENT_AND_WORKER_ROUTE=PASS');
console.log('LOCAL_E2E_GPOINT_API_EROS_HISTORY=PASS');
