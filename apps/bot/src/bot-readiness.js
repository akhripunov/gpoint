import { createServer } from 'node:http';

export const BOT_READINESS_HOST = '127.0.0.1';

export function handleBotReadiness(request, response, isReady) {
  if (request.method !== 'GET' || request.url !== '/health/ready') {
    response.writeHead(404, { 'content-type': 'application/json' });
    response.end('{"status":"not_found"}');
    return;
  }
  const ready = isReady();
  response.writeHead(ready ? 200 : 503, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
  });
  response.end(JSON.stringify({ status: ready ? 'ready' : 'starting' }));
}

export function startBotReadinessServer(isReady, port = 9091) {
  const server = createServer((request, response) => handleBotReadiness(request, response, isReady));
  server.listen(port, BOT_READINESS_HOST);
  return server;
}
