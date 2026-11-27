import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { HttpException } from '@nestjs/common';
import { ErosWorkerGatewayController } from '../src/eros-worker-gateway.js';
import { buildChatCompletionSse, buildOpenAiEmbeddingResponse, deriveBridgeInferenceIdentity, parseBridgeRequest, parseErosEmbeddingRequest, signBridgeIdentity, verifyBridgeIdentity, workerChatCompletionRequest } from '../src/eros-worker-contract.js';

const secret = new TextEncoder().encode('local-test-bridge-signing-secret-at-least-32-bytes');
const now = Math.floor(Date.now() / 1000);

test('Eros queue migration forces tenant RLS and binds access to transaction-local context', () => {
  const sql = readFileSync(new URL('../../../packages/db/prisma/migrations/20261016120000_eros_worker_queue/migration.sql', import.meta.url), 'utf8');
  assert.match(sql, /ALTER TABLE engine\.eros_inference_jobs ENABLE ROW LEVEL SECURITY/);
  assert.match(sql, /ALTER TABLE engine\.eros_inference_jobs FORCE ROW LEVEL SECURITY/);
  assert.match(sql, /tenant_id = NULLIF\(current_setting\('app\.tenant_id', true\), ''\)/);
  assert.match(sql, /WITH CHECK \(tenant_id = NULLIF\(current_setting\('app\.tenant_id', true\), ''\)\)/);
});

async function identity(overrides: Record<string, unknown> = {}) {
  return signBridgeIdentity({
    tenantId: '81a4ba28-6de5-4b03-8332-0661ba4d46f4',
    creatorId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4',
    sessionId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1',
    instanceId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9',
    userId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2',
    requestId: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3',
    ...overrides,
  } as Parameters<typeof signBridgeIdentity>[0], secret, now);
}

test('bridge accepts only a signed, short-lived Eros identity bound to its chat request', async () => {
  const token = await identity();
  assert.ok(token.length <= 256, 'stock Eros rejects audit.user values longer than 256 characters');
  const claims = await verifyBridgeIdentity(token, secret, now);
  assert.equal(claims.tenantId, '81a4ba28-6de5-4b03-8332-0661ba4d46f4');
  assert.equal(claims.userId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2');
  assert.equal(claims.creatorId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4');
  assert.equal(claims.sessionId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1');
  assert.equal(claims.instanceId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9');
  assert.equal(claims.requestId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3');
  const [header, payload, signature] = token.split('.');
  assert.ok(signature?.length);
  const corruptedSignature = `${signature[0] === 'A' ? 'B' : 'A'}${signature.slice(1)}`;
  await assert.rejects(verifyBridgeIdentity(`${header}.${payload}.${corruptedSignature}`, secret, now));
  await assert.rejects(verifyBridgeIdentity(token, new TextEncoder().encode('wrong-secret-wrong-secret-wrong-secret'), now));
  assert.throws(() => signBridgeIdentity({
    tenantId: claims.tenantId, userId: claims.userId, creatorId: 'not-a-uuid',
    sessionId: claims.sessionId, instanceId: claims.instanceId, requestId: claims.requestId,
  }, secret, now), /eros_bridge_identity_invalid/);
  await assert.rejects(verifyBridgeIdentity(token, secret, now + 121), /eros_bridge_identity_invalid/);
});

test('one signed chat turn derives stable per-inference IDs for Eros internal calls', async () => {
  const claims = await verifyBridgeIdentity(await identity(), secret, now);
  const chat = deriveBridgeInferenceIdentity(claims, 'chat-fingerprint');
  const extraction = deriveBridgeInferenceIdentity(claims, 'memory-extraction-fingerprint');
  const retry = deriveBridgeInferenceIdentity(claims, 'chat-fingerprint');

  assert.match(chat.requestId, /^[0-9a-f-]{36}$/i);
  assert.notEqual(chat.requestId, claims.requestId);
  assert.notEqual(extraction.requestId, chat.requestId);
  assert.equal(retry.requestId, chat.requestId);
  assert.equal(chat.userId, claims.userId);
  assert.equal(chat.sessionId, claims.sessionId);
  assert.equal(chat.instanceId, claims.instanceId);
});

test('chat request validation uses signed identity and bounds the OpenAI-compatible payload', async () => {
  const token = await identity();
  const parsed = await parseBridgeRequest({
    model: 'gpoint-codex',
    user: token,
    messages: [{ role: 'system', content: 'Be Mira.' }, { role: 'user', content: 'Привет.' }],
    max_tokens: 512,
    stream: true,
  }, secret, now);
  assert.equal(parsed.identity?.userId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2');
  assert.deepEqual(parsed.messages.map((message) => message.role), ['system', 'user']);
  const internal = await parseBridgeRequest({
    model: 'gpoint-codex', messages: [{ role: 'system', content: 'Extract a fact.' }, { role: 'user', content: 'Синтетический факт.' }],
  }, secret, now);
  assert.equal(internal.identity, undefined);
  assert.equal(internal.stream, false, 'omitted stream uses the OpenAI non-streaming default');
  const systemTask = await parseBridgeRequest({
    model: 'gpoint-codex', user: '11111111-1111-1111-1111-111111111111',
    messages: [{ role: 'system', content: 'Extract a synthetic memory fact.' }],
  }, secret, now);
  assert.equal(systemTask.identity, undefined, 'stock Eros system audit marker does not claim a user identity');
  await assert.rejects(parseBridgeRequest({
    model: 'gpoint-codex', user: token,
    user_id: 'victim', creator_id: 'another-character',
    messages: [{ role: 'user', content: 'hello' }], max_tokens: 32,
  }, secret, now), /eros_chat_request_invalid/);
  await assert.rejects(parseBridgeRequest({
    model: 'gpoint-codex', user: token,
    messages: [{ role: 'tool', content: 'not accepted' }], max_tokens: 32,
  }, secret, now), /eros_chat_request_invalid/);
  await assert.rejects(parseBridgeRequest({
    model: 'gpoint-codex', user: token,
    messages: [{ role: 'user', content: 'x'.repeat(20_001) }], max_tokens: 32,
  }, secret, now), /eros_chat_request_invalid/);
});

test('GPoint Eros chat receives a scoped English dialogue adapter after stock Eros system context', async () => {
  const token = await identity();
  const stockSystem = 'Synthetic persona context. [reply_length]最多 1 句，不超过 40 字';
  const userText = 'Could you suggest a detailed rainy afternoon plan?';
  const parsed = await parseBridgeRequest({
    model: 'gpoint-codex',
    user: token,
    messages: [{ role: 'system', content: stockSystem }, { role: 'user', content: userText }],
    max_tokens: 512,
    temperature: 1,
    stream: true,
  }, secret, now);

  const workerRequest = workerChatCompletionRequest(parsed);
  assert.deepEqual(workerRequest.messages[0], { role: 'system', content: stockSystem });
  assert.equal(workerRequest.messages[1].role, 'system', 'the adapter is a system instruction, not dialogue');
  assert.equal(workerRequest.messages[2].role, 'user');
  assert.equal(workerRequest.messages[2].content, userText, 'the user request remains unchanged and in order');
  const adapter = workerRequest.messages[1].content;
  assert.match(adapter, /answer the user's actual question or request directly/i);
  assert.match(adapter, /proportionate to the request/i);
  assert.match(adapter, /fixed (character|sentence) (ceiling|limit)/i);
  assert.match(adapter, /language of the latest user message/i);
  assert.match(adapter, /character.*mood.*relationship.*feelings/is);
  assert.doesNotMatch(adapter, /memories|memory/i, 'GPoint must not direct memory use');
  assert.match(adapter, /age.*consent.*safety/is);
  assert.match(adapter, /internal.*prompt|prompt.*internal/is);

  assert.equal(workerRequest.model, 'gpoint-codex');
  assert.equal(workerRequest.max_tokens, 512);
  assert.equal(workerRequest.temperature, 1);
  assert.equal('user' in workerRequest, false);
  assert.equal('stream' in workerRequest, false);
  assert.equal(JSON.stringify(workerRequest).includes(token), false);
});

test('GPoint dialogue adapter does not alter stock Eros system jobs', async () => {
  const internal = await parseBridgeRequest({
    model: 'gpoint-codex',
    messages: [{ role: 'system', content: 'Synthetic memory extraction instructions.' }, { role: 'user', content: 'Synthetic fact.' }],
    max_tokens: 128,
  }, secret, now);

  const workerRequest = workerChatCompletionRequest(internal);
  assert.deepEqual(workerRequest.messages, internal.messages);
});

test('streaming completion emits standard OpenAI SSE deltas and one terminal marker', () => {
  const frames = buildChatCompletionSse({ id: 'job-safe-id', model: 'gpoint-codex', text: 'Привет, рада тебя видеть.' });
  const data = frames.split('\n\n').filter((frame) => frame.startsWith('data: ')).map((frame) => frame.slice(6));
  assert.equal(data.at(-1), '[DONE]');
  const chunks = data.slice(0, -1).map((frame) => JSON.parse(frame));
  assert.equal(chunks[0].object, 'chat.completion.chunk');
  assert.equal(chunks[0].choices[0].delta.content, 'Привет, рада тебя видеть.');
  assert.equal(chunks.at(-1).choices[0].finish_reason, 'stop');
  assert.ok(chunks.every((chunk) => chunk.id === 'job-safe-id' && chunk.model === 'gpoint-codex'));
});

test('chat request rejects sampling temperatures the Mac Worker cannot honor', async () => {
  await assert.rejects(parseBridgeRequest({
    model: 'gpoint-codex', messages: [{ role: 'user', content: 'hello' }], temperature: 0.4,
  }, secret, now), /eros_chat_request_invalid/);
});

test('synthetic embedding contract requires its exact fixture model and 512-dimensional vectors', () => {
  const request = parseErosEmbeddingRequest({
    model: 'gpoint-test-embedding', input: ['Привет', 'память'], dimensions: 512,
  });
  assert.equal(request.input.length, 2);
  assert.equal(request.dimensions, 512);
  assert.throws(() => parseErosEmbeddingRequest({ ...request, dimensions: 1024 }));
  assert.throws(() => parseErosEmbeddingRequest({ ...request, model: 'other-model' }));
  assert.throws(() => parseErosEmbeddingRequest({ ...request, extra: 'untrusted' }));
  const output = buildOpenAiEmbeddingResponse(request.model, [Array(512).fill(0.25), Array(512).fill(-0.25)]);
  assert.equal(output.data.length, 2);
  assert.equal(output.data[0].embedding.length, 512);
  assert.equal(output.data[1].index, 1);
  assert.throws(() => buildOpenAiEmbeddingResponse(request.model, [Array(1024).fill(0)]));
  assert.throws(() => buildOpenAiEmbeddingResponse(request.model, [Array(512).fill(Number.NaN)]));
});

test('Core embeddings call only the disposable loopback synthetic provider', async () => {
  const env = {
    nodeEnv: process.env.NODE_ENV,
    dataMode: process.env.EROS_DATA_MODE,
    localTest: process.env.EROS_WORKER_LOCAL_TEST,
    disposableDb: process.env.EROS_WORKER_DISPOSABLE_TEST_DB,
    key: process.env.EROS_TEST_EMBEDDING_API_KEY,
    url: process.env.EROS_TEST_EMBEDDING_URL,
  };
  const originalFetch = globalThis.fetch;
  let calledUrl = '';
  let calledInit: RequestInit | undefined;
  process.env.NODE_ENV = 'development';
  process.env.EROS_DATA_MODE = 'synthetic';
  process.env.EROS_WORKER_LOCAL_TEST = 'true';
  process.env.EROS_WORKER_DISPOSABLE_TEST_DB = '1';
  process.env.EROS_TEST_EMBEDDING_API_KEY = 'local-embedding-test-key';
  process.env.EROS_TEST_EMBEDDING_URL = 'http://127.0.0.1:11434/api/embed';
  globalThis.fetch = async (input, init) => {
    calledUrl = String(input);
    calledInit = init;
    return new Response(JSON.stringify({ embeddings: [Array.from({ length: 512 }, (_, index) => index / 512)] }), { status: 200 });
  };
  try {
    const controller = Object.create(ErosWorkerGatewayController.prototype) as ErosWorkerGatewayController;
    const result = await controller.embeddings(
      { authorization: 'Bearer local-embedding-test-key' },
      { model: 'gpoint-test-embedding', input: 'private network embedding', dimensions: 512 },
    );
    assert.equal(calledUrl, 'http://127.0.0.1:11434/api/embed');
    assert.equal(calledInit?.method, 'POST');
    assert.equal(calledInit?.redirect, 'error');
    assert.deepEqual(JSON.parse(String(calledInit?.body)), {
      input: ['private network embedding'], dimensions: 512, truncate: false,
    });
    assert.equal((result as { data: { embedding: number[] }[] }).data[0].embedding.length, 512);
  } finally {
    globalThis.fetch = originalFetch;
    for (const [key, value] of Object.entries({
      NODE_ENV: env.nodeEnv,
      EROS_DATA_MODE: env.dataMode,
      EROS_WORKER_LOCAL_TEST: env.localTest,
      EROS_WORKER_DISPOSABLE_TEST_DB: env.disposableDb,
      EROS_TEST_EMBEDDING_API_KEY: env.key,
      EROS_TEST_EMBEDDING_URL: env.url,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test('production mode rejects synthetic embeddings before making a network request', async () => {
  const env = {
    nodeEnv: process.env.NODE_ENV,
    dataMode: process.env.EROS_DATA_MODE,
    localTest: process.env.EROS_WORKER_LOCAL_TEST,
    disposableDb: process.env.EROS_WORKER_DISPOSABLE_TEST_DB,
    key: process.env.EROS_TEST_EMBEDDING_API_KEY,
    url: process.env.EROS_TEST_EMBEDDING_URL,
  };
  const originalFetch = globalThis.fetch;
  let calls = 0;
  process.env.NODE_ENV = 'production';
  process.env.EROS_DATA_MODE = 'gpoint-owned';
  process.env.EROS_WORKER_LOCAL_TEST = 'false';
  process.env.EROS_WORKER_DISPOSABLE_TEST_DB = '0';
  process.env.EROS_TEST_EMBEDDING_API_KEY = 'synthetic-only-key';
  process.env.EROS_TEST_EMBEDDING_URL = 'http://127.0.0.1:11434/api/embed';
  globalThis.fetch = async () => {
    calls += 1;
    return new Response('{}', { status: 200 });
  };
  try {
    const controller = Object.create(ErosWorkerGatewayController.prototype) as ErosWorkerGatewayController;
    await assert.rejects(
      controller.embeddings(
        { authorization: 'Bearer synthetic-only-key' },
        { model: 'gpoint-test-embedding', input: 'production must not use test vectors', dimensions: 512 },
      ),
      (error: unknown) => error instanceof HttpException && error.getStatus() === 503,
    );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    for (const [key, value] of Object.entries({
      NODE_ENV: env.nodeEnv,
      EROS_DATA_MODE: env.dataMode,
      EROS_WORKER_LOCAL_TEST: env.localTest,
      EROS_WORKER_DISPOSABLE_TEST_DB: env.disposableDb,
      EROS_TEST_EMBEDDING_API_KEY: env.key,
      EROS_TEST_EMBEDDING_URL: env.url,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
