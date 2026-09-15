import test from 'node:test';
import assert from 'node:assert/strict';

const api = process.env.GPOINT_API_URL ?? 'http://localhost:3000';

test('real API stack is reachable', async () => {
  const response = await fetch(`${api}/health/live`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok' });
});

test('public API does not expose an unknown private media asset', async () => {
  const response = await fetch(`${api}/media/assets/e2e-nonexistent-media`);
  assert.equal(response.status, 404);
});
