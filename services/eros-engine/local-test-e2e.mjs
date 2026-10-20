import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

const api = 'http://127.0.0.1:3000';
const creator = 'eros_test_creator';
const control = 'http://127.0.0.1:18080/control/state';

async function request(url, { method = 'GET', token, body } = {}) {
  const response = await fetch(url, {
    method,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(60_000),
  });
  return { status: response.status, data: await response.json().catch(() => null) };
}

async function setFixtureState(update) {
  const response = await request(control, {
    method: 'POST',
    token: process.env.EROS_TEST_AGATA_TOKEN,
    body: update,
  });
  assert.equal(response.status, 200);
}

async function authenticate() {
  const response = await request(`${api}/auth/telegram`, {
    method: 'POST',
    body: { initData: 'synthetic local E2E', devTelegramUserId: '900000001' },
  });
  assert.equal(response.status, 201);
  assert.equal(response.data?.user?.id, 'eros_test_user');
  assert.equal(response.data?.user?.telegramUsername, 'eros_test_dialogue');
  return response.data.accessToken;
}

async function readHistory(token) {
  return request(`${api}/messages/creator/${creator}?limit=100`, { token });
}

async function assertCompleteHistory(token, stage, expectedTurns = 16) {
  const response = await readHistory(token);
  assert.equal(response.status, 200, `${stage}: history read`);
  const users = response.data.items.filter((item) => item.role === 'user');
  const assistants = response.data.items.filter((item) => item.role === 'assistant');
  assert.equal(users.length, expectedTurns, `${stage}: user message count`);
  assert.equal(assistants.length, expectedTurns, `${stage}: assistant message count`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_CITY_ZEPHYRBAY')), `${stage}: city fact`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_OLD_2026-10-22_18:40')), `${stage}: historical meeting`);
  assert.ok(users.some((item) => item.text.includes('SYNTH_CURRENT_2026-10-23_19:15')), `${stage}: current meeting`);
  if (expectedTurns === 16) assert.ok(users.some((item) => item.text.includes('SYNTH_RECALL_PROBE')), `${stage}: final recall probe`);
  assert.ok(assistants.slice(0, -1).every((item) => item.text === 'DETERMINISTIC_LOCAL_E2E_REPLY'), `${stage}: assistant response pairs`);
  if (expectedTurns === 16) assert.ok(assistants.at(-1)?.text.startsWith('SYNTH_RECALL_CONFIRMED'), `${stage}: recall response`);
  return users;
}

if (process.argv.includes('--history-only')) {
  const token = await authenticate();
  await assertCompleteHistory(token, 'post-restart');
  console.log('LOCAL_E2E_RESTART_HISTORY=PASS');
  process.exit(0);
}

if (process.argv.includes('--recall-only')) {
  const token = await authenticate();
  const priorUsers = await assertCompleteHistory(token, 'pre-recall', 15);
  const sourceTurn = priorUsers.findIndex((item) => item.text.includes('SYNTH_CITY_ZEPHYRBAY'));
  assert.equal(sourceTurn, 0, 'recall source must be the first user turn');
  assert.equal(priorUsers.length - sourceTurn - 1, 14, 'source fact must be outside the latest 12 user messages');
  console.log('LOCAL_E2E_RECALL_GAP=PASS intermediate_user_messages=14');
  const response = await request(`${api}/messages/creator/${creator}`, {
    method: 'POST',
    token,
    body: {
      clientMessageId: randomUUID(),
      text: 'SYNTH_RECALL_PROBE: Return to our first topic. What city did I mention, and what is the current meeting date and time? The older date should be treated as historical.',
    },
  });
  assert.equal(response.status, 201, JSON.stringify(response.data));
  assert.equal(response.data?.status, 'completed');
  assert.equal(response.data?.response?.text,
    'SYNTH_RECALL_CONFIRMED city=ZephyrBay meeting=2026-10-23_19:15 history=2026-10-22_18:40');
  await assertCompleteHistory(token, 'post-recall', 16);
  console.log('LOCAL_E2E_MEMORY_RECALL_RESPONSE=PASS');
  process.exit(0);
}

const unauthenticated = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  body: { clientMessageId: randomUUID(), text: 'synthetic unauthorized check' },
});
assert.equal(unauthenticated.status, 401);

const token = await authenticate();
await setFixtureState({ ageAssuranceRequired: true, longTermMemory: true });
const ageGate = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  token,
  body: { clientMessageId: randomUUID(), text: 'synthetic age gate check' },
});
assert.equal(ageGate.status, 403);

await setFixtureState({ ageAssuranceRequired: false, longTermMemory: false });
const noConsent = await request(`${api}/messages/creator/${creator}`, {
  method: 'POST',
  token,
  body: { clientMessageId: randomUUID(), text: 'synthetic no-memory-consent check' },
});
assert.equal(noConsent.status, 201, JSON.stringify(noConsent.data));
assert.equal(noConsent.data?.response?.text, 'SYNTHETIC_AGATA_FALLBACK_REPLY');

await setFixtureState({ ageAssuranceRequired: false, longTermMemory: true });
const dialogue = [
  'SYNTH_CITY_ZEPHYRBAY: In this synthetic continuity test, I live in Zephyr Bay. My meeting is currently on 22 October 2026 at 18:40, marker SYNTH_OLD_2026-10-22_18:40.',
  'Correction: the meeting moved to 23 October 2026 at 19:15, marker SYNTH_CURRENT_2026-10-23_19:15. Keep the earlier date as history only.',
  'Topic switch 1: synthetic preference, I prefer quiet cafés for reading.',
  'Topic switch 2: synthetic planning, I want to learn watercolor painting this winter.',
  'Topic switch 3: tell me a short astronomy fact.',
  'Topic switch 4: I am comparing notebooks and fountain pens.',
  'Topic switch 5: discuss a fictional weekend walk beside the river.',
  'Topic switch 6: suggest a simple vegetarian lunch.',
  'Topic switch 7: I am sorting a bookshelf by genre.',
  'Topic switch 8: tell me about an imaginary botanical garden.',
  'Topic switch 9: I am thinking about a short train trip in a fictional country.',
  'Topic switch 10: describe a calm rainy afternoon.',
  'Topic switch 11: compare two fictional board games.',
  'Topic switch 12: share a small tip for learning a language.',
  'Topic switch 13: I am choosing a playlist for focused work.',
];

for (const [index, text] of dialogue.entries()) {
  const response = await request(`${api}/messages/creator/${creator}`, {
    method: 'POST',
    token,
    body: { clientMessageId: randomUUID(), text },
  });
  assert.equal(response.status, 201, `turn ${index + 1}: ${JSON.stringify(response.data)}`);
  assert.equal(response.data?.status, 'completed', `turn ${index + 1}`);
  assert.equal(response.data?.response?.text, 'DETERMINISTIC_LOCAL_E2E_REPLY', `turn ${index + 1}`);
}
console.log('LOCAL_E2E_15_TURN_DIALOGUE=PASS');

await assertCompleteHistory(token, 'pre-recall', 15);
console.log('LOCAL_E2E_AUTH_AGE_CONSENT_AND_WORKER_ROUTE=PASS');
console.log('LOCAL_E2E_GPOINT_API_EROS_HISTORY=PASS');
