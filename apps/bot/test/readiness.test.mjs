import test from 'node:test';
import assert from 'node:assert/strict';
import { BOT_READINESS_HOST, handleBotReadiness } from '../src/bot-readiness.js';

function responseFor(request, ready) {
  const response = {
    status: undefined,
    headers: undefined,
    body: undefined,
    writeHead(status, headers) { this.status = status; this.headers = headers; },
    end(body) { this.body = body; },
  };
  handleBotReadiness(request, response, () => ready);
  return response;
}

test('Bot readiness endpoint is loopback-only and reflects Telegram polling state', () => {
  assert.equal(BOT_READINESS_HOST, '127.0.0.1');
  const starting = responseFor({ method: 'GET', url: '/health/ready' }, false);
  assert.equal(starting.status, 503);
  assert.deepEqual(JSON.parse(starting.body), { status: 'starting' });
  assert.equal(starting.headers['cache-control'], 'no-store');

  const ready = responseFor({ method: 'GET', url: '/health/ready' }, true);
  assert.equal(ready.status, 200);
  assert.deepEqual(JSON.parse(ready.body), { status: 'ready' });

  const wrongPath = responseFor({ method: 'GET', url: '/' }, true);
  assert.equal(wrongPath.status, 404);
  assert.deepEqual(JSON.parse(wrongPath.body), { status: 'not_found' });
});
