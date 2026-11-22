import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';
import { createWorkerTunnelProxy } from '../src/gpoint-worker-tunnel-proxy.mjs';

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server.address().port;
}

async function close(server) {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

test('tunnels only worker routes through the existing Kamal Proxy', async (t) => {
  let upstreamRequest;
  const upstream = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    upstreamRequest = {
      method: request.method,
      url: request.url,
      host: request.headers.host,
      authorization: request.headers.authorization,
      body: Buffer.concat(chunks).toString(),
    };
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ status: 'ok' }));
  });
  const upstreamPort = await listen(upstream);
  const proxy = createWorkerTunnelProxy({ upstreamHost: '127.0.0.1', upstreamPort });
  const proxyPort = await listen(proxy);
  t.after(async () => {
    await close(proxy);
    await close(upstream);
  });

  const response = await fetch(`http://127.0.0.1:${proxyPort}/gpoint-worker/claim`, {
    method: 'POST',
    headers: {
      authorization: 'Bearer worker-test-token',
      'content-type': 'application/json',
    },
    body: JSON.stringify({ worker_id: 'mac-product-worker' }),
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok' });
  assert.deepEqual(upstreamRequest, {
    method: 'POST',
    url: '/api/gpoint-worker/claim',
    host: 'gpoint.one',
    authorization: 'Bearer worker-test-token',
    body: JSON.stringify({ worker_id: 'mac-product-worker' }),
  });
});

test('rejects unrelated routes and methods without contacting the upstream', async (t) => {
  let upstreamHits = 0;
  const upstream = http.createServer((_request, response) => {
    upstreamHits += 1;
    response.writeHead(200).end();
  });
  const upstreamPort = await listen(upstream);
  const proxy = createWorkerTunnelProxy({ upstreamHost: '127.0.0.1', upstreamPort });
  const proxyPort = await listen(proxy);
  t.after(async () => {
    await close(proxy);
    await close(upstream);
  });

  const unrelated = await fetch(`http://127.0.0.1:${proxyPort}/admin`);
  assert.equal(unrelated.status, 404);
  const wrongMethod = await fetch(`http://127.0.0.1:${proxyPort}/gpoint-worker/claim`);
  assert.equal(wrongMethod.status, 405);
  assert.equal(upstreamHits, 0);
});

test('allows only the read-only queue status GET endpoint', async (t) => {
  const upstream = http.createServer((request, response) => {
    assert.equal(request.method, 'GET');
    assert.equal(request.url, '/api/gpoint-worker/queue-status');
    assert.equal(request.headers.host, 'gpoint.one');
    response.writeHead(200, { 'content-type': 'application/json' }).end('{"status":"ok"}');
  });
  const upstreamPort = await listen(upstream);
  const proxy = createWorkerTunnelProxy({ upstreamHost: '127.0.0.1', upstreamPort });
  const proxyPort = await listen(proxy);
  t.after(async () => {
    await close(proxy);
    await close(upstream);
  });

  const response = await fetch(`http://127.0.0.1:${proxyPort}/gpoint-worker/queue-status`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok' });
});

test('keeps the Worker tunnel online across two Core API updates and rollback', async (t) => {
  let activeApiVersion = 'core-v1';
  const kamalProxy = http.createServer((request, response) => {
    assert.equal(request.headers.host, 'gpoint.one');
    assert.equal(request.url, '/api/gpoint-worker/health');
    if (request.headers.authorization !== 'Bearer worker-test-token') {
      response.writeHead(401, { 'content-type': 'application/json' }).end('{"status":"unauthorized"}');
      return;
    }
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ status: 'ok', core: activeApiVersion }));
  });
  const upstreamPort = await listen(kamalProxy);
  const workerProxy = createWorkerTunnelProxy({ upstreamHost: '127.0.0.1', upstreamPort });
  const workerProxyPort = await listen(workerProxy);
  t.after(async () => {
    await close(workerProxy);
    await close(kamalProxy);
  });

  async function workerHealth(expectedVersion) {
    const response = await fetch(`http://127.0.0.1:${workerProxyPort}/gpoint-worker/health`, {
      method: 'POST',
      headers: { authorization: 'Bearer worker-test-token', 'content-type': 'application/json' },
      body: '{}',
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: 'ok', core: expectedVersion });
  }

  const unauthorized = await fetch(`http://127.0.0.1:${workerProxyPort}/gpoint-worker/health`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  });
  assert.equal(unauthorized.status, 401);

  await workerHealth('core-v1');
  activeApiVersion = 'core-v2';
  await workerHealth('core-v2');
  activeApiVersion = 'core-v3';
  await workerHealth('core-v3');
  activeApiVersion = 'core-v2';
  await workerHealth('core-v2');
});
