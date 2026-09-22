import test from 'node:test';
import assert from 'node:assert/strict';
import { downloadTelegramFile } from '../src/telegram-channel.js';

const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
const response = (status: number, body: Uint8Array = bytes, contentType = 'image/jpeg') => new Response(Buffer.from(body), { status, headers: { 'content-type': contentType, 'content-length': String(body.byteLength) } });
const noWait = async () => undefined;

test('Telegram avatar download stores a successful binary response', async () => {
  let calls = 0;
  const result = await downloadTelegramFile('photos/avatar.jpg', 'token', async () => { calls += 1; return response(200); }, noWait);
  assert.deepEqual(result, Buffer.from(bytes));
  assert.equal(calls, 1);
});

test('connection reset is retried and then succeeds', async () => {
  let calls = 0;
  const result = await downloadTelegramFile('photos/avatar.jpg', 'token', async () => {
    calls += 1;
    if (calls === 1) throw Object.assign(new Error('reset'), { cause: { code: 'ECONNRESET' } });
    return response(200);
  }, noWait);
  assert.equal(result.length, bytes.byteLength);
  assert.equal(calls, 2);
});

test('temporary Telegram 5xx is retried', async () => {
  let calls = 0;
  const result = await downloadTelegramFile('photos/avatar.jpg', 'token', async () => {
    calls += 1;
    return calls === 1 ? response(503, new Uint8Array()) : response(200);
  }, noWait);
  assert.equal(result.length, bytes.byteLength);
  assert.equal(calls, 2);
});

test('429 has bounded retry behavior', async () => {
  let calls = 0;
  await assert.rejects(() => downloadTelegramFile('photos/avatar.jpg', 'token', async () => { calls += 1; return response(429, new Uint8Array()); }, noWait), /download failed/);
  assert.equal(calls, 4);
});

test('permanent 403 does not retry forever', async () => {
  let calls = 0;
  await assert.rejects(() => downloadTelegramFile('photos/avatar.jpg', 'token', async () => { calls += 1; return response(403, new Uint8Array()); }, noWait), /download failed/);
  assert.equal(calls, 1);
});

test('HTML and zero-byte responses are rejected without storing them', async () => {
  let calls = 0;
  await assert.rejects(() => downloadTelegramFile('photos/avatar.jpg', 'token', async () => { calls += 1; return response(200, new TextEncoder().encode('<html>error</html>'), 'text/html'); }, noWait), /download failed/);
  assert.equal(calls, 1);
});
