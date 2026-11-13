import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('production API build includes the gateway network policy helper', async () => {
  const compiled = new URL('../dist/apps/api/src/eros-network-policy.js', import.meta.url);
  assert.deepEqual(await readFile(compiled), await readFile(new URL('../src/eros-network-policy.js', import.meta.url)));
  const gateway = await readFile(new URL('../dist/apps/api/src/eros-worker-gateway.js', import.meta.url), 'utf8');
  assert.match(gateway, /from ['"]\.\/eros-network-policy\.js['"]/);
  const { isAllowedOllamaEmbeddingEndpoint } = await import(compiled.href);
  assert.equal(typeof isAllowedOllamaEmbeddingEndpoint, 'function');
  const { ErosWorkerGatewayController } = await import('../dist/apps/api/src/eros-worker-gateway.js');
  assert.equal(typeof ErosWorkerGatewayController, 'function');
});

test('production API build includes the GPoint chat policy state machine', async () => {
  const compiled = new URL('../dist/apps/api/src/gpoint-chat-state.js', import.meta.url);
  assert.deepEqual(await readFile(compiled), await readFile(new URL('../src/gpoint-chat-state.js', import.meta.url)));
  const { buildGpointMessagesState, isGpointChatPermitted } = await import(compiled.href);
  assert.equal(typeof buildGpointMessagesState, 'function');
  assert.equal(typeof isGpointChatPermitted, 'function');
});

test('production API build includes the GPoint-owned Eros response policy', async () => {
  const compiled = new URL('../dist/apps/api/src/eros-response-prompt.js', import.meta.url);
  assert.deepEqual(await readFile(compiled), await readFile(new URL('../src/eros-response-prompt.js', import.meta.url)));
  const adapter = await readFile(new URL('../dist/apps/api/src/eros-adapter.js', import.meta.url), 'utf8');
  assert.match(adapter, /from ['"]\.\/eros-response-prompt\.js['"]/);
  const { GPOINT_EROS_RESPONSE_PROMPT_TRAITS } = await import(compiled.href);
  assert.deepEqual(GPOINT_EROS_RESPONSE_PROMPT_TRAITS.map(({ tag }) => tag), [
    'gpoint_reply_language', 'gpoint_language_privacy', 'gpoint_turn_style',
  ]);
});
