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

test('queue status is a registered authenticated GET and aggregates content-free GPOINT and Eros counts', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(AgataWorkerGatewayController.prototype, 'queueStatus');
  assert.ok(descriptor?.value);
  assert.equal(Reflect.getMetadata('path', descriptor.value), 'queue-status');
  assert.equal(Reflect.getMetadata('method', descriptor.value), 0);

  class TestGateway extends AgataWorkerGatewayController {
    protected override async readAgataQueueStatus() {
      return { source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT' as const,
        pending_jobs: 2, running_jobs: 1, snapshot_at: '2026-10-14T05:00:00Z' };
    }
    protected override async readErosQueueStatus() {
      return { pending_jobs: 3, running_jobs: 2 };
    }
  }

  const controller = new TestGateway();
  const previousToken = process.env.GPOINT_AGATA_WORKER_TOKEN;
  const previousEnabled = process.env.EROS_ENABLED;
  process.env.GPOINT_AGATA_WORKER_TOKEN = 'worker-gateway-test-token';
  process.env.EROS_ENABLED = 'true';
  try {
    await assert.rejects(controller.queueStatus({}), { status: 401 });
    const result = await controller.queueStatus({ authorization: 'Bearer worker-gateway-test-token' });
    assert.deepEqual(result, {
      source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT',
      pending_jobs: 5, running_jobs: 3, snapshot_at: '2026-10-14T05:00:00Z',
      eros_pending_jobs: 3, eros_running_jobs: 2,
      eros_queue_available: true,
    });
  } finally {
    if (previousToken === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN;
    else process.env.GPOINT_AGATA_WORKER_TOKEN = previousToken;
    if (previousEnabled === undefined) delete process.env.EROS_ENABLED;
    else process.env.EROS_ENABLED = previousEnabled;
  }
});

test('Eros queue status is disabled without EROS configuration', async () => {
  class TestGateway extends AgataWorkerGatewayController {
    async erosQueueStatus() { return this.readErosQueueStatus(); }
  }
  const previousEnabled = process.env.EROS_ENABLED;
  delete process.env.EROS_ENABLED;
  try {
    assert.deepEqual(await new TestGateway().erosQueueStatus(), { pending_jobs: 0, running_jobs: 0 });
  } finally {
    if (previousEnabled === undefined) delete process.env.EROS_ENABLED;
    else process.env.EROS_ENABLED = previousEnabled;
  }
});

test('queue status preserves GPOINT health when the optional Eros queue is unavailable', async () => {
  class TestGateway extends AgataWorkerGatewayController {
    protected override async readAgataQueueStatus() {
      return { source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT' as const,
        pending_jobs: 2, running_jobs: 1, snapshot_at: 'now' };
    }
    protected override async readErosQueueStatus(): Promise<{ pending_jobs: number; running_jobs: number }> {
      throw new Error('synthetic Eros queue outage');
    }
  }
  const previous = { token: process.env.GPOINT_AGATA_WORKER_TOKEN, enabled: process.env.EROS_ENABLED };
  process.env.GPOINT_AGATA_WORKER_TOKEN = 'worker-gateway-test-token';
  process.env.EROS_ENABLED = 'true';
  try {
    assert.deepEqual(await new TestGateway().queueStatus({ authorization: 'Bearer worker-gateway-test-token' }), {
      source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT', pending_jobs: 2, running_jobs: 1,
      snapshot_at: 'now', eros_pending_jobs: 0, eros_running_jobs: 0, eros_queue_available: false,
    });
  } finally {
    if (previous.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = previous.token;
    if (previous.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = previous.enabled;
  }
});

test('claim gives existing GPOINT jobs priority over the optional Eros queue', async () => {
  let erosCalled = false;
  class TestGateway extends AgataWorkerGatewayController {
    protected override async claimAgataJob() { return { job: { id: 'agata-user-reply' } as never }; }
    protected override async claimErosJob() { erosCalled = true; return { job: { id: 'eros' } }; }
  }
  const previous = { token: process.env.GPOINT_AGATA_WORKER_TOKEN, enabled: process.env.EROS_ENABLED };
  process.env.GPOINT_AGATA_WORKER_TOKEN = 'worker-gateway-test-token';
  process.env.EROS_ENABLED = 'true';
  try {
    assert.deepEqual(await new TestGateway().claim({ authorization: 'Bearer worker-gateway-test-token' }), {
      job: { id: 'agata-user-reply' },
    });
    assert.equal(erosCalled, false);
  } finally {
    if (previous.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = previous.token;
    if (previous.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = previous.enabled;
  }
});

test('claim returns an empty GPOINT poll instead of failing when optional Eros arbitration is down', async () => {
  class TestGateway extends AgataWorkerGatewayController {
    protected override async claimAgataJob() { return { job: null }; }
    protected override async claimErosJob(): Promise<never> { throw new Error('synthetic Eros queue outage'); }
  }
  const previous = { token: process.env.GPOINT_AGATA_WORKER_TOKEN, enabled: process.env.EROS_ENABLED };
  process.env.GPOINT_AGATA_WORKER_TOKEN = 'worker-gateway-test-token';
  process.env.EROS_ENABLED = 'true';
  try {
    assert.deepEqual(await new TestGateway().claim({ authorization: 'Bearer worker-gateway-test-token' }), { job: null });
  } finally {
    if (previous.token === undefined) delete process.env.GPOINT_AGATA_WORKER_TOKEN; else process.env.GPOINT_AGATA_WORKER_TOKEN = previous.token;
    if (previous.enabled === undefined) delete process.env.EROS_ENABLED; else process.env.EROS_ENABLED = previous.enabled;
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
