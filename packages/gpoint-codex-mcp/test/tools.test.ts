import assert from 'node:assert/strict';
import test from 'node:test';
import { createApi, createCodexTools } from '../src/index.js';

function fixture(responses: unknown[]) {
  const values = new Map<string, string>();
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const keychain = {
    async get(key: string) { return values.get(key) ?? null; },
    async set(key: string, value: string) { values.set(key, value); },
    async delete(key: string) { values.delete(key); },
  };
  const api = { async request<T>(path: string, init?: RequestInit) { calls.push({ path, init }); const next = responses.shift(); if (next instanceof Error) throw next; return next as T; } };
  return { tools: createCodexTools(api, keychain), values, calls };
}

test('MCP connection persists device secret privately and surfaces only the one-time code', async () => {
  const { tools, values } = fixture([{ deviceCode: 'x'.repeat(43), userCode: '1234-ABCD', expiresIn: 300, verificationPath: '/messages' }]);
  const result = await tools.connect();
  assert.match(result, /1234-ABCD/);
  assert.equal(result.includes('x'.repeat(43)), false);
  assert.equal(values.get('pending-device-code'), 'x'.repeat(43));
});

test('MCP exchange keeps both credentials in Keychain and history uses the one bound creator', async () => {
  const { tools, values, calls } = fixture([{ status: 'connected', accessToken: 'a'.repeat(43), refreshToken: 'r'.repeat(43), accessExpiresAt: new Date(Date.now() + 600_000).toISOString(), refreshExpiresAt: new Date(Date.now() + 90_000_000).toISOString(), creatorId: 'creator/one' }, { items: [{ role: 'assistant', text: 'Exact history.', occurredAt: '2026-01-01T00:00:00Z' }] }]);
  values.set('pending-device-code', 'd'.repeat(43));
  assert.match(await tools.finish(), /Connected/);
  assert.equal(values.get('access-token'), 'a'.repeat(43));
  assert.equal(values.get('refresh-token'), 'r'.repeat(43));
  assert.equal(await tools.history(), 'AGATA [2026-01-01T00:00:00Z]: Exact history.');
  assert.equal(calls[1].path, '/messages/creator/creator%2Fone?limit=50');
  assert.equal(new Headers(calls[1].init?.headers).get('authorization'), `Bearer ${'a'.repeat(43)}`);
});

test('send returns AGATA text verbatim and disconnect revokes before removing credentials', async () => {
  const exact = '  The response, with punctuation!  ';
  const { tools, values, calls } = fixture([{ status: 'completed', response: { text: exact } }, { revoked: true }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'creator-1');
  assert.equal(await tools.send('hello'), exact);
  assert.equal(JSON.parse(String(calls[0].init?.body)).text, 'hello');
  assert.match(calls[0].path, /^\/messages\/creator\/creator-1$/);
  assert.match(await tools.disconnect(), /revoked/);
  assert.deepEqual([...values.keys()], []);
});

test('pending send never claims the next shared-conversation reply as its own', async () => {
  const { tools, values, calls } = fixture([{ status: 'pending' }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('creator-id', 'creator-1');
  const result = await tools.send('hello');
  assert.match(result, /still pending/);
  assert.equal(calls.length, 1);
});

test('parallel MCP sends reach GPoint in call order', async () => {
  const { tools, values, calls } = fixture([{ status: 'completed', response: { text: 'one' } }, { status: 'completed', response: { text: 'two' } }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('creator-id', 'creator-1');
  assert.deepEqual(await Promise.all([tools.send('first'), tools.send('second')]), ['one', 'two']);
  assert.deepEqual(calls.map(call => JSON.parse(String(call.init?.body)).text), ['first', 'second']);
});

test('parallel calls rotate an expired refresh token only once', async () => {
  const { tools, values, calls } = fixture([
    { accessToken: 'b'.repeat(43), refreshToken: 's'.repeat(43), accessExpiresAt: new Date(Date.now() + 600_000).toISOString(), creatorId: 'creator-1' },
    { items: [] }, { items: [] },
  ]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() - 60_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'creator-1');
  await Promise.all([tools.history(), tools.history()]);
  assert.equal(calls.filter(call => call.path === '/auth/codex/device/refresh').length, 1);
});

test('GPoint API URL rejects non-TLS non-loopback destinations', () => {
  assert.throws(() => createApi('http://gpoint.invalid'), /HTTPS/);
  assert.throws(() => createApi('https://user:pass@gpoint.invalid'), /credentials/);
  assert.throws(() => createApi('https://gpoint.invalid/?token=unsafe'), /credentials/);
  assert.doesNotThrow(() => createApi('http://127.0.0.1:3000'));
});
