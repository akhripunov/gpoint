import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';

function send(response, status, body) {
  response.writeHead(status, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
  });
  response.end(JSON.stringify(body));
}

async function bodyJson(request) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > 1_000_000) throw new Error('request_too_large');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString() || '{}');
}

const embeddings = createServer(async (request, response) => {
  try {
    if (request.url !== '/api/embed' || request.method !== 'POST') {
      return send(response, 404, { code: 'not_found' });
    }
    const body = await bodyJson(request);
    const inputs = Array.isArray(body.input) ? body.input : [body.input];
    if (!inputs.length || inputs.some((input) => typeof input !== 'string')) {
      return send(response, 400, { code: 'invalid_input' });
    }
    const vectors = inputs.map((input) => Array.from({ length: 512 }, (_, index) => {
      const digest = createHash('sha256').update(`${input}\0${index}`).digest();
      return (digest.readUInt32BE(0) / 0xffffffff) * 2 - 1;
    }));
    return send(response, 200, { model: 'synthetic-test-embedding', embeddings: vectors });
  } catch {
    return send(response, 400, { code: 'invalid_input' });
  }
});

await new Promise((resolve, reject) => embeddings.once('error', reject).listen(11434, '127.0.0.1', resolve));

const api = spawn(
  'pnpm',
  ['--filter', '@gpoint/api', 'exec', 'tsx', 'src/main.ts'],
  { cwd: new URL('../../', import.meta.url), env: process.env, stdio: 'inherit' },
);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    api.kill(signal);
    embeddings.close();
  });
}
api.once('exit', (code) => process.exit(code ?? 1));
