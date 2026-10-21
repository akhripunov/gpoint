import { spawn } from 'node:child_process';
import { createHash, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { isAgataMessageHistoryPath } from './local-test-api-routes.mjs';

const state = {
  chatHistory: true,
  longTermMemory: true,
  disclosureRequired: false,
  onboardingCompleted: true,
  disclosureVersion: 1,
  onboardingCompletedAt: '2026-01-01T00:00:00.000Z',
  memoryOptIn: true,
  memoryOptInAt: '2026-01-01T00:00:00.000Z',
  ageAssuranceRequired: false,
  ageBand: 'adult',
  ageStatus: 'self_attested',
  ageMethod: 'synthetic_local_test',
  verifiedAt: '2026-01-01T00:00:00.000Z',
  expiresAt: '2030-01-01T00:00:00.000Z',
  jurisdiction: 'TEST',
};

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

function authorized(request, expected) {
  const value = request.headers.authorization;
  if (typeof value !== 'string' || !value.startsWith('Bearer ')) return false;
  const actual = Buffer.from(value.slice(7));
  const target = Buffer.from(expected ?? '');
  return actual.length === target.length && actual.length > 0 && timingSafeEqual(actual, target);
}

const agata = createServer(async (request, response) => {
  try {
    if (request.url === '/control/state' && request.method === 'POST') {
      if (!authorized(request, process.env.EROS_TEST_AGATA_TOKEN)) {
        return send(response, 401, { code: 'unauthorized' });
      }
      const update = await bodyJson(request);
      for (const key of ['chatHistory', 'longTermMemory', 'ageAssuranceRequired']) {
        if (typeof update[key] === 'boolean') state[key] = update[key];
      }
      return send(response, 200, { updated: true });
    }

    if (!authorized(request, process.env.AGATA_INTERNAL_API_TOKEN)) {
      return send(response, 401, { code: 'unauthorized' });
    }
    const url = new URL(request.url ?? '/', 'http://127.0.0.1');

    if (url.pathname === '/health/ready' && request.method === 'GET') {
      return send(response, 200, { status: 'ok' });
    }
    if (url.pathname === '/internal/v1/worker/claim' && request.method === 'POST') {
      return send(response, 200, { job: null });
    }
    if (url.pathname === '/internal/v1/worker/queue-status' && request.method === 'GET') {
      return send(response, 200, {
        source_id: 'agata-worker-queue-status-v1:gpoint',
        lane: 'GPOINT',
        pending_jobs: 0,
        running_jobs: 0,
        snapshot_at: new Date().toISOString(),
      });
    }
    if (/^\/internal\/v1\/characters\/[^/]+$/.test(url.pathname) && request.method === 'PUT') {
      return send(response, 200, {
        character_id: url.pathname.split('/').at(-1),
        profile_version: 1,
      });
    }
    if (url.pathname === '/internal/v1/respond' && request.method === 'POST') {
      const body = await bodyJson(request);
      return send(response, 200, {
        status: 'completed',
        request_id: body.request_id,
        conversation_id: body.conversation_id,
        message_id: body.message_id,
        character_id: body.gpoint_character_id,
        text: 'SYNTHETIC_AGATA_FALLBACK_REPLY',
      });
    }
    if (/^\/internal\/v1\/users\/[^/]+\/chat-state$/.test(url.pathname) && request.method === 'GET') {
      return send(response, 200, state);
    }
    if (isAgataMessageHistoryPath(request.url ?? '/') && request.method === 'GET') {
      return send(response, 200, { items: [], nextCursor: null });
    }
    if (/^\/internal\/v1\/users\/[^/]+\/consent$/.test(url.pathname) && request.method === 'PUT') {
      return send(response, 200, { age_version: 1, disclosure: 'RECORDED' });
    }
    return send(response, 404, { code: 'not_found' });
  } catch {
    return send(response, 400, { code: 'local_test_fixture_invalid' });
  }
});

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

await Promise.all([
  new Promise((resolve, reject) => agata.once('error', reject).listen(18080, '127.0.0.1', resolve)),
  new Promise((resolve, reject) => embeddings.once('error', reject).listen(11434, '127.0.0.1', resolve)),
]);

const api = spawn(
  'pnpm',
  ['--filter', '@gpoint/api', 'exec', 'tsx', 'src/main.ts'],
  { cwd: '/workspace', env: process.env, stdio: 'inherit' },
);

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    api.kill(signal);
    agata.close();
    embeddings.close();
  });
}
api.once('exit', (code) => process.exit(code ?? 1));
