import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {retryNetworkRead} from './release-network-read.mjs';

function codedError(code, message = 'private network detail') {
  return new TypeError('fetch failed with private details', {
    cause: Object.assign(new Error(message), {code}),
  });
}

function tempState() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-network-read-'));
  return {directory, statePath: path.join(directory, 'budget.state')};
}

test('read helper retries allowlisted fetch transport codes and omits error details from logs', async () => {
  const logs = [];
  let calls = 0;
  const value = await retryNetworkRead('test_get', async timeoutMs => {
    calls++;
    assert.ok(timeoutMs > 0);
    if (calls === 1) throw codedError('UND_ERR_CONNECT_TIMEOUT');
    return 'ok';
  }, {budgetSeconds: 2, sleep: async () => {}, jitter: () => 0,
    emit: message => logs.push(message)});
  assert.equal(value, 'ok');
  assert.equal(calls, 2);
  assert.ok(logs.some(message => message.includes('NETWORK_RETRYING')));
  assert.ok(logs.every(message => !message.includes('private')));
});

test('read helper fails immediately for generic type, HTTP, certificate, JSON, and integrity errors', async () => {
  const failures = [
    new TypeError('fetch failed'),
    Object.assign(new Error('untyped timeout'), {name: 'TimeoutError'}),
    Object.assign(new Error('HTTP 403'), {status: 403}),
    codedError('CERT_HAS_EXPIRED'),
    new SyntaxError('invalid JSON'),
    new Error('response exceeded size limit'),
  ];
  for (const failure of failures) {
    let calls = 0;
    await assert.rejects(retryNetworkRead('test_get', async () => {
      calls++;
      throw failure;
    }, {budgetSeconds: 2, sleep: async () => {}, jitter: () => 0, emit: () => {}}));
    assert.equal(calls, 1);
  }
});

test('fatal nested HTTP, certificate, schema, and gate causes take precedence over transient codes', async () => {
  const cert = Object.assign(new Error('private certificate detail'), {code: 'CERT_HAS_EXPIRED'});
  const certName = Object.assign(new Error('private certificate detail'), {code: 'ERR_TLS_CERT_ALTNAME_INVALID'});
  const failures = [
    new TypeError('fetch failed', {cause: Object.assign(new Error('socket'), {code: 'UND_ERR_SOCKET', cause: cert})}),
    new TypeError('fetch failed', {cause: Object.assign(new Error('socket'), {code: 'ECONNRESET', cause: certName})}),
    Object.assign(new Error('private HTTP response'), {status: 403,
      cause: Object.assign(new Error('timeout'), {code: 'ETIMEDOUT'})}),
    Object.assign(new Error('private HTTP response'), {statusCode: 503,
      cause: Object.assign(new Error('timeout'), {code: 'UND_ERR_CONNECT_TIMEOUT'})}),
    new SyntaxError('invalid JSON', {cause: Object.assign(new Error('socket'), {code: 'ECONNRESET'})}),
    Object.assign(new Error('gate failed'), {name: 'ReconciliationGateError',
      cause: Object.assign(new Error('socket'), {code: 'UND_ERR_SOCKET'})}),
    Object.assign(new Error('integrity failed'), {name: 'IntegrityError',
      cause: Object.assign(new Error('socket'), {code: 'UND_ERR_SOCKET'})}),
  ];
  for (const failure of failures) {
    let calls = 0;
    await assert.rejects(retryNetworkRead('test_get', async () => {
      calls++;
      throw failure;
    }, {budgetSeconds: 2, sleep: async () => {}, jitter: () => 0, emit: () => {}}));
    assert.equal(calls, 1);
  }
});

test('read helper accepts the runtime timeout code and rejects invalid budget values', async () => {
  let calls = 0;
  const timeout = Object.assign(new Error('request timed out'), {name: 'TimeoutError', code: 23});
  assert.equal(await retryNetworkRead('test_get', async () => {
    if (calls++ === 0) throw timeout;
    return 'ok';
  }, {budgetSeconds: 2, sleep: async () => {}, jitter: () => 0, emit: () => {}}), 'ok');
  for (const budgetSeconds of [0, -1, NaN, Infinity, '2']) {
    await assert.rejects(retryNetworkRead('test_get', async () => 'ok', {budgetSeconds}), /finite positive/);
  }
});

test('read helper rejects a depleted shared budget before making a request', async () => {
  const {directory, statePath} = tempState();
  try {
    fs.writeFileSync(statePath, '1800.000000\n');
    let calls = 0;
    await assert.rejects(retryNetworkRead('test_get', async () => {
      calls++;
      return 'should not run';
    }, {statePath, budgetSeconds: 1800, emit: () => {}}), /budget exhausted/);
    assert.equal(calls, 0);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('read helper passes the smaller remaining shared budget deadline to the request', async () => {
  const {directory, statePath} = tempState();
  try {
    fs.writeFileSync(statePath, '0.9\n');
    let timeoutMs;
    await retryNetworkRead('test_get', async remainingMs => {
      timeoutMs = remainingMs;
      return 'ok';
    }, {statePath, budgetSeconds: 1, now: () => 0, emit: () => {}});
    assert.ok(timeoutMs > 0 && timeoutMs <= 100);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('read helper bounds attempts by the monotonic deadline', async () => {
  const {directory, statePath} = tempState();
  try {
    let now = 0;
    let calls = 0;
    await assert.rejects(retryNetworkRead('test_get', async timeoutMs => {
      calls++;
      assert.ok(timeoutMs > 0);
      now += 600;
      throw codedError('UND_ERR_SOCKET');
    }, {statePath, budgetSeconds: 1, now: () => now, sleep: async ms => { now += ms; },
      jitter: () => 0, emit: () => {}}), /budget exhausted/);
    assert.equal(calls, 2);
    assert.ok(Number(fs.readFileSync(statePath, 'utf8').trim()) >= 1);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('shared retry budget accumulates elapsed time once across sequential reads', async () => {
  const {directory, statePath} = tempState();
  try {
    let now = 100;
    for (let operation = 0; operation < 2; operation++) {
      let calls = 0;
      await retryNetworkRead('test_get', async () => {
        calls++;
        now += 1_000;
        if (calls === 1) throw codedError('UND_ERR_SOCKET');
        return 'ok';
      }, {statePath, budgetSeconds: 30, now: () => now,
        sleep: async ms => { now += ms; }, jitter: () => 0, emit: () => {}});
      assert.equal(calls, 2);
    }
    const spent = Number(fs.readFileSync(statePath, 'utf8').trim());
    assert.ok(spent >= 4 && spent < 4.01, `unexpected cumulative spend ${spent}`);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('shared retry budget does not reload a lower stale state value', async () => {
  const {directory, statePath} = tempState();
  try {
    let now = 10;
    let calls = 0;
    await retryNetworkRead('test_get', async timeoutMs => {
      calls++;
      assert.ok(timeoutMs > 0);
      now += 500;
      if (calls === 1) throw codedError('UND_ERR_SOCKET');
      return 'ok';
    }, {statePath, budgetSeconds: 10, now: () => now, sleep: async ms => { now += ms; },
      jitter: () => 0, emit: () => {}});
    const firstSpend = Number(fs.readFileSync(statePath, 'utf8').trim());
    fs.writeFileSync(statePath, '0\n');
    let remaining;
    await retryNetworkRead('second_get', async timeoutMs => { remaining = timeoutMs; return 'ok'; },
      {statePath, budgetSeconds: 10, now: () => now, emit: () => {}});
    assert.ok(remaining < 9_500);
    assert.ok(Number(fs.readFileSync(statePath, 'utf8').trim()) >= firstSpend);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('concurrent reads sharing one state path reserve retry budget sequentially', async () => {
  const {directory, statePath} = tempState();
  try {
    let now = 0;
    let active = 0;
    let maxActive = 0;
    let firstCalls = 0;
    const operation = id => async () => {
      active++;
      maxActive = Math.max(maxActive, active);
      now += 500;
      try {
        if (id === 'first' && firstCalls++ === 0) throw codedError('UND_ERR_SOCKET');
        return id;
      } finally { active--; }
    };
    const options = {statePath, budgetSeconds: 10, now: () => now,
      sleep: async ms => { now += ms; }, jitter: () => 0, emit: () => {}};
    const [first, second] = await Promise.all([
      retryNetworkRead('first_get', operation('first'), options),
      retryNetworkRead('second_get', operation('second'), options),
    ]);
    assert.deepEqual([first, second], ['first', 'second']);
    assert.equal(maxActive, 1);
    assert.ok(Number(fs.readFileSync(statePath, 'utf8').trim()) >= 1);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
