import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { SignJWT } from 'jose';
import { buildChatCompletionSse, buildOpenAiEmbeddingResponse, parseBridgeRequest, parseErosEmbeddingRequest, verifyBridgeIdentity } from '../src/eros-worker-contract.js';

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
  return new SignJWT({
    tenant_id: '81a4ba28-6de5-4b03-8332-0661ba4d46f4',
    creator_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4',
    session_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1',
    instance_id: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9',
    ...overrides,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuer('eros-engine')
    .setAudience('gpoint-worker-gateway')
    .setSubject('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2')
    .setJti('7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3')
    .setIssuedAt(now)
    .setExpirationTime(now + 60)
    .sign(secret);
}

test('bridge accepts only a signed, short-lived Eros identity bound to its chat request', async () => {
  const token = await identity();
  const claims = await verifyBridgeIdentity(token, secret, now);
  assert.equal(claims.tenantId, '81a4ba28-6de5-4b03-8332-0661ba4d46f4');
  assert.equal(claims.userId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2');
  assert.equal(claims.creatorId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc4');
  assert.equal(claims.sessionId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1');
  assert.equal(claims.instanceId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc9');
  assert.equal(claims.requestId, '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc3');
  await assert.rejects(verifyBridgeIdentity(`${token.slice(0, -1)}x`, secret, now));
  await assert.rejects(verifyBridgeIdentity(token, new TextEncoder().encode('wrong-secret-wrong-secret-wrong-secret'), now));
  await assert.rejects(verifyBridgeIdentity(await identity({ session_id: 'not-a-uuid' }), secret, now));
  await assert.rejects(verifyBridgeIdentity(await identity({ creator_id: 'not-a-uuid' }), secret, now));
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

test('embedding proxy contract requires exact Qwen model and native 512-dimensional vectors', () => {
  const request = parseErosEmbeddingRequest({
    model: 'qwen3-embedding:0.6b', input: ['Привет', 'память'], dimensions: 512,
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
