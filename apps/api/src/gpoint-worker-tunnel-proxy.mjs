import http from 'node:http';

const WORKER_PATHS = new Map([
  ['/gpoint-worker/health', 'POST'],
  ['/gpoint-worker/claim', 'POST'],
  ['/gpoint-worker/cancel-check', 'POST'],
  ['/gpoint-worker/fail', 'POST'],
  ['/gpoint-worker/complete', 'POST'],
  ['/gpoint-worker/queue-status', 'GET'],
]);

export function createWorkerTunnelProxy({
  upstreamHost = 'gpoint-core-api',
  upstreamPort = 3000,
  requestTimeoutMs = 18_000,
} = {}) {
  if (!Number.isInteger(upstreamPort) || upstreamPort < 1 || upstreamPort > 65_535) {
    throw new TypeError('upstreamPort must be a valid TCP port');
  }

  return http.createServer((request, response) => {
    let url;
    try {
      url = new URL(request.url ?? '/', 'http://127.0.0.1');
    } catch {
      response.writeHead(400).end();
      return;
    }

    const expectedMethod = WORKER_PATHS.get(url.pathname);
    if (!expectedMethod) {
      response.writeHead(404).end();
      return;
    }
    if (request.method !== expectedMethod) {
      response.writeHead(405, { allow: expectedMethod }).end();
      return;
    }

    const headers = { host: 'gpoint.one', connection: 'close' };
    for (const name of ['accept', 'authorization', 'content-length', 'content-type']) {
      const value = request.headers[name];
      if (value !== undefined) headers[name] = value;
    }

    const upstream = http.request({
      hostname: upstreamHost,
      port: upstreamPort,
      method: request.method,
      path: `${url.pathname}${url.search}`,
      headers,
      timeout: requestTimeoutMs,
    }, (upstreamResponse) => {
      const responseHeaders = {};
      for (const name of ['content-length', 'content-type', 'retry-after']) {
        const value = upstreamResponse.headers[name];
        if (value !== undefined) responseHeaders[name] = value;
      }
      response.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders);
      upstreamResponse.pipe(response);
    });

    upstream.on('timeout', () => upstream.destroy(new Error('upstream timeout')));
    upstream.on('error', () => {
      if (response.headersSent) {
        response.destroy();
        return;
      }
      response.writeHead(502, { 'content-type': 'application/json' });
      response.end('{"status":"unavailable"}');
    });
    request.on('aborted', () => upstream.destroy());
    request.pipe(upstream);
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const port = Number(process.env.PORT ?? '3000');
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('PORT must be a valid TCP port');
  }
  createWorkerTunnelProxy().listen(port, '0.0.0.0');
}
