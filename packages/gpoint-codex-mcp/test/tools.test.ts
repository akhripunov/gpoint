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
  const api = { async request<T>(path: string, init?: RequestInit) {
    calls.push({ path, init });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    if (next && typeof next === 'object' && (next as { status?: string }).status === 'completed') {
      const body = JSON.parse(String(init?.body)) as { clientMessageId?: string };
      const completed = next as { userMessageId?: string; response?: { id?: string; text?: string } };
      return { ...completed, userMessageId: body.clientMessageId, response: { ...completed.response, id: completed.response?.id ?? `assistant-${calls.length}` } } as T;
    }
    return next as T;
  } };
  return { tools: createCodexTools(api, keychain), api, keychain, values, calls };
}

test('MCP connection persists device secret privately and surfaces only the short-lived Telegram link', async () => {
  const link = 'https://t.me/GPointBot?start=codex_' + 'T'.repeat(43);
  const { tools, values, calls } = fixture([{ deviceCode: 'x'.repeat(43), telegramLink: link, expiresIn: 300 }]);
  const result = await tools.connect('models-hub');
  assert.match(result, new RegExp(link.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.equal(result.includes('x'.repeat(43)), false);
  assert.equal(values.get('pending-device-code'), 'x'.repeat(43));
  assert.equal(JSON.parse(String(calls[0].init?.body)).creatorSlug, 'models-hub');
  await assert.rejects(tools.connect('../other'), /Models Hub/);
});

test('MCP exchange keeps both credentials in Keychain and history identifies the existing Models Hub thread', async () => {
  const { tools, values, calls } = fixture([{ status: 'connected', accessToken: 'a'.repeat(43), refreshToken: 'r'.repeat(43), accessExpiresAt: new Date(Date.now() + 600_000).toISOString(), refreshExpiresAt: new Date(Date.now() + 90_000_000).toISOString(), creatorId: 'creator/one' }, { items: [{ id: 'assistant-1', role: 'assistant', text: 'Exact history.', occurredAt: '2026-01-01T00:00:00Z' }] }]);
  values.set('pending-device-code', 'd'.repeat(43));
  assert.match(await tools.finish(), /Connected/);
  assert.equal(values.get('access-token'), 'a'.repeat(43));
  assert.equal(values.get('refresh-token'), 'r'.repeat(43));
  assert.equal(await tools.history(), 'MODELS HUB [id=assistant-1; 2026-01-01T00:00:00Z]: Exact history.');
  assert.equal(calls[1].path, '/messages/creator/creator%2Fone?limit=50');
  assert.equal(new Headers(calls[1].init?.headers).get('authorization'), `Bearer ${'a'.repeat(43)}`);
});

test('manual send returns the exact Models Hub text and disconnect revokes before removing credentials', async () => {
  const exact = '  The response, with punctuation!  ';
  const { tools, values, calls } = fixture([{ status: 'completed', response: { id: 'assistant-1', text: exact } }, { revoked: true }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'creator-1');
  const sent = await tools.send('hello');
  assert.equal(sent.status, 'completed');
  if (sent.status !== 'completed') throw new Error('Expected a completed Models Hub reply.');
  assert.equal(sent.text, exact);
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
  assert.equal(result.status, 'pending');
  assert.equal(values.has('pending-message'), true);
  assert.equal(calls.length, 1);
});

test('a timed-out send resumes with the same id and exact payload instead of creating a duplicate', async () => {
  const { tools, values, calls } = fixture([new Error('network timeout'), { status: 'completed', response: { id: 'server-assistant-id', text: 'Exact Models Hub reply.' } }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'creator-1');

  await assert.rejects(tools.send('  original text  '), /pending/i);
  const original = JSON.parse(String(calls[0].init?.body));
  assert.equal(original.text, 'original text');
  assert.equal(JSON.parse(String(values.get('pending-message'))).clientMessageId, original.clientMessageId);
  await assert.rejects(tools.send('different text'), /resume_pending_gpoint_message/i);

  const result = await tools.resumePendingMessage();
  assert.equal(result.status, 'completed');
  if (result.status !== 'completed') throw new Error('Expected the pending Models Hub reply to complete.');
  const resumed = JSON.parse(String(calls[1].init?.body));
  assert.deepEqual(resumed, original);
  assert.equal(result.text, 'Exact Models Hub reply.');
  assert.equal(result.userMessageId, original.clientMessageId);
  assert.equal(result.responseId, 'server-assistant-id');
  assert.equal(values.has('pending-message'), false);
});

test('automatic scenario cap persists across MCP restart and pending reconciliation consumes only one turn', async () => {
  const { tools, api, keychain, values, calls } = fixture([
    new Error('network timeout'),
    { status: 'completed', response: { id: 'assistant-1', text: 'Models Hub answer.' } },
  ]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'creator-1');

  const started = await tools.beginScenario('Continue the existing topic.', 1);
  assert.equal(started.maxTurns, 1);
  assert.equal(values.get('active-dialogue-scenario')?.includes('Continue the existing topic.'), false);
  await assert.rejects(tools.send('manual bypass'), /automatic scenario/i);
  await assert.rejects(tools.sendScenarioTurn('first turn'), /pending/i);

  const restartedTools = createCodexTools(api, keychain);
  const resumed = await restartedTools.resumePendingMessage();
  assert.equal(resumed.status, 'completed');
  if (resumed.status !== 'completed') throw new Error('Expected the pending scenario turn to complete.');
  assert.equal(resumed.text, 'Models Hub answer.');
  assert.equal(JSON.parse(String(values.get('active-dialogue-scenario'))).turnsUsed, 1);
  await assert.rejects(restartedTools.sendScenarioTurn('second turn'), /turn limit/i);
  assert.equal(calls.length, 2);
});

test('automatic scenario limits are mandatory and bounded to at most twenty turns', async () => {
  const { tools } = fixture([]);
  await assert.rejects(tools.beginScenario('scenario', 0), /1–20/);
  await assert.rejects(tools.beginScenario('scenario', 21), /1–20/);
});

test('connection status reports local token state without exposing credentials or claiming server validation', async () => {
  const { tools, values } = fixture([]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('refresh-token', 'r'.repeat(43));
  values.set('creator-id', 'models-hub-creator-id');
  const status = await tools.connectionStatus();
  assert.equal(status.status, 'local_token_present');
  assert.equal(status.creatorId, 'models-hub-creator-id');
  assert.equal(status.serverValidation, 'not_checked');
  assert.equal(JSON.stringify(status).includes('a'.repeat(43)), false);
  assert.equal(JSON.stringify(status).includes('r'.repeat(43)), false);
});

test('parallel MCP sends reach GPoint in call order', async () => {
  const { tools, values, calls } = fixture([{ status: 'completed', response: { text: 'one' } }, { status: 'completed', response: { text: 'two' } }]);
  values.set('access-token', 'a'.repeat(43));
  values.set('access-expiry', new Date(Date.now() + 600_000).toISOString());
  values.set('creator-id', 'creator-1');
  const results = await Promise.all([tools.send('first'), tools.send('second')]);
  assert.deepEqual(results.map((result) => result.status), ['completed', 'completed']);
  assert.deepEqual(results.map((result) => result.status === 'completed' ? result.text : ''), ['one', 'two']);
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
