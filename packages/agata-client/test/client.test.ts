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

test('worker queue status uses authenticated read-only GET and validates a GPOINT-only content-free shape', async () => {
  const requests: Array<{ path: string; method: string | undefined; authorization: string | null; body: string | null }> = [];
  const client = new AgataClient({
    baseUrl: 'https://agata.internal/', token: 'internal-service-test-token',
    fetchImpl: async (input, init) => {
      requests.push({
        path: new URL(String(input)).pathname,
        method: init?.method,
        authorization: new Headers(init?.headers).get('authorization'),
        body: init?.body ? String(init.body) : null,
      });
      return new Response(JSON.stringify({
        source_id: 'agata-worker-queue-status-v1:gpoint', lane: 'GPOINT',
        pending_jobs: 2, running_jobs: 1, snapshot_at: '2026-10-14T05:00:00Z',
      }), { status: 200 });
    },
  });
  const status = await client.getWorkerQueueStatus('GPOINT');
  assert.equal(status.lane, 'GPOINT');
  assert.deepEqual(requests, [{
    path: '/internal/v1/worker/queue-status', method: 'GET',
    authorization: 'Bearer internal-service-test-token', body: null,
  }]);

  const invalid = new AgataClient({
    baseUrl: 'https://agata.internal', token: 'internal-service-test-token',
    fetchImpl: async () => new Response(JSON.stringify({
      source_id: 'agata-worker-queue-status-v1:gsales', lane: 'GSALES',
      pending_jobs: 2, running_jobs: 1, snapshot_at: '2026-10-14T05:00:00Z', private_payload: 'must-not-escape',
    }), { status: 200 }),
  });
  await assert.rejects(
    invalid.getWorkerQueueStatus('GPOINT'),
    (error: unknown) => error instanceof AgataServiceError && error.status === 502 && error.code === 'worker_queue_status_invalid',
  );
});

test('chat read methods authenticate and encode user, character, cursor and limits', async () => {
  const requests: Array<{ url: URL; headers: Headers }> = [];
  const client = new AgataClient({
    baseUrl: 'http://agata-api:8000', token: 'internal-test-token',
    fetchImpl: async (input, init) => {
      requests.push({ url: new URL(String(input)), headers: new Headers(init?.headers) });
      return new Response(JSON.stringify({ items: [], nextCursor: null }), { status: 200 });
    },
  });
  await client.getChatState('user/a');
  await client.listConversations('user/a', { limit: 30, cursor: 'cursor +/=' });
  await client.listMessages('user/a', 'character/a', { limit: 50, cursor: 'next' });
  assert.deepEqual(requests.map(({ url }) => url.pathname), [
    '/internal/v1/users/user%2Fa/chat-state',
    '/internal/v1/users/user%2Fa/conversations',
    '/internal/v1/users/user%2Fa/characters/character%2Fa/messages',
  ]);
  assert.equal(requests[1].url.searchParams.get('cursor'), 'cursor +/=');
  assert.equal(requests[2].url.searchParams.get('limit'), '50');
  assert.ok(requests.every(({ headers }) => headers.get('authorization') === 'Bearer internal-test-token'));
});

test('chat read methods do not retry permanent errors', async () => {
  let calls = 0;
  const client = new AgataClient({
    baseUrl: 'http://agata-api:8000', token: 'internal-test-token',
    fetchImpl: async () => { calls += 1; return new Response(JSON.stringify({ detail: { code: 'unauthorized' } }), { status: 401 }); },
  });
  await assert.rejects(client.getChatState('u'), (error: unknown) => error instanceof AgataServiceError && error.status === 401);
  assert.equal(calls, 1);
});

test('unread summary and bounded acknowledgements use authenticated scoped paths', async () => {
  const calls: Array<{path:string; method:string; body:any}> = [];
  const client=new AgataClient({baseUrl:'http://agata.test',token:'internal-test-token',fetchImpl:async (input,init)=>{
    assert.equal(new Headers(init?.headers).get('authorization'),'Bearer internal-test-token');
    const path=new URL(String(input)).pathname;
    calls.push({path,method:init?.method??'',body:init?.body?JSON.parse(String(init.body)):null});
    return new Response(JSON.stringify(path.endsWith('/read')?{acknowledgedMessageIds:['message-1']}:{items:[{gpointCharacterId:'creator-1',unreadCount:2}]}));
  }});
  assert.deepEqual(await client.unreadMessages('user/a'),{items:[{gpointCharacterId:'creator-1',unreadCount:2}]});
  assert.deepEqual(await client.markMessagesRead('user/a','creator/b',['message-1']),{acknowledgedMessageIds:['message-1']});
  assert.deepEqual(calls,[{path:'/internal/v1/users/user%2Fa/unread',method:'GET',body:null},{path:'/internal/v1/users/user%2Fa/characters/creator%2Fb/messages/read',method:'POST',body:{messageIds:['message-1']}}]);
});
