import assert from 'node:assert/strict';
import test from 'node:test';
import type { AgataClient } from '@gpoint/agata-client';
import { createAgataIntegration, AgataIntegrationService } from '../src/agata-integration.js';
import { assertWorkerAuth } from '../src/agata-worker-gateway.js';

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
