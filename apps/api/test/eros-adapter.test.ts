import assert from 'node:assert/strict';
import test from 'node:test';
import { jwtVerify } from 'jose';
import { verifyBridgeIdentity } from '../src/eros-worker-contract.js';
import { createErosEngineClient, decodeErosCursor, encodeErosCursor, ErosAdapterError, ErosEngineClient, erosIdentityFor, erosTestCreatorIds, isErosChatAllowed, isSyntheticErosPair, markErosAssistantMessagesRead, mergeErosInboxItems, erosInboxLastMessageSummary, prepareErosRelationship, resolveErosRelationshipInstanceId, unreadErosAssistantCounts, uuidV5 } from '../src/eros-adapter.js';

test('Eros client uses the configured bounded request timeout', async () => {
  let observedTimeout: number | undefined;
  const originalTimeout = AbortSignal.timeout;
  AbortSignal.timeout = (milliseconds: number) => {
    observedTimeout = milliseconds;
    return originalTimeout(milliseconds);
  };
  try {
    const fetchImpl: typeof fetch = async () => Response.json({ total: 0, messages: [] });
    const client = createErosEngineClient({
      NODE_ENV: 'test',
      EROS_ENABLED: 'true',
      EROS_SERVICE_URL: 'http://eros:8080',
      EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
      EROS_REQUEST_TIMEOUT_MS: '240000',
    }, fetchImpl)!;
    await client.history('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1');
    assert.equal(observedTimeout, 240_000);
  } finally {
    AbortSignal.timeout = originalTimeout;
  }
});

test('Eros client can be explicitly enabled in production', () => {
  const base = {
    NODE_ENV: 'production',
    EROS_SERVICE_URL: 'http://eros:8080',
    EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
  };
  assert.ok(createErosEngineClient({ ...base, EROS_ENABLED: 'true' }));
  assert.equal(createErosEngineClient({ ...base, EROS_ENABLED: 'false' }), null);
});

test('Eros timeout configuration accepts only decimal milliseconds within the worker deadline', () => {
  const base = {
    NODE_ENV: 'test',
    EROS_ENABLED: 'true',
    EROS_SERVICE_URL: 'http://eros:8080',
    EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
  };
  assert.ok(createErosEngineClient({ ...base, EROS_REQUEST_TIMEOUT_MS: '210000' }));
  for (const value of ['999', '240001', '1e3', '0x3e8', ' 1000']) {
    assert.throws(
      () => createErosEngineClient({ ...base, EROS_REQUEST_TIMEOUT_MS: value }),
      (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_timeout_invalid',
    );
  }
});

test('production Eros routing is limited to explicitly enabled synthetic pilot identities', () => {
  const production = {
    NODE_ENV: 'production',
    EROS_ENABLED: 'true',
    EROS_PRODUCTION_TEST_MODE: 'true',
    EROS_DATA_MODE: 'synthetic',
    EROS_PRODUCTION_TEST_USER_IDS: 'pilot-user-1,pilot-user-2',
    EROS_PRODUCTION_TEST_CREATOR_IDS: 'pilot-creator-1',
  };
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-1', production), true);
  assert.equal(isErosChatAllowed('pilot-user-1-extra', 'pilot-creator-1', production), false);
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-2', production), false);
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-1', { ...production, EROS_PRODUCTION_TEST_MODE: 'false' }), false);
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-1', { ...production, EROS_DATA_MODE: 'shared' }), false);
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-1', { ...production, EROS_PRODUCTION_TEST_USER_IDS: '' }), false);
  assert.equal(isErosChatAllowed('pilot-user-1', 'pilot-creator-1', { ...production, EROS_PRODUCTION_TEST_CREATOR_IDS: '' }), false);
  assert.deepEqual(erosTestCreatorIds(production), ['pilot-creator-1']);
  assert.equal(isSyntheticErosPair(
    { telegramUsername: 'eros_test_pilot' },
    { slug: 'eros-test-pilot', profileStatus: 'APPROVED' },
  ), true);
  assert.equal(isSyntheticErosPair(
    { telegramUsername: 'real_user' },
    { slug: 'eros-test-pilot', profileStatus: 'APPROVED' },
  ), false);
});

test('UUIDv5 identity mapping is stable and separates GPoint users, personas, and relationships', () => {
  assert.equal(uuidV5('6ba7b810-9dad-11d1-80b4-00c04fd430c8', 'www.widgets.com'), '21f7f8de-8051-5b89-8680-0195ef798b6a');
  const first = erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' });
  const repeated = erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' });
  const otherUser = erosIdentityFor({ userId: 'user_cuid_2', creatorId: 'creator_cuid_1' });
  const otherCreator = erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_2' });

  assert.deepEqual(first, repeated);
  assert.notEqual(first.userId, otherUser.userId);
  assert.notEqual(first.creatorId, otherCreator.creatorId);
  assert.notEqual(first.relationshipId, otherUser.relationshipId);
  assert.notEqual(first.relationshipId, otherCreator.relationshipId);
  for (const id of [first.userId, first.creatorId, first.relationshipId]) {
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  }
});

test('relationship instance is created through stock Eros API and mapped in GPoint', async () => {
  const mappingWrites: unknown[][] = [];
  const store = {
    async getInstanceId() { return null; },
    async saveInstanceId(...args: unknown[]) { mappingWrites.push(args); },
  };
  const identity = erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' });
  const observed: unknown[][] = [];
  const client = {
    async startSession(...args: unknown[]) {
      observed.push(args);
      return { sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', instanceId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9' };
    },
  };

  const result = await prepareErosRelationship(client, store, {
    userId: 'user_cuid_1',
    creator: { id: 'creator_cuid_1', displayName: 'Mira', genomeId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc8' },
  });

  assert.equal(result.session.instanceId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9');
  assert.deepEqual(observed, [[identity.userId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc8', undefined]]);
  assert.deepEqual(mappingWrites, [['user_cuid_1', 'creator_cuid_1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9']]);
});

test('mapped relationships resume by stock instance ID and never fall back to genome reactivation', async () => {
  const existingInstanceId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9';
  const mappingWrites: unknown[][] = [];
  const store = {
    async getInstanceId(userId: string, creatorId: string) {
      assert.equal(userId, 'user_cuid_1');
      assert.equal(creatorId, 'creator_cuid_1');
      return existingInstanceId;
    },
    async saveInstanceId(...args: unknown[]) { mappingWrites.push(args); },
  };
  const observed: unknown[][] = [];
  const client = {
    async startSession(...args: unknown[]) {
      observed.push(args);
      return { sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', instanceId: existingInstanceId };
    },
  };

  await prepareErosRelationship(client, store, {
    userId: 'user_cuid_1',
    creator: { id: 'creator_cuid_1', displayName: 'Mira', genomeId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc8' },
  });

  assert.deepEqual(observed, [[
    erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' }).userId,
    '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc8', existingInstanceId,
  ]]);
  assert.deepEqual(mappingWrites, []);
});

test('mapped relationship resumes without requiring genome provisioning again', async () => {
  const existingInstanceId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9';
  const observed: unknown[][] = [];
  const store = {
    async getInstanceId() { return existingInstanceId; },
    async saveInstanceId() { assert.fail('existing mapping should only be read'); },
  };
  const client = {
    async startSession(...args: unknown[]) {
      observed.push(args);
      return { sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', instanceId: existingInstanceId };
    },
  };

  const result = await prepareErosRelationship(client, store, {
    userId: 'user_cuid_1',
    creator: { id: 'creator_cuid_1', displayName: 'Mira', genomeId: null },
  });

  assert.equal(result.session.instanceId, existingInstanceId);
  assert.deepEqual(observed, [[
    erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' }).userId,
    null,
    existingInstanceId,
  ]]);
});

test('relationship instance mapping is looked up from GPoint by owner and creator', async () => {
  const observed: unknown[][] = [];
  const store = {
    async getInstanceId(...args: unknown[]) { observed.push(args); return '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9'; },
    async saveInstanceId() { assert.fail('lookup must not write'); },
  };
  const result = await resolveErosRelationshipInstanceId(store, 'user_cuid_1', 'creator_cuid_1');
  assert.equal(result, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9');
  assert.deepEqual(observed, [['user_cuid_1', 'creator_cuid_1']]);
});

test('relationship preparation fails closed when persona has not been seeded by stock Eros CLI', async () => {
  let started = false;
  let saved = false;
  const client = { async startSession() { started = true; throw new Error('must not call Eros'); } };
  const store = {
    async getInstanceId() { return null; },
    async saveInstanceId() { saved = true; },
  };
  await assert.rejects(
    prepareErosRelationship(client, store, { userId: 'user_cuid_1', creator: { id: 'creator_cuid_1', displayName: 'Mira' } }),
    (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_persona_not_provisioned',
  );
  assert.equal(started, false);
  assert.equal(saved, false);
});

test('local Eros routing requires a non-production process and exact synthetic allowlist matches', () => {
  const enabled = {
    NODE_ENV: 'development',
    EROS_ENABLED: 'true',
    EROS_DATA_MODE: 'synthetic',
    EROS_TEST_USER_IDS: 'test-user-1,test-user-2',
    EROS_TEST_CREATOR_IDS: 'test-creator-1',
    DATABASE_URL: 'postgresql://local:test@127.0.0.1:5432/gpoint_test',
    EROS_ADAPTER_DATABASE_URL: 'postgresql://local:test@localhost:5432/gpoint_eros',
  };

  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', enabled), true);
  assert.equal(isErosChatAllowed('test-user-1x', 'test-creator-1', enabled), false);
  assert.equal(isErosChatAllowed('test-user-1', 'other-creator', enabled), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, NODE_ENV: 'production' }), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, EROS_ENABLED: 'false' }), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, EROS_DATA_MODE: 'shared' }), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, DATABASE_URL: 'postgresql://host/gpoint_test' }), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, EROS_ADAPTER_DATABASE_URL: 'postgresql://127.0.0.1:5432/gpoint' }), false);
  assert.equal(isErosChatAllowed('test-user-1', 'test-creator-1', { ...enabled, NODE_ENV: undefined }), false);
});

test('Eros routing requires test markers on the actual allowlisted user and approved creator rows', () => {
  const user = { telegramUsername: 'eros_test_owner', firstName: 'Synthetic Test' };
  const creator = { slug: 'eros-test-mira', displayName: 'Mira', profileStatus: 'APPROVED' };
  assert.equal(isSyntheticErosPair(user, creator), true);
  assert.equal(isSyntheticErosPair({ ...user, telegramUsername: 'real_owner' }, creator), false);
  assert.equal(isSyntheticErosPair(user, { ...creator, slug: 'mira' }), false);
  assert.equal(isSyntheticErosPair(user, { ...creator, profileStatus: 'DRAFT' }), false);
});

test('Eros client uses mapped JWT identity and reuses GPoint clientMessageId as idempotency key', async () => {
  const secret = 'unit-test-eros-jwt-secret-at-least-32-bytes';
  const userId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0';
  const sessionId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1';
  const genomeId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2';
  const messageId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3';
  const calls: Array<{ url: string; init: RequestInit; body: Record<string, unknown> }> = [];
  const fetchImpl: typeof fetch = async (input, init = {}) => {
    const headers = new Headers(init.headers);
    const token = headers.get('authorization')!.slice('Bearer '.length);
    const verified = await jwtVerify(token, new TextEncoder().encode(secret));
    assert.equal(verified.payload.sub, userId);
    assert.equal(verified.payload.aud, 'eros-engine');
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    const url = String(input);
    calls.push({ url, init, body });
    if (url.endsWith('/comp/chat/start')) {
      return Response.json({ session_id: sessionId, instance_id: genomeId, persona_name: 'Mira', is_new: true });
    }
    return new Response([
      `data: ${JSON.stringify({ type: 'meta', message_id: messageId, action_type: 'reply' })}`,
      `data: ${JSON.stringify({ type: 'delta', content: 'Привет!' })}`,
      `data: ${JSON.stringify({ type: 'done', ghost_fallback: false })}`,
      `data: ${JSON.stringify({ type: 'final' })}`,
    ].join('\n\n') + '\n\n', { headers: { 'content-type': 'text/event-stream' } });
  };
  const workerSecret = 'unit-test-worker-signing-secret-at-least-32-bytes';
  const workerTenantId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5';
  const client = new ErosEngineClient({
    baseUrl: 'http://eros:8080', jwtSecret: secret, workerSigningSecret: workerSecret,
    workerTenantId, fetchImpl,
  });

  const relationshipId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4';
  const session = await client.startSession(userId, genomeId, relationshipId);
  const reply = await client.sendMessage(userId, session.sessionId, messageId, 'Привет', {
    creatorId: genomeId, instanceId: relationshipId,
  });

  assert.deepEqual(session, { sessionId, instanceId: genomeId });
  assert.deepEqual(reply, { id: messageId, text: 'Привет!' });
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.client_msg_id, messageId);
  assert.equal(calls[1].body.memory_scope, 'neutral_and_relationship');
  const audit = calls[1].body.audit as { user?: string };
  assert.equal(typeof audit?.user, 'string');
  assert.ok(audit.user!.length <= 256, 'stock Eros caps audit.user at 256 characters');
  const bridge = await verifyBridgeIdentity(audit.user!, new TextEncoder().encode(workerSecret));
  assert.equal(bridge.userId, userId);
  assert.equal(bridge.tenantId, workerTenantId);
  assert.equal(bridge.creatorId, genomeId);
  assert.equal(bridge.sessionId, session.sessionId);
  assert.equal(bridge.instanceId, relationshipId);
  assert.equal(bridge.requestId, messageId);
  assert.equal(calls[0].body.instance_id, relationshipId);
  assert.equal(calls[1].body.content, 'Привет');
  assert.equal(calls[1].init.redirect, 'error');
});

const bridgeTestIdentity = { creatorId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3', instanceId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4' };
const bridgeTestOptions = { workerSigningSecret: 'unit-test-worker-signing-secret-at-least-32-bytes', workerTenantId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5' };

test('Eros client fails closed on an upstream ghost fallback', async () => {
  const fetchImpl: typeof fetch = async () => new Response([
    `data: ${JSON.stringify({ type: 'meta', message_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3', action_type: 'reply' })}`,
    `data: ${JSON.stringify({ type: 'delta', content: 'fallback' })}`,
    `data: ${JSON.stringify({ type: 'done', ghost_fallback: true })}`,
  ].join('\n\n') + '\n\n', { headers: { 'content-type': 'text/event-stream' } });
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', ...bridgeTestOptions, fetchImpl });
  await assert.rejects(
    client.sendMessage('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 'hello', bridgeTestIdentity),
    (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_fallback_response',
  );
});

test('Eros stream discards truncated attempt text and only returns a complete final reply', async () => {
  const frames = [
    { type: 'meta', message_id: '01JABCDEF0123456789ABCDEFGH', action_type: 'reply' },
    { type: 'delta', content: 'partial answer' },
    { type: 'done', truncated: true },
    { type: 'meta', message_id: '01JBCDEF0123456789ABCDEFGH1', action_type: 'reply' },
    { type: 'delta', content: 'complete answer' },
    { type: 'done', truncated: false, ghost_fallback: false },
    { type: 'final', filtered: false },
  ];
  const fetchImpl: typeof fetch = async () => new Response(frames.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), {
    headers: { 'content-type': 'text/event-stream' },
  });
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', ...bridgeTestOptions, fetchImpl });
  const reply = await client.sendMessage('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 'hello', bridgeTestIdentity);
  assert.deepEqual(reply, { id: '01JBCDEF0123456789ABCDEFGH1', text: 'complete answer' });
});

test('Eros stream rejects a partial reply without protocol terminal frames', async () => {
  const fetchImpl: typeof fetch = async () => new Response([
    `data: ${JSON.stringify({ type: 'meta', message_id: '01JABCDEF0123456789ABCDEFGH', action_type: 'reply' })}`,
    `data: ${JSON.stringify({ type: 'delta', content: 'partial' })}`,
  ].join('\n\n') + '\n\n', { headers: { 'content-type': 'text/event-stream' } });
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', ...bridgeTestOptions, fetchImpl });
  await assert.rejects(
    client.sendMessage('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 'hello', bridgeTestIdentity),
    (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_response_invalid',
  );
});

test('Eros cancels the upstream stream after a protocol error', async () => {
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: 'error', code: 'provider_unavailable' })}\n\n`));
    },
    cancel() { cancelled = true; },
  });
  const fetchImpl: typeof fetch = async () => new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', ...bridgeTestOptions, fetchImpl });

  await assert.rejects(
    client.sendMessage('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 'hello', bridgeTestIdentity),
    (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_provider_unavailable',
  );
  assert.equal(cancelled, true);
});

test('Eros refuses to send bearer JWTs to non-local HTTP endpoints', () => {
  for (const baseUrl of ['http://evil.example', 'https://eros.example', 'http://user:pass@eros:8080']) {
    assert.throws(() => new ErosEngineClient({ baseUrl, jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes' }), /eros_service_url_invalid/);
  }
});

test('Eros history preserves user client message ids for timeout reconciliation without leaking engine-only fields', async () => {
  const fetchImpl: typeof fetch = async (input) => {
    assert.match(String(input), /\/bff\/v1\/comp\/chat\/7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1\/history\?limit=20$/);
    return Response.json({ session_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', total: 1, messages: [
      { id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4', role: 'user', content: 'Hi', sent_at: '2026-10-16T07:00:00Z', client_msg_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5', extracted_facts: { private: true } },
    ] });
  };
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', fetchImpl });
  const history = await client.history('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', 20);
  assert.deepEqual(history, {
    total: 1,
    messages: [{ id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4', role: 'user', content: 'Hi', sent_at: '2026-10-16T07:00:00Z', client_msg_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5' }],
  });
});

test('Eros history clamps pages to the upstream 50-message cap and uses a stable keyset cursor', async () => {
  const before = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3';
  const fetchImpl: typeof fetch = async (input) => {
    assert.ok(String(input).endsWith(`history?limit=50&before=${before}`));
    return Response.json({ total: 50, messages: Array.from({ length: 50 }, (_, i) => ({
      id: `7f43ff08-7bf9-5647-9dc2-bb4a46f8ed${String(i).padStart(2, '0')}`, role: 'user', content: 'hi', sent_at: '2026-10-16T07:00:00Z',
    })) });
  };
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', fetchImpl });
  const history = await client.history('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', 100, before);
  assert.equal(history.messages.length, 50);
  assert.equal(history.total, 50);
});

test('Eros session listing is user-scoped, validates sessions, and preserves channel identity', async () => {
  const userId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0';
  const sessionId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1';
  const instanceId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2';
  const calls: Array<{ url: string; method: string }> = [];
  const fetchImpl: typeof fetch = async (input, init = {}) => {
    const url = String(input);
    const method = init.method ?? 'GET';
    calls.push({ url, method });
    if (method === 'GET') return Response.json({ sessions: [
      { session_id: sessionId, instance_id: instanceId, last_active_at: '2026-10-16T07:00:00Z', channel: 'text' },
      { session_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5', instance_id: null, last_active_at: '2026-10-16T07:01:00Z', channel: 'group' },
    ] });
    return Response.json({ session_id: sessionId, marked: 2 });
  };
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', fetchImpl });
  const sessions = await client.sessions(userId);
  assert.deepEqual(sessions, [
    { sessionId, instanceId, lastActiveAt: '2026-10-16T07:00:00Z', channel: 'text' },
    { sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5', instanceId: null, lastActiveAt: '2026-10-16T07:01:00Z', channel: 'group' },
  ]);
  assert.ok(calls[0].url.endsWith(`/comp/chat/${userId}/sessions`));
  assert.equal(calls.length, 1, 'message-level acknowledgements must not call the session-wide Eros read endpoint');
});

test('message-level read acknowledgement is limited to exact assistant IDs in the owned text session', async () => {
  const calls: unknown[][] = [];
  const db = { async $queryRawUnsafe<T>(...args: unknown[]): Promise<T> {
    calls.push(args);
    return [{ id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4' }] as T;
  } };
  const ids = ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4'];
  const marked = await markErosAssistantMessagesRead(db, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', ids);
  assert.deepEqual(marked, ids);
  assert.match(String(calls[0][0]), /UPDATE engine\.chat_messages/);
  assert.match(String(calls[0][0]), /s\.channel = 'text'/);
  assert.match(String(calls[0][0]), /m\.role = 'assistant'/);
  assert.deepEqual(calls[0].slice(1), ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', ids]);
});

test('unread counts cover all assistant messages and remain session-owner/text-channel scoped', async () => {
  const calls: unknown[][] = [];
  const db = { async $queryRawUnsafe<T>(...args: unknown[]): Promise<T> {
    calls.push(args);
    return [{ session_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', unread_count: 73 }] as T;
  } };
  const counts = await unreadErosAssistantCounts(db, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1']);
  assert.equal(counts.get('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1'), 73);
  assert.match(String(calls[0][0]), /s\.user_id = \$1::uuid AND s\.channel = 'text'/);
  assert.match(String(calls[0][0]), /m\.read_at IS NULL/);
  assert.deepEqual(calls[0].slice(1), ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1']]);
});

test('Eros inbox replaces a routed creator only on the first page and filters it from later AGATA pages', () => {
  const agata = [{ creator: { id: 'creator-a' }, lastMessageAt: '2026-10-16T08:00:00Z' }, { creator: { id: 'creator-b' }, lastMessageAt: '2026-10-16T06:00:00Z' }];
  const eros = [{ creator: { id: 'creator-a' }, lastMessageAt: '2026-10-16T07:00:00Z' }];
  assert.deepEqual(mergeErosInboxItems(agata, eros, true), [eros[0], agata[1]]);
  assert.deepEqual(mergeErosInboxItems([{ creator: { id: 'creator-a' }, lastMessageAt: '2026-10-16T08:00:00Z' }, { creator: { id: 'creator-c' }, lastMessageAt: '2026-10-16T05:00:00Z' }], eros, false), [{ creator: { id: 'creator-c' }, lastMessageAt: '2026-10-16T05:00:00Z' }]);
});

test('system_error previews are normalized and never exposed as assistant-authored content', () => {
  assert.deepEqual(erosInboxLastMessageSummary({ role: 'system_error', content: 'private internal upstream detail' }), {
    lastMessagePreview: 'Сообщение не удалось обработать. Попробуйте ещё раз.',
    lastMessageRole: 'system',
  });
});

test('Eros chat cursors are opaque message IDs and reject malformed values', () => {
  const id = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1';
  assert.equal(encodeErosCursor(id), `eros-before:${id}`);
  assert.equal(decodeErosCursor(`eros-before:${id}`), id);
  assert.equal(decodeErosCursor(undefined), undefined);
  for (const value of ['50', 'eros-offset:50', 'eros-before:nope', 'other:50']) {
    assert.throws(() => decodeErosCursor(value), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_cursor_invalid');
  }
});
