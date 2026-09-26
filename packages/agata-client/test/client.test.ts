import assert from 'node:assert/strict';
import test from 'node:test';
import { AgataClient, AgataServiceError } from '../src/index.ts';

test('respond retries a transient error once using the stable message id', async () => {
  let calls = 0;
  const client = new AgataClient({
    baseUrl: 'http://agata-api:8000',
    token: 'internal-test-token',
    fetchImpl: async (_input, init) => {
      calls += 1;
      assert.equal(init?.method, 'POST');
      assert.equal(JSON.parse(String(init?.body)).message_id, 'msg-1');
      return calls === 1
        ? new Response(JSON.stringify({ code: 'temporary' }), { status: 503 })
        : new Response(JSON.stringify({ request_id: 'req-1', conversation_id: 'c', message_id: 'a', character_id: 'm', text: 'ok' }), { status: 200 });
    },
  });
  const output = await client.respond({ request_id: 'req-1', gpoint_user_id: 'u', gpoint_character_id: 'm', conversation_id: 'c', message_id: 'msg-1', text: 'hello' });
  assert.equal(output.text, 'ok');
  assert.equal(calls, 2);
});

test('does not retry permanent authorization failures', async () => {
  let calls = 0;
  const client = new AgataClient({
    baseUrl: 'http://agata-api:8000',
    token: 'internal-test-token',
    fetchImpl: async () => { calls += 1; return new Response(JSON.stringify({ detail: { code: 'unauthorized' } }), { status: 401 }); },
  });
  await assert.rejects(client.respond({ request_id: 'r', gpoint_user_id: 'u', gpoint_character_id: 'm', conversation_id: 'c', message_id: 'msg', text: 'hello' }), (error: unknown) => error instanceof AgataServiceError && error.status === 401 && error.code === 'unauthorized');
  assert.equal(calls, 1);
});

test('live and ready use the health endpoints without a request body', async () => {
  const paths: string[] = [];
  const client = new AgataClient({
    baseUrl: 'http://agata-api:8000/',
    token: 'internal-test-token',
    fetchImpl: async (input, init) => {
      paths.push(new URL(String(input)).pathname);
      assert.equal(init?.method, 'GET');
      assert.equal(init?.body, undefined);
      assert.ok(init?.signal instanceof AbortSignal);
      return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
    },
  });
  await client.live();
  await client.ready();
  assert.deepEqual(paths, ['/health/live', '/health/ready']);
});
