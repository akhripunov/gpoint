import test from 'node:test';
import assert from 'node:assert/strict';
import { createRetryingGit } from './release-network-retry.mjs';

test('retries only the idempotent ledger fetch after a subprocess timeout', () => {
  let calls = 0;
  const git = createRetryingGit((args, input) => {
    calls += 1;
    if (calls === 1) throw Object.assign(new Error('timed out'), {code: 'ETIMEDOUT'});
    return 'fetched';
  }, {budgetSeconds: 5, statePath: null, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.equal(git(['fetch', '--quiet', 'origin', 'refs/heads/gpoint-release-ledger']), 'fetched');
  assert.equal(calls, 2);
});

test('does not retry authentication or other non-network failures', () => {
  let calls = 0;
  const failure = Object.assign(new Error('authentication failed'), {code: 'EACCES'});
  const git = createRetryingGit(() => { calls += 1; throw failure; },
    {budgetSeconds: 5, statePath: null, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.throws(() => git(['fetch', '--quiet', 'origin', 'refs/heads/gpoint-release-ledger']), error => error === failure);
  assert.equal(calls, 1);
});

test('retries a Git CLI transport failure reported through status and stderr', () => {
  let calls = 0;
  const git = createRetryingGit(() => {
    calls += 1;
    if (calls === 1) {
      throw Object.assign(new Error('git exited 128'), {
        status: 128,
        stderr: Buffer.from("fatal: unable to access 'https://github.com/akhripunov/gpoint': Failed to connect: Connection timed out"),
      });
    }
    return 'fetched';
  }, {budgetSeconds: 5, statePath: null, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.equal(git(['fetch', '--quiet', 'origin', 'refs/heads/gpoint-release-ledger']), 'fetched');
  assert.equal(calls, 2);
});

test('does not retry Git CLI authentication failures reported through stderr', () => {
  let calls = 0;
  const failure = Object.assign(new Error('git exited 128'), {
    status: 128,
    stderr: Buffer.from("fatal: Authentication failed for 'https://github.com/akhripunov/gpoint'"),
  });
  const git = createRetryingGit(() => { calls += 1; throw failure; },
    {budgetSeconds: 5, statePath: null, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.throws(() => git(['fetch', '--quiet', 'origin', 'refs/heads/gpoint-release-ledger']), error => error === failure);
  assert.equal(calls, 1);
});

test('never retries a ledger push because it mutates the remote ref', () => {
  let calls = 0;
  const failure = Object.assign(new Error('timed out'), {code: 'ETIMEDOUT'});
  const git = createRetryingGit(() => { calls += 1; throw failure; },
    {budgetSeconds: 5, statePath: null, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.throws(() => git(['push', 'origin', 'candidate:refs/heads/gpoint-release-ledger']), error => error === failure);
  assert.equal(calls, 1);
});

test('stops retrying when the shared network budget expires', () => {
  let calls = 0, now = 0;
  const git = createRetryingGit((args, input, timeoutMs) => {
    calls += 1;
    now += timeoutMs / 1000;
    throw Object.assign(new Error('timed out'), {code: 'ETIMEDOUT'});
  }, {budgetSeconds: 1, statePath: null, clock: () => now, sleep: () => {}, jitter: () => 0, emit: () => {}});

  assert.throws(() => git(['fetch', '--quiet', 'origin', 'refs/heads/gpoint-release-ledger']), /retry budget exhausted/);
  assert.equal(calls, 1);
});
