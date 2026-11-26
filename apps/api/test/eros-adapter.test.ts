import assert from 'node:assert/strict';
import { GPOINT_EROS_RESPONSE_PROMPT_TRAITS } from '../src/eros-response-prompt.js';
import test from 'node:test';
import { jwtVerify } from 'jose';
import { verifyBridgeIdentity } from '../src/eros-worker-contract.js';
import { createErosWorldEnrollment, enrollErosWorld, worldEnrollmentConnection } from '../src/eros-world-enrollment.js';
import { PrismaClient } from '@prisma/client';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createErosEngineClient, decodeErosCursor, encodeErosCursor, ErosAdapterError, ErosEngineClient, erosGenomeCardFor, erosGenomeIdentityFor, erosIdentityFor, erosTestCreatorIds, isErosChatAllowed, isSyntheticErosPair, markErosAssistantMessagesRead, erosInboxLastMessageSummary, prepareErosRelationship, recordErosAssistantMessage, resolveErosRelationshipInstanceId, syncErosAssistantMessageStates, unreadErosAssistantCounts, uuidV5 } from '../src/eros-adapter.js';

test('Creator genomes use a stable identity namespace and a non-invented stock card', () => {
  const genomeId = erosGenomeIdentityFor('creator_cuid_1');
  assert.equal(genomeId, erosGenomeIdentityFor('creator_cuid_1'));
  assert.notEqual(genomeId, erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_1' }).creatorId);
  assert.notEqual(genomeId, erosIdentityFor({ userId: 'user_cuid_1', creatorId: 'creator_cuid_2' }).relationshipId);
  assert.deepEqual(erosGenomeCardFor('  Creator Name  '), {
    spec: 'eros-character',
    spec_version: '1.1',
    data: {
      name: 'Creator Name',
      system_prompt: 'You are a creator on GPoint. Speak naturally and respectfully. Do not invent personal facts; ask when information is missing. Respect applicable safety and privacy rules.',
      tip_personality: null,
      art_metadata: {},
    },
  });
  assert.throws(() => erosGenomeCardFor('  '), /eros_genome_card_invalid/);
});

test('stock genome write uses the dedicated secret and accepts only exact 200/201 UUID confirmation', async () => {
  const creatorId = erosGenomeIdentityFor('creator_cuid_1');
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const client = createErosEngineClient({
    NODE_ENV: 'test', EROS_ENABLED: 'true', EROS_SERVICE_URL: 'http://eros:8080',
    EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
    EROS_GENOME_WRITE_SECRET: 'unit-test-genome-write-secret-at-least-32-bytes',
  }, async (input, init) => {
    calls.push({ url: String(input), init: init ?? {} });
    return Response.json({ id: creatorId }, { status: 201 });
  })!;
  assert.equal(await client.createGenome(creatorId, erosGenomeCardFor('Creator Name')), creatorId);
  assert.equal(calls[0].url, 'http://eros:8080/svc/v1/genomes');
  assert.equal(calls[0].init.method, 'POST');
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get('x-genome-write-secret'), 'unit-test-genome-write-secret-at-least-32-bytes');
  assert.equal(headers.get('authorization'), null);
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), { id: creatorId, card: erosGenomeCardFor('Creator Name') });
});

test('genome provisioning fails closed on a missing secret, invalid status, or mismatched UUID', async () => {
  const creatorId = erosGenomeIdentityFor('creator_cuid_1');
  const base = { NODE_ENV: 'test', EROS_ENABLED: 'true', EROS_SERVICE_URL: 'http://eros:8080', EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes' };
  const noWriteSecret = createErosEngineClient(base, async () => Response.json({ id: creatorId }, { status: 201 }))!;
  await assert.rejects(noWriteSecret.createGenome(creatorId, erosGenomeCardFor('Creator')), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_genome_write_not_configured');
  const badStatus = createErosEngineClient({ ...base, EROS_GENOME_WRITE_SECRET: 'unit-test-genome-write-secret-at-least-32-bytes' }, async () => new Response(null, { status: 204 }))!;
  await assert.rejects(badStatus.createGenome(creatorId, erosGenomeCardFor('Creator')), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_genome_write_failed');
  const wrongId = createErosEngineClient({ ...base, EROS_GENOME_WRITE_SECRET: 'unit-test-genome-write-secret-at-least-32-bytes' }, async () => Response.json({ id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0' }, { status: 200 }))!;
  await assert.rejects(wrongId.createGenome(creatorId, erosGenomeCardFor('Creator')), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_genome_response_invalid');
});

test('stock genome POST accepts idempotent 200 and PUT updates only the exact stable UUID', async () => {
  const genomeId = erosGenomeIdentityFor('creator_cuid_1');
  const calls: Array<{ url: string; method?: string; body: unknown }> = [];
  const client = createErosEngineClient({
    NODE_ENV: 'test', EROS_ENABLED: 'true', EROS_SERVICE_URL: 'http://eros:8080',
    EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
    EROS_GENOME_WRITE_SECRET: 'unit-test-genome-write-secret-at-least-32-bytes',
  }, async (input, init = {}) => {
    calls.push({ url: String(input), method: init.method, body: JSON.parse(String(init.body)) });
    return Response.json({ id: genomeId }, { status: 200 });
  })!;
  const card = erosGenomeCardFor('Updated Creator');
  assert.equal(await client.createGenome(genomeId, card), genomeId);
  assert.equal(await client.updateGenome(genomeId, card), genomeId);
  assert.deepEqual(calls, [
    { url: 'http://eros:8080/svc/v1/genomes', method: 'POST', body: { id: genomeId, card } },
    { url: `http://eros:8080/svc/v1/genomes/${genomeId}`, method: 'PUT', body: { card } },
  ]);
});

test('stock genome write preserves 400 and 403 rejection statuses', async () => {
  const genomeId = erosGenomeIdentityFor('creator_cuid_1');
  const base = {
    NODE_ENV: 'test', EROS_ENABLED: 'true', EROS_SERVICE_URL: 'http://eros:8080',
    EROS_JWT_SECRET: 'unit-test-eros-jwt-secret-at-least-32-bytes',
    EROS_GENOME_WRITE_SECRET: 'unit-test-genome-write-secret-at-least-32-bytes',
  };
  for (const status of [400, 403]) {
    const client = createErosEngineClient(base, async () => new Response(null, { status }))!;
    await assert.rejects(client.createGenome(genomeId, erosGenomeCardFor('Creator')),
      (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_genome_write_failed' && error.status === status);
  }
});

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
    EROS_PRODUCTION_TEST_USER_IDS: '10000000-0000-4000-8000-000000000001',
    EROS_PRODUCTION_TEST_CREATOR_IDS: '20000000-0000-4000-8000-000000000002',
  };
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', production), true);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001-extra', '20000000-0000-4000-8000-000000000002', production), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000003', production), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...production, EROS_PRODUCTION_TEST_MODE: 'false' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...production, EROS_DATA_MODE: 'shared' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...production, EROS_PRODUCTION_TEST_USER_IDS: '' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...production, EROS_PRODUCTION_TEST_CREATOR_IDS: '' }), false);
  assert.deepEqual(erosTestCreatorIds(production), ['20000000-0000-4000-8000-000000000002']);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...production, EROS_PRODUCTION_TEST_USER_IDS: '10000000-0000-4000-8000-000000000001,10000000-0000-4000-8000-000000000003' }), false, 'expanded allowlist blocks the entire pilot');
  assert.equal(isSyntheticErosPair(
    { telegramUsername: 'eros_test_pilot' },
    { slug: 'eros-test-pilot', profileStatus: 'APPROVED' },
  ), true);
  assert.equal(isSyntheticErosPair(
    { telegramUsername: 'real_user' },
    { slug: 'eros-test-pilot', profileStatus: 'APPROVED' },
  ), false);
});

test('production GPoint-owned routing admits any valid GPoint identity only with an exact tenant and enabled Eros', () => {
  const owned = {
    NODE_ENV: 'production',
    EROS_ENABLED: 'true',
    EROS_DATA_MODE: 'gpoint-owned',
    GPOINT_CHAT_POLICY_TENANT_ID: '30000000-0000-4000-8000-000000000003',
  };
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', owned), true);
  assert.equal(isErosChatAllowed('invalid user id', '20000000-0000-4000-8000-000000000002', owned), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', 'bad creator', owned), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...owned, EROS_ENABLED: 'false' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...owned, GPOINT_CHAT_POLICY_TENANT_ID: '' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...owned, EROS_DATA_MODE: 'synthetic' }), false);
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

test('relationship preparation fails closed when persona has not been provisioned through stock Eros API', async () => {
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
    EROS_TEST_USER_IDS: '10000000-0000-4000-8000-000000000001',
    EROS_TEST_CREATOR_IDS: '20000000-0000-4000-8000-000000000002',
    DATABASE_URL: 'postgresql://local:test@127.0.0.1:5432/gpoint_test',
  };

  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', enabled), true);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001x', '20000000-0000-4000-8000-000000000002', enabled), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000003', enabled), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, NODE_ENV: 'production' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, EROS_ENABLED: 'false' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, EROS_DATA_MODE: 'shared' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, DATABASE_URL: 'postgresql://host/gpoint_test' }), false);
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, EROS_ADAPTER_DATABASE_URL: 'postgresql://untrusted.example/eros' }), true,
    'the GPoint API no longer opens a separate connection to the Eros database');
  assert.equal(isErosChatAllowed('10000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000002', { ...enabled, NODE_ENV: undefined }), false);
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
  assert.equal('memory_scope' in calls[1].body, false, 'Eros controls the memory scope');
  assert.deepEqual(calls[1].body.prompt_traits, GPOINT_EROS_RESPONSE_PROMPT_TRAITS);
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

test('Eros maps a timeout while reading a response stream to a gateway timeout', async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: 'meta', message_id: '01JABCDEF0123456789ABCDEFGH', action_type: 'reply' })}\\n\\n`));
      controller.error(new DOMException('synthetic stream deadline', 'TimeoutError'));
    },
  });
  const fetchImpl: typeof fetch = async () => new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
  const client = new ErosEngineClient({ baseUrl: 'http://eros:8080', jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes', ...bridgeTestOptions, fetchImpl });

  await assert.rejects(
    client.sendMessage('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 'hello', bridgeTestIdentity),
    (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_timeout' && error.status === 504,
  );
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
  const before = '01a1630d-4127-0726-2b87-9b2624548592';
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

function mockErosMessageStateStore(initial: Array<{ userId: string; creatorId: string; sessionId: string; messageId: string; readAt: Date | null }> = []) {
  const records = [...initial];
  const key = (row: { userId: string; sessionId: string; messageId: string }) => `${row.userId}/${row.sessionId}/${row.messageId}`;
  return {
    records,
    erosAssistantMessageState: {
      async createMany({ data }: { data: typeof records; skipDuplicates: boolean }) {
        let count = 0;
        for (const row of data) {
          if (records.some(existing => key(existing) === key(row))) continue;
          records.push({ ...row }); count++;
        }
        return { count };
      },
      async findMany({ where }: { where: { userId: string; creatorId: string; sessionId: string; messageId?: { in: string[] }; readAt?: null } }) {
        return records.filter(row => row.userId === where.userId && row.creatorId === where.creatorId && row.sessionId === where.sessionId &&
          (!where.messageId || where.messageId.in.includes(row.messageId)) && (where.readAt !== null || row.readAt === null))
          .map(({ messageId, readAt }) => ({ messageId, readAt }));
      },
      async updateMany({ where, data }: { where: { userId: string; creatorId: string; sessionId: string; messageId: { in: string[] }; readAt: null }; data: { readAt: Date } }) {
        let count = 0;
        for (const row of records) if (row.userId === where.userId && row.creatorId === where.creatorId && row.sessionId === where.sessionId && where.messageId.in.includes(row.messageId) && row.readAt === null) { row.readAt = data.readAt; count++; }
        return { count };
      },
      async groupBy({ where }: { where: { userId: string; readAt: null; OR: Array<{ creatorId: string; sessionId: string }> } }) {
        const counts = new Map<string, number>();
        for (const row of records) if (row.userId === where.userId && where.OR.some((scope) => scope.creatorId === row.creatorId && scope.sessionId === row.sessionId) && row.readAt === null) counts.set(row.sessionId, (counts.get(row.sessionId) ?? 0) + 1);
        return [...counts].map(([sessionId, count]) => ({ creatorId: where.OR.find((scope) => scope.sessionId === sessionId)!.creatorId, sessionId, _count: { _all: count } }));
      },
    },
  };
}

test('assistant message state stores only IDs and read timestamps in GPoint-owned storage', async () => {
  const db = mockErosMessageStateStore();
  const userId = 'user_a'; const creatorId = 'creator_a'; const sessionId = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1';
  await recordErosAssistantMessage(db, userId, creatorId, sessionId, 'reply_01');
  await syncErosAssistantMessageStates(db, userId, creatorId, sessionId, [
    { id: 'reply_02', role: 'assistant', content: 'must not be stored', sent_at: '2026-10-22T08:00:00.000Z' },
    { id: 'user_01', role: 'user', content: 'must not be indexed', sent_at: '2026-10-22T08:00:00.000Z' },
  ]);
  assert.deepEqual(db.records.map(({ messageId }) => messageId).sort(), ['reply_01', 'reply_02']);
  assert.equal(JSON.stringify(db.records).includes('must not be stored'), false);
  assert.equal(JSON.stringify(db.records).includes('must not be indexed'), false);
});

test('message-level read acknowledgement requires exact IDs previously observed for that user, creator and session', async () => {
  const db = mockErosMessageStateStore([
    { userId: 'user_a', creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', messageId: 'reply_a', readAt: null },
    { userId: 'user_b', creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', messageId: 'reply_b', readAt: null },
  ]);
  const ids = ['reply_a'];
  assert.deepEqual(await markErosAssistantMessagesRead(db, 'user_a', 'creator_a', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', ids), ids);
  await assert.rejects(markErosAssistantMessagesRead(db, 'user_a', 'creator_a', '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', ['reply_b']), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_read_messages_not_owned');
  assert.equal(db.records[0].readAt instanceof Date, true);
  assert.equal(db.records[1].readAt, null);
});

test('unread counts include only unacknowledged assistant IDs scoped by GPoint ownership', async () => {
  const db = mockErosMessageStateStore([
    { userId: 'user_a', creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', messageId: 'reply_a', readAt: null },
    { userId: 'user_a', creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', messageId: 'reply_b', readAt: new Date() },
    { userId: 'user_b', creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', messageId: 'reply_c', readAt: null },
    { userId: 'user_a', creatorId: 'creator_b', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', messageId: 'reply_d', readAt: null },
  ]);
  const counts = await unreadErosAssistantCounts(db, 'user_a', [
    { creatorId: 'creator_a', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1' },
    { creatorId: 'creator_b', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2' },
    { creatorId: 'creator_c', sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3' },
  ]);
  assert.deepEqual([...counts], [
    ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1', 1],
    ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2', 1],
    ['7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3', 0],
  ]);
});

test('system_error previews are normalized and never exposed as assistant-authored content', () => {
  assert.deepEqual(erosInboxLastMessageSummary({ role: 'system_error', content: 'private internal upstream detail' }), {
    lastMessagePreview: 'Сообщение не удалось обработать. Попробуйте ещё раз.',
    lastMessageRole: 'system',
  });
});

test('Eros chat cursors preserve stock opaque message IDs and reject malformed values', () => {
  const id = '01a1630d-4127-0726-2b87-9b2624548592';
  assert.equal(encodeErosCursor(id), `eros-before:${id}`);
  assert.equal(decodeErosCursor(`eros-before:${id}`), id);
  assert.equal(encodeErosCursor('reply_01'), 'eros-before:reply_01');
  assert.equal(decodeErosCursor('eros-before:reply_01'), 'reply_01');
  assert.equal(decodeErosCursor(undefined), undefined);
  for (const value of ['50', 'eros-offset:50', 'eros-before:', 'eros-before:bad%2Fid', 'other:50']) {
    assert.throws(() => decodeErosCursor(value), (error: unknown) => error instanceof ErosAdapterError && error.code === 'eros_cursor_invalid');
  }
});

test('world enrollment requires the narrow private writer connection, not Core or runtime credentials', () => {
  assert.equal(createErosWorldEnrollment({}), undefined);
  assert.throws(() => createErosWorldEnrollment({ EROS_WORLD_ENROLLMENT_REQUIRED: 'true' }), /eros_world_database_missing/);
  assert.equal(new URL(worldEnrollmentConnection('postgresql://eros_world_writer:test@gpoint-eros-db:5432/eros')).searchParams.get('connection_limit'), '2');
  for (const dsn of [
    'postgresql://eros_runtime:test@gpoint-eros-db:5432/eros',
    'postgresql://eros_world_writer:test@evil.example:5432/eros',
    'postgresql://eros_world_writer:test@gpoint-eros-db:5432/gpoint',
    'postgresql://eros_world_writer:test@gpoint-eros-db:5432/eros?host=evil.example',
  ]) assert.throws(() => worldEnrollmentConnection(dsn), /eros_world_database_invalid/);
});

test('world enrollment runs before a validated message and never on session start or rejected input', async () => {
  const owner = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc0';
  const session = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1';
  const instance = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2';
  const message = '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3';
  const events: string[] = [];
  let failEnrollment = false;
  const client = new ErosEngineClient({
    baseUrl: 'http://eros:8080',
    jwtSecret: 'unit-test-eros-jwt-secret-at-least-32-bytes',
    workerSigningSecret: 'unit-test-worker-signing-secret-at-least-32-bytes',
    workerTenantId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc5',
    worldEnrollment: async (...ids) => {
      assert.deepEqual(ids, [owner, session, instance]);
      events.push('enroll');
      if (failEnrollment) throw new Error('private database error');
    },
    fetchImpl: async input => {
      if (String(input).endsWith('/comp/chat/start')) {
        events.push('start');
        return Response.json({ session_id: session, instance_id: instance });
      }
      events.push('send');
      return new Response([
        'data: '+JSON.stringify({ type: 'meta', message_id: message, action_type: 'reply' }),
        'data: '+JSON.stringify({ type: 'delta', content: 'Hello!' }),
        'data: '+JSON.stringify({ type: 'done', ghost_fallback: false }),
        'data: '+JSON.stringify({ type: 'final' }),
      ].join('\n\n')+'\n\n');
    },
  });
  await client.startSession(owner, instance);
  assert.deepEqual(events, ['start']);
  await assert.rejects(client.sendMessage(owner, session, message, '', {creatorId: instance, instanceId: instance}));
  assert.deepEqual(events, ['start']);
  await client.sendMessage(owner, session, message, 'Hello', {creatorId: instance, instanceId: instance});
  assert.deepEqual(events, ['start','enroll','send']);
  failEnrollment = true;
  await assert.rejects(client.sendMessage(owner, session, message, 'Hello', {creatorId: instance, instanceId: instance}), /eros_world_enrollment_failed/);
  assert.deepEqual(events, ['start','enroll','send','enroll']);
});

test('hosted disposable PostgreSQL verifies actual application enrollment, isolation, race and ACL', {
  skip: process.env.GITHUB_ACTIONS !== 'true',
  timeout: 90_000,
}, async () => {
  // GitHub-hosted service only, never a process-supplied or production DSN.
  const dbName = 'gpoint_world_enrollment_test';
  const conn = ['-h','127.0.0.1','-p','5432','-U','gpoint'];
  const env = { ...process.env, PGPASSWORD: 'gpoint_dev_only' };
  for (const key of ['PGSERVICE','PGSERVICEFILE','PGOPTIONS','PGDATABASE','PGHOST','PGHOSTADDR','PGPORT','PGUSER']) delete env[key as keyof typeof env];
  const psql = (database: string, sql: string) => execFileSync('psql', ['-X',...conn,'-d',database,'-v','ON_ERROR_STOP=1'], {env,input:sql,encoding:'utf8',timeout:15_000});
  execFileSync('createdb', [...conn,dbName], {env,timeout:15_000});
  let admin: PrismaClient | undefined;
  let writer: PrismaClient | undefined;
  let roleCreated = false;
  try {
    psql(dbName, `
      CREATE SCHEMA engine;
      CREATE TABLE engine.persona_instances(id uuid PRIMARY KEY, genome_id uuid, owner_uid uuid NOT NULL, status text NOT NULL);
      CREATE TABLE engine.chat_sessions(id uuid PRIMARY KEY, user_id uuid NOT NULL, instance_id uuid, channel text NOT NULL, archived boolean NOT NULL DEFAULT false);
      CREATE TABLE engine.world_enrollments(owner_uid uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), town_enabled boolean NOT NULL DEFAULT false, stories_enabled boolean NOT NULL DEFAULT false);
      CREATE TABLE engine.world_worldviews(owner_uid uuid PRIMARY KEY, content text NOT NULL CHECK(char_length(content) BETWEEN 1 AND 10000), updated_at timestamptz NOT NULL DEFAULT now());
      CREATE TABLE engine.chat_messages(id uuid PRIMARY KEY, content text);
      ALTER TABLE engine.persona_instances ENABLE ROW LEVEL SECURITY;
      ALTER TABLE engine.chat_sessions ENABLE ROW LEVEL SECURITY;
      ALTER TABLE engine.world_enrollments ENABLE ROW LEVEL SECURITY;
      ALTER TABLE engine.world_worldviews ENABLE ROW LEVEL SECURITY;
      REVOKE CONNECT ON DATABASE gpoint_world_enrollment_test FROM PUBLIC;
    `);
    const acl = readFileSync(new URL('../../../services/eros-engine/world-enrollment/writer-acl.sql', import.meta.url), 'utf8')
      .replace("current_database() <> 'eros'", "current_database() <> 'gpoint_world_enrollment_test'")
      .replace("GRANT CONNECT ON DATABASE eros TO", "GRANT CONNECT ON DATABASE gpoint_world_enrollment_test TO");
    psql(dbName, acl);
    roleCreated = true;
    psql(dbName, "ALTER ROLE eros_world_writer LOGIN PASSWORD 'fixture-only-writer';");
    admin = new PrismaClient({ datasourceUrl: 'postgresql://gpoint:gpoint_dev_only@127.0.0.1:5432/'+dbName });
    writer = new PrismaClient({ datasourceUrl: 'postgresql://eros_world_writer:fixture-only-writer@127.0.0.1:5432/'+dbName+'?connection_limit=2' });
    const ids = (n: number) => ({
      owner: '10000000-0000-4000-8000-'+String(n).padStart(12,'0'),
      session: '20000000-0000-4000-8000-'+String(n).padStart(12,'0'),
      instance: '30000000-0000-4000-8000-'+String(n).padStart(12,'0'),
    });
    const [a,b,c,d] = [1,2,3,4].map(ids);
    for (const v of [a,b,c,d]) {
      const genome = v === d ? null : '40000000-0000-4000-8000-000000000001';
      await admin.$executeRaw`INSERT INTO engine.persona_instances VALUES (${v.instance}::uuid,${genome}::uuid,${v.owner}::uuid,'active')`;
      await admin.$executeRaw`INSERT INTO engine.chat_sessions VALUES (${v.session}::uuid,${v.owner}::uuid,${v.instance}::uuid,'text',${v === c})`;
    }
    await admin.$executeRaw`INSERT INTO engine.world_worldviews(owner_uid,content,updated_at) VALUES (${b.owner}::uuid,'Existing world','2020-01-01T00:00:00Z')`;
    await admin.$executeRaw`INSERT INTO engine.world_enrollments(owner_uid) VALUES (${b.owner}::uuid)`;
    const ensure = (v: typeof a) => enrollErosWorld(writer!,v.owner,v.session,v.instance);
    await Promise.all([ensure(a),ensure(a)]);
    await ensure(b);
    await ensure(a);
    await assert.rejects(ensure(c), /eros_world_relationship_invalid/);
    await assert.rejects(ensure(d), /eros_world_relationship_invalid/);
    await assert.rejects(enrollErosWorld(writer,a.owner,b.session,b.instance), /eros_world_relationship_invalid/);
    const rows = await admin.$queryRaw<Array<{owner_uid:string,town_enabled:boolean,stories_enabled:boolean,content:string,updated_at:Date}>>`
      SELECT e.owner_uid,e.town_enabled,e.stories_enabled,w.content,w.updated_at
      FROM engine.world_enrollments e JOIN engine.world_worldviews w USING(owner_uid) ORDER BY e.owner_uid
    `;
    assert.equal(rows.length,2);
    assert.deepEqual(rows.map(x=>x.owner_uid),[a.owner,b.owner]);
    assert.ok(rows.every(x=>x.town_enabled && x.stories_enabled));
    assert.equal(rows[1].content,'Existing world');
    assert.equal(rows[1].updated_at.toISOString(),'2020-01-01T00:00:00.000Z');
    await assert.rejects(writer.$queryRaw`SELECT content FROM engine.chat_messages`);
    await assert.rejects(writer.$executeRaw`DELETE FROM engine.world_enrollments`);
    await assert.rejects(writer.$executeRaw`UPDATE engine.persona_instances SET status='inactive'`);
  } finally {
    await writer?.$disconnect();
    await admin?.$disconnect();
    execFileSync('dropdb',[...conn,dbName],{env,timeout:15_000});
    if (roleCreated) psql('gpoint','DROP ROLE eros_world_writer;');
  }
});
