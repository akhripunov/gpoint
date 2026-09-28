import assert from 'node:assert/strict';
import test from 'node:test';
import { AgeAssuranceController } from '../src/messages.js';
import { createSession } from '../src/session.js';

const ageRequired = {
  required: true,
  eligible: false,
  status: 'pending',
  method: 'unknown',
  jurisdiction: 'GLOBAL',
  assertedAt: null,
  expiresAt: null,
  selfAttestationAllowed: true,
};

const ageAccepted = {
  required: false,
  eligible: true,
  status: 'self_attested',
  method: 'self_attested',
  jurisdiction: 'GLOBAL',
  assertedAt: '2026-09-28T10:00:00.000Z',
  expiresAt: null,
  selfAttestationAllowed: true,
};

test('REG-085 — authenticated self-attestation writes and immediately reads back for the same GPoint principal', async () => {
  const previousFetch = globalThis.fetch;
  const env = {
    enabled: process.env.AGATA_ENABLED,
    serviceUrl: process.env.AGATA_SERVICE_URL,
    token: process.env.AGATA_INTERNAL_API_TOKEN,
  };
  const requests: Array<{ url: string; method: string; body?: string }> = [];
  const userId = 'authenticated-telegram-user';
  const session = createSession(userId);
  process.env.AGATA_ENABLED = 'true';
  process.env.AGATA_SERVICE_URL = 'https://agata.test';
  process.env.AGATA_INTERNAL_API_TOKEN = 'disposable-test-token';
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    requests.push({ url, method, ...(typeof init?.body === 'string' ? { body: init.body } : {}) });
    const payload = method === 'POST' ? { eligible: true, status: 'self_attested', method: 'self_attested' }
      : requests.length === 1 ? ageRequired : ageAccepted;
    return new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } });
  };

  try {
    const controller = new AgeAssuranceController();
    const headers = { authorization: `Bearer ${session}` };
    assert.deepEqual(await controller.state(headers), ageRequired);
    assert.deepEqual(await controller.selfAttest(headers, { confirmed18Plus: true }), ageAccepted);
    assert.deepEqual(requests.map(({ method }) => method), ['GET', 'POST', 'GET']);
    assert.ok(requests.every(({ url }) => url.includes(`/users/${userId}/age-assurance`)));
    assert.deepEqual(JSON.parse(requests[1].body ?? '{}'), { confirmed_18_plus: true });
  } finally {
    globalThis.fetch = previousFetch;
    if (env.enabled === undefined) delete process.env.AGATA_ENABLED; else process.env.AGATA_ENABLED = env.enabled;
    if (env.serviceUrl === undefined) delete process.env.AGATA_SERVICE_URL; else process.env.AGATA_SERVICE_URL = env.serviceUrl;
    if (env.token === undefined) delete process.env.AGATA_INTERNAL_API_TOKEN; else process.env.AGATA_INTERNAL_API_TOKEN = env.token;
  }
});

test('REG-085 — a rejected server write never maps to age acceptance', async () => {
  const previousFetch = globalThis.fetch;
  const env = {
    enabled: process.env.AGATA_ENABLED,
    serviceUrl: process.env.AGATA_SERVICE_URL,
    token: process.env.AGATA_INTERNAL_API_TOKEN,
  };
  const session = createSession('authenticated-telegram-user');
  process.env.AGATA_ENABLED = 'true';
  process.env.AGATA_SERVICE_URL = 'https://agata.test';
  process.env.AGATA_INTERNAL_API_TOKEN = 'disposable-test-token';
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: { code: 'self_attestation_not_allowed' } }),
    { status: 403, headers: { 'content-type': 'application/json' } },
  );
  try {
    const controller = new AgeAssuranceController();
    await assert.rejects(
      controller.selfAttest({ authorization: `Bearer ${session}` }, { confirmed18Plus: true }),
      (error: unknown) => error instanceof Error && 'getStatus' in error
        && (error as { getStatus: () => number }).getStatus() === 503,
    );
  } finally {
    globalThis.fetch = previousFetch;
    if (env.enabled === undefined) delete process.env.AGATA_ENABLED; else process.env.AGATA_ENABLED = env.enabled;
    if (env.serviceUrl === undefined) delete process.env.AGATA_SERVICE_URL; else process.env.AGATA_SERVICE_URL = env.serviceUrl;
    if (env.token === undefined) delete process.env.AGATA_INTERNAL_API_TOKEN; else process.env.AGATA_INTERNAL_API_TOKEN = env.token;
  }
});
