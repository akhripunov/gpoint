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
