import assert from 'node:assert/strict';
import test from 'node:test';
import { HttpException } from '@nestjs/common';
import type { AgataClient } from '@gpoint/agata-client';
import { createAgataIntegration, AgataIntegrationService } from '../src/agata-integration.js';
import { AgataWorkerGatewayController, assertWorkerAuth } from '../src/agata-worker-gateway.js';

const input = {
  userId: 'gpoint-user-1',
  characterId: 'character-1',
  requestId: 'request-1',
  messageId: 'message-stable-id',
  text: 'test message',
  profile: { name: 'Anna' },
  consent: {
    chat_history: true,
    long_term_memory: false,
    age_band: 'adult' as const,
    age_status: 'self_attested' as const,
    age_method: 'test',
    jurisdiction: 'US',
    disclosure_accepted: true,
    disclosure_receipt_id: 'receipt-1',
  },
};

test('AGATA boundary is disabled by default and ignores absent credentials', () => {
  assert.equal(createAgataIntegration({}), null);
  assert.equal(createAgataIntegration({ AGATA_ENABLED: 'false' }), null);
});

test('Codex worker gateway accepts only the dedicated bearer token', () => {
  const env = { GPOINT_AGATA_WORKER_TOKEN: 'dedicated-test-token' };
  assert.doesNotThrow(() => assertWorkerAuth({ authorization: 'Bearer dedicated-test-token' }, env));
  assert.throws(() => assertWorkerAuth({}, env), { status: 401 });
  assert.throws(() => assertWorkerAuth({ authorization: 'Bearer wrong-token' }, env), { status: 401 });
});

test('queue status is a registered authenticated GET and exposes only the content-free GPOINT snapshot', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(AgataWorkerGatewayController.prototype, 'queueStatus');
  assert.ok(descriptor?.value);
  assert.equal(Reflect.getMetadata('path', descriptor.value), 'queue-status');
  assert.equal(Reflect.getMetadata('method', descriptor.value), 0);

  const previous = {
    workerToken: process.env.GPOINT_AGATA_WORKER_TOKEN,
    enabled: process.env.AGATA_ENABLED,
    serviceUrl: process.env.AGATA_SERVICE_URL,
    internalToken: process.env.AGATA_INTERNAL_API_TOKEN,
    fetch: globalThis.fetch,
  };
  const requests: Array<{ url: string; method: string | undefined; authorization: string | null; body: string | null }> = [];
  try {
    process.env.GPOINT_AGATA_WORKER_TOKEN = 'worker-gateway-test-token';
    process.env.AGATA_ENABLED = 'true';
    process.env.AGATA_SERVICE_URL = 'https://agata.internal';
    process.env.AGATA_INTERNAL_API_TOKEN = 'internal-service-test-token';
    globalThis.fetch = async (input, init) => {
      requests.push({
        url: String(input), method: init?.method,
        authorization: new Headers(init?.headers).get('authorization'),
        body: init?.body ? String(init.body) : null,
      });
      return new Response(JSON.stringify({
        source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT',
        pending_jobs: 2, running_jobs: 1, snapshot_at: '2026-10-14T05:00:00Z',
      }), { status: 200 });
    };
    const controller = new AgataWorkerGatewayController();
    await assert.rejects(controller.queueStatus({}), { status: 401 });
    const result = await controller.queueStatus({ authorization: 'Bearer worker-gateway-test-token' });
    assert.deepEqual(result, {
      source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT',
      pending_jobs: 2, running_jobs: 1, snapshot_at: '2026-10-14T05:00:00Z',
    });
    assert.deepEqual(requests, [{
      url: 'https://agata.internal/internal/v1/worker/queue-status', method: 'GET',
      authorization: 'Bearer internal-service-test-token', body: null,
    }]);
    globalThis.fetch = async () => new Response('private downstream details', { status: 403 });
    await assert.rejects(
      controller.queueStatus({ authorization: 'Bearer worker-gateway-test-token' }),
      (error: unknown) => error instanceof HttpException && error.getStatus() === 503 &&
        JSON.stringify(error.getResponse()) === JSON.stringify({ code: 'AGATA_UNAVAILABLE' }),
    );
    assert.equal(requests.length, 1);
  } finally {
    for (const [key, value] of Object.entries({
      GPOINT_AGATA_WORKER_TOKEN: previous.workerToken,
      AGATA_ENABLED: previous.enabled,
      AGATA_SERVICE_URL: previous.serviceUrl,
      AGATA_INTERNAL_API_TOKEN: previous.internalToken,
    })) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    globalThis.fetch = previous.fetch;
  }
});

test('enabled boundary synchronizes character and consent before idempotent respond', async () => {
  const calls: unknown[][] = [];
  const client = {
    async syncCharacter(...args: unknown[]) { calls.push(['character', ...args]); return { character_id: 'a', profile_version: 1 }; },
    async syncConsent(...args: unknown[]) { calls.push(['consent', ...args]); return { user_id: 'u', age_version: 1, disclosure: 'RECORDED' }; },
    async respond(...args: unknown[]) {
      calls.push(['respond', ...args]);
      return { request_id: input.requestId, conversation_id: 'conversation-1', message_id: input.messageId, character_id: 'character-1', text: 'reply' };
    },
  } as unknown as ConstructorParameters<typeof AgataIntegrationService>[0];
  const service = new AgataIntegrationService(client);
  const result = await service.respond(input);
  assert.deepEqual(calls.map(([name]) => name), ['character', 'consent', 'respond']);
  assert.equal((calls[2][1] as { message_id: string }).message_id, input.messageId);
  assert.equal(result.text, 'reply');
});

test('AGATA failures propagate; the adapter cannot manufacture a successful response', async () => {
  const failure = new Error('service unavailable');
  const service = new AgataIntegrationService({
    async syncCharacter() { throw failure; },
    async syncConsent() { assert.fail('consent must not run after failed character sync'); },
    async respond() { assert.fail('respond must not run after failed sync'); },
  } as unknown as ConstructorParameters<typeof AgataIntegrationService>[0]);
  await assert.rejects(service.respond(input), (error) => error === failure);
});
