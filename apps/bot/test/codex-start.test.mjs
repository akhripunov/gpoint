import assert from 'node:assert/strict';
import test from 'node:test';
import { forwardCodexStart } from '../src/codex-start.ts';

test('valid private Telegram /start proof is forwarded to the fixed internal approval route without redirects', async () => {
  const prior = { url: process.env.GPOINT_INGEST_URL, secret: process.env.TELEGRAM_WEBHOOK_SECRET };
  process.env.GPOINT_INGEST_URL = 'http://127.0.0.1:3000/internal/telegram/update';
  process.env.TELEGRAM_WEBHOOK_SECRET = 'internal-test-secret';
  const requests = [];
  try {
    const result = await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 42, type: 'private' }, { id: 42 }, async (url, init) => {
      requests.push({ url: String(url), init });
      return { ok: true, json: async () => ({ approved: true }) };
    });
    assert.deepEqual(result, { approved: true });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, 'http://127.0.0.1:3000/auth/codex/telegram/codex-approve');
    assert.equal(requests[0].init.redirect, 'error');
    assert.equal(new Headers(requests[0].init.headers).get('x-telegram-webhook-secret'), 'internal-test-secret');
    assert.deepEqual(JSON.parse(requests[0].init.body), {
      payload: 'A'.repeat(43), chat: { id: 42, type: 'private' }, from: { id: 42 },
    });
  } finally {
    if (prior.url === undefined) delete process.env.GPOINT_INGEST_URL; else process.env.GPOINT_INGEST_URL = prior.url;
    if (prior.secret === undefined) delete process.env.TELEGRAM_WEBHOOK_SECRET; else process.env.TELEGRAM_WEBHOOK_SECRET = prior.secret;
  }
});

test('group chat and mismatched Telegram sender are never forwarded', async () => {
  const forward = async () => assert.fail('unexpected internal API request');
  assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: -20, type: 'group' }, { id: 42 }, forward), { approved: false });
  assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 7, type: 'private' }, { id: 42 }, forward), { approved: false });
});

test('Telegram IDs outside the supported signed database range are rejected', async () => {
  const forward = async () => assert.fail('out-of-range Telegram ID must not reach API');
  const id = '9223372036854775808';
  assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id, type: 'private' }, { id }, forward), { approved: false });
});

test('missing Telegram sender is denied without throwing or forwarding', async () => {
  const forward = async () => assert.fail('unexpected internal API request');
  assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 42, type: 'private' }, undefined, forward), { approved: false });
});

test('invalid payload and missing shared secret fail closed without a request', async () => {
  const prior = process.env.TELEGRAM_WEBHOOK_SECRET;
  delete process.env.TELEGRAM_WEBHOOK_SECRET;
  const forward = async () => assert.fail('unexpected internal API request');
  try {
    assert.deepEqual(await forwardCodexStart('codex_short', { id: 42, type: 'private' }, { id: 42 }, forward), { approved: false });
    assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 42, type: 'private' }, { id: 42 }, forward), { approved: false });
  } finally {
    if (prior !== undefined) process.env.TELEGRAM_WEBHOOK_SECRET = prior;
  }
});

test('misconfigured ingest target and internal API failure return generic denial', async () => {
  const prior = { url: process.env.GPOINT_INGEST_URL, secret: process.env.TELEGRAM_WEBHOOK_SECRET };
  process.env.GPOINT_INGEST_URL = 'https://attacker.invalid/collect';
  process.env.TELEGRAM_WEBHOOK_SECRET = 'internal-test-secret';
  try {
    const never = async () => assert.fail('misconfigured URL must fail before fetch');
    assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 42, type: 'private' }, { id: 42 }, never), { approved: false });
    process.env.GPOINT_INGEST_URL = 'http://127.0.0.1:3000/internal/telegram/update';
    assert.deepEqual(await forwardCodexStart('codex_' + 'A'.repeat(43), { id: 42, type: 'private' }, { id: 42 }, async () => ({ ok: false, json: async () => ({}) })), { approved: false });
  } finally {
    if (prior.url === undefined) delete process.env.GPOINT_INGEST_URL; else process.env.GPOINT_INGEST_URL = prior.url;
    if (prior.secret === undefined) delete process.env.TELEGRAM_WEBHOOK_SECRET; else process.env.TELEGRAM_WEBHOOK_SECRET = prior.secret;
  }
});
