import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { canCreateProductionRef, checkAgataEffectiveConfig, checkArtifactParity, checkEffectiveConfig, classifyReleaseFailure, cleanupLocalPackageCaches, evaluateSafetyGates, failureEvidence, isTransientGitHubTransportFailure, localDiskDecision, pushProductionTag, recoveryArtifactIdentities, recoveryRuntimeFailures, releaseStateAfterError, remoteProof, runLocalDiskPreflight, runRemoteShaList, runWithBoundedGitHubRetries, shouldFinalizeRecovery, validateCandidate, validateMacWorkerStatus, verifiedRuntimeState } from './release-gpoint.mjs';
import { agataInternalConfigProvisionCommand, agataPreSwitchReadinessCommand, agataProviderRuntimeCommand, candidateBuildCacheCleanupCommand, classifyPostSwitchState, cleanupTemporaryWorktree, createSourceArchive, databaseNetworkArgs, detectorNeedsRebuild, detectorSourceHash, exactCheckout, GPOINT_DATABASE_NETWORK, migrationNeedsDeploy, parseAgataRuntimeIdentityLine, planReleaseRetention, productionPreflightCommand, resolveRunnerPath, runCleanupCommand, runRemoteCommand, runWithPreExecRetry, shouldReuseCandidateImage, SOURCE_ARCHIVE_EXCLUDES, upload, validateAgataProviderConfig, validateAgataProviderMode, validateAgataRuntimeIdentity, validateRuntimeIdentity } from './release-gpoint-vps-adapter.mjs';

test('temporary worktree cleanup uses git metadata removal when available', () => {
  const calls = [];
  const tempWorktree = path.join(os.tmpdir(), 'gpoint-release-test');
  const result = cleanupTemporaryWorktree(tempWorktree, {
    removeWorktree: target => calls.push(['git', target]),
    removeDirectory: target => calls.push(['fs', target]),
    pruneWorktreeMetadata: () => calls.push(['prune']),
  });
  assert.equal(result, 'GIT_WORKTREE_REMOVE');
  assert.deepEqual(calls, [['git', tempWorktree]]);
});

test('temporary worktree cleanup falls back to its exact directory and prunes stale metadata', () => {
  const calls = [];
  const tempWorktree = path.join(os.tmpdir(), 'gpoint-release-test');
  const result = cleanupTemporaryWorktree(tempWorktree, {
    removeWorktree: target => { calls.push(['git', target]); throw new Error('worktree unavailable'); },
    removeDirectory: target => calls.push(['fs', target]),
    pruneWorktreeMetadata: () => calls.push(['prune']),
  });
  assert.equal(result, 'FILESYSTEM_FALLBACK');
  assert.deepEqual(calls, [
    ['git', tempWorktree],
    ['fs', tempWorktree],
    ['prune'],
  ]);
});

test('temporary worktree cleanup refuses paths outside recognized GPoint temp worktrees', () => {
  let touched = false;
  assert.throws(() => cleanupTemporaryWorktree(path.join(os.tmpdir(), 'user-data'), {
    removeWorktree: () => { touched = true; },
    removeDirectory: () => { touched = true; },
  }), /refusing to clean a path/);
  assert.equal(touched, false);
});

const sha = '4d99e0da856d2ffe174c54d7ae7f2f971ab462f5';
const artifacts = { api: { sourceSha: sha }, worker: { sourceSha: sha }, bot: { sourceSha: sha }, miniapp: { sourceSha: sha } };
const config = { api: sha, worker: sha, bot: sha, miniapp: sha };

test('local disk gate passes at the 15 GiB target', () => {
  const warnings = [];
  const result = runLocalDiskPreflight({ freeBytes: () => 16 * 1024 ** 3, cleanup: () => assert.fail('cleanup should not run at target'), warn: message => warnings.push(message) });
  assert.equal(result.gate, 'PASS');
  assert.equal(result.cleanupAttempted, false);
  assert.deepEqual(warnings, []);
});

test('local disk gate performs one safe cleanup and warns between 10 and 15 GiB', () => {
  const free = [12, 12].map(value => value * 1024 ** 3);
  const warnings = [];
  let cleanups = 0;
  const result = runLocalDiskPreflight({ freeBytes: () => free.shift(), cleanup: () => { cleanups += 1; return [{ command: 'pnpm', ok: true }, { command: 'npm', ok: true }]; }, warn: message => warnings.push(message) });
  assert.equal(result.gate, 'WARNING');
  assert.equal(result.cleanupAttempted, true);
  assert.equal(cleanups, 1);
  assert.match(warnings[0], /LOCAL_DISK_GATE=WARNING/);
});

test('local disk gate fails closed below 10 GiB after exactly one cleanup', () => {
  const free = [8, 9].map(value => value * 1024 ** 3);
  let cleanups = 0;
  assert.throws(() => runLocalDiskPreflight({ freeBytes: () => free.shift(), cleanup: () => { cleanups += 1; return []; }, warn: () => {} }), /requires at least 10 GiB/);
  assert.equal(cleanups, 1);
});

test('local package-cache cleanup is limited to bounded pnpm/npm cache commands', () => {
  const calls = [];
  const results = cleanupLocalPackageCaches((...args) => { calls.push(args); return { status: 0 }; });
  assert.deepEqual(calls.map(([binary, args]) => [binary, args]), [
    ['pnpm', ['store', 'prune']],
    ['npm', ['cache', 'clean', '--force']],
  ]);
  assert.ok(calls.every(([, , options]) => options.timeout === 30_000));
  assert.deepEqual(results, [{ command: 'pnpm', ok: true }, { command: 'npm', ok: true }]);
  assert.equal(localDiskDecision(9 * 1024 ** 3).gate, 'FAIL');
});

test('remote SHA verifier accepts the exact main SHA through the canonical transport', () => {
  const calls = [];
  const proof = remoteProof(sha, {
    runRemote: options => {
      calls.push(options);
      return `${sha}\trefs/heads/main\n${sha}\trefs/tags/candidate`;
    },
  });
  assert.equal(proof.mainSha, sha);
  assert.deepEqual(calls, [{ timeoutMs: 15_000 }]);
});

test('transient transport failure retries once then accepts exact main SHA', () => {
  const calls = [];
  const delays = [];
  const proof = remoteProof(sha, {
    runRemote: options => {
      calls.push(options);
      if (calls.length === 1) throw new Error('Connection timed out during banner exchange');
      return `${sha}\trefs/heads/main`;
    },
    sleep: milliseconds => delays.push(milliseconds),
    jitter: (_low, high) => high,
  });
  assert.equal(proof.mainSha, sha);
  assert.equal(calls.length, 2);
  assert.deepEqual(delays, [1_000]);
});

test('canonical SSH-over-443 command inherits the configured identity and strict verification', () => {
  const previousIdentity = process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE;
  const previousCommand = process.env.GIT_SSH_COMMAND;
  process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE = '/local/known github key';
  delete process.env.GIT_SSH_COMMAND;
  try {
  runRemoteShaList({
    runner: (file, args, options) => {
      assert.equal(file, 'git');
      assert.deepEqual(args, ['ls-remote', 'origin', 'refs/heads/main', 'refs/tags/*']);
      assert.equal(options.timeout, 15_000);
      assert.match(options.env.GIT_SSH_COMMAND, /HostName=ssh\.github\.com/);
      assert.match(options.env.GIT_SSH_COMMAND, /HostKeyAlias=github\.com/);
      assert.match(options.env.GIT_SSH_COMMAND, /StrictHostKeyChecking=yes/);
      assert.match(options.env.GIT_SSH_COMMAND, /ConnectionAttempts=1/);
      assert.match(options.env.GIT_SSH_COMMAND, /ConnectTimeout=8/);
      assert.match(options.env.GIT_SSH_COMMAND, /-i "\/local\/known github key"/);
      return `${sha}\trefs/heads/main`;
    },
  });
  } finally {
    if (previousIdentity === undefined) delete process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE;
    else process.env.GPOINT_GITHUB_SSH_IDENTITY_FILE = previousIdentity;
    if (previousCommand === undefined) delete process.env.GIT_SSH_COMMAND;
    else process.env.GIT_SSH_COMMAND = previousCommand;
  }
});

test('remote SHA mismatch fails closed without retry', () => {
  const calls = [];
  assert.throws(() => remoteProof(sha, {
    runRemote: options => {
      calls.push(options);
      return `${'0'.repeat(40)}\trefs/heads/main`;
    },
  }), /candidate SHA is not exact origin\/main/);
  assert.equal(calls.length, 1);
});

test('transient remote outage expires as NETWORK_UNAVAILABLE without exposing secrets', () => {
  const calls = [];
  const delays = [];
  let now = 0;
  assert.throws(() => remoteProof(sha, {
    runRemote: options => {
      calls.push(options);
      throw Object.assign(new Error('worker-secret-token ssh: Could not resolve hostname github.com'), {
        stderr: 'worker-secret-token ssh: Could not resolve hostname github.com',
        code: 'ETIMEDOUT',
      });
    },
    sleep: milliseconds => { delays.push(milliseconds); now += milliseconds; },
    now: () => now,
    jitter: (_low, high) => high,
    budgetMs: 3_500,
    emit: () => {},
  }), error => {
    assert.match(error.message, /NETWORK_UNAVAILABLE/);
    assert.doesNotMatch(error.message, /worker-secret-token/);
    return true;
  });
  assert.equal(calls.length, 3);
  assert.deepEqual(delays, [1_000, 2_000, 500]);
});

test('auth and host-key failures are not retried; UNKNOWN remote SHA is rejected', () => {
  const calls = [];
  assert.throws(() => remoteProof(sha, {
    runRemote: options => {
      calls.push(options);
      throw new Error('Permission denied (publickey)');
    },
  }), /non-transient error/);
  assert.equal(calls.length, 1);
  assert.equal(isTransientGitHubTransportFailure(new Error('Host key verification failed')), false);
  let retries = 0;
  assert.throws(() => runWithBoundedGitHubRetries(() => { retries += 1; throw new Error('Host key verification failed'); }, { sleep: () => assert.fail('host-key failure must not retry') }), /Host key verification failed/);
  assert.equal(retries, 1);
  assert.throws(() => remoteProof(sha, { runRemote: () => `${sha}\trefs/tags/not-main` }), /origin\/main could not be independently verified/);
});

test('specific DNS transport errors outrank Git generic remote-read footer', () => {
  const exactError = new Error([
    'ssh: Could not resolve hostname ssh.github.com: nodename nor servname provided, or not known',
    'fatal: Could not read from remote repository.',
  ].join('\n'));
  assert.equal(isTransientGitHubTransportFailure(exactError), true);
  assert.equal(isTransientGitHubTransportFailure(new Error('ssh: Could not resolve hostname ssh.github.com: nodename nor servname provided, or not known\\nfatal: Could not read from remote repository.')), true);
  assert.equal(isTransientGitHubTransportFailure(new Error('ssh: nodename nor servname provided\\nfatal: Could not read from remote repository.')), true);
  assert.equal(isTransientGitHubTransportFailure(new Error('Permission denied (publickey).\\nfatal: Could not read from remote repository.')), false);
  assert.equal(isTransientGitHubTransportFailure(new Error('Host key verification failed.')), false);
  assert.equal(isTransientGitHubTransportFailure(new Error('SSL certificate problem: unable to get local issuer certificate')), false);
  assert.equal(isTransientGitHubTransportFailure(new Error('OpenSSL SSL_ERROR_SYSCALL during TLS read')), true);
  assert.equal(isTransientGitHubTransportFailure(new Error('candidate SHA mismatch')), false);
  assert.equal(isTransientGitHubTransportFailure(new Error('fatal: Could not read from remote repository.')), false);
});

test('DNS transport error followed by success retries and verifies exact main SHA', () => {
  const calls = [];
  const delays = [];
  const proof = remoteProof(sha, {
    runRemote: () => {
      calls.push(calls.length + 1);
      if (calls.length === 1) throw new Error('ssh: Could not resolve hostname ssh.github.com: nodename nor servname provided, or not known\\nfatal: Could not read from remote repository.');
      return `${sha}\trefs/heads/main`;
    },
    sleep: milliseconds => delays.push(milliseconds),
    jitter: (_low, high) => high,
  });
  assert.equal(proof.mainSha, sha);
  assert.equal(calls.length, 2);
  assert.deepEqual(delays, [1_000]);
});

test('GitHub recovery and a later SSH outage share one cumulative retry budget', () => {
  const statePath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-retry-budget-')), 'spent');
  const previous = process.env.NETWORK_RETRY_BUDGET_STATE;
  process.env.NETWORK_RETRY_BUDGET_STATE = statePath;
  let now = 0;
  const clock = () => now;
  const sleep = milliseconds => { now += milliseconds; };
  try {
    let githubCalls = 0;
    runWithBoundedGitHubRetries(() => {
      githubCalls += 1;
      if (githubCalls === 1) throw Object.assign(new Error('temporary network timeout'), { code: 'ETIMEDOUT' });
      return 'main-sha';
    }, { now: clock, sleep, jitter: () => 700, emit: () => {} });
    assert.equal(fs.readFileSync(statePath, 'utf8').trim(), '0.700000');

    let sshCalls = 0;
    runWithPreExecRetry(() => {
      sshCalls += 1;
      if (sshCalls === 1) throw Object.assign(new Error('temporary SSH banner timeout'), { classification: 'PRE_EXEC_TRANSPORT_FAILURE' });
      return 'read-only baseline';
    }, { now: clock, sleep, jitter: () => 800, emit: () => {} });
    assert.equal(fs.readFileSync(statePath, 'utf8').trim(), '1.500000');
    assert.equal(githubCalls, 2);
    assert.equal(sshCalls, 2);
  } finally {
    if (previous === undefined) delete process.env.NETWORK_RETRY_BUDGET_STATE;
    else process.env.NETWORK_RETRY_BUDGET_STATE = previous;
    fs.rmSync(path.dirname(statePath), { recursive: true, force: true });
  }
});

test('later SSH failure exhausts the remaining shared budget as NETWORK_UNAVAILABLE', () => {
  const statePath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-retry-budget-')), 'spent');
  const previous = process.env.NETWORK_RETRY_BUDGET_STATE;
  process.env.NETWORK_RETRY_BUDGET_STATE = statePath;
  fs.writeFileSync(statePath, '1799.5\n');
  let now = 0;
  try {
    assert.throws(() => runWithPreExecRetry(() => {
      throw Object.assign(new Error('temporary SSH banner timeout'), { classification: 'PRE_EXEC_TRANSPORT_FAILURE' });
    }, { now: () => now, sleep: milliseconds => { now += milliseconds; }, jitter: () => 500, emit: () => {} }),
    error => error.code === 'NETWORK_UNAVAILABLE');
  } finally {
    if (previous === undefined) delete process.env.NETWORK_RETRY_BUDGET_STATE;
    else process.env.NETWORK_RETRY_BUDGET_STATE = previous;
    fs.rmSync(path.dirname(statePath), { recursive: true, force: true });
  }
});

test('tag push retries transient connection loss after confirming remote tag is absent', () => {
  let pushes = 0;
  const lookups = [];
  const delays = [];
  const result = pushProductionTag('gpoint-production-test', sha, {
    push: () => { pushes += 1; if (pushes === 1) throw new Error('Connection reset by peer'); },
    lookup: () => { const value = lookups.length === 0 ? null : sha; lookups.push(value); return value; },
    sleep: milliseconds => delays.push(milliseconds),
  });
  assert.equal(result, sha);
  assert.equal(pushes, 2);
  assert.deepEqual(lookups, [null, sha]);
  assert.deepEqual(delays, [1_000]);
});

test('tag push connection loss is treated as success when exact remote tag already exists', () => {
  let pushes = 0;
  const result = pushProductionTag('gpoint-production-test', sha, {
    push: () => { pushes += 1; throw new Error('Connection closed during push'); },
    lookup: () => sha,
    sleep: () => assert.fail('verified remote tag does not need a retry'),
  });
  assert.equal(result, sha);
  assert.equal(pushes, 1);
});

test('three transient tag push failures stop after remote reconciliation checks', () => {
  let pushes = 0;
  let lookups = 0;
  const delays = [];
  assert.throws(() => pushProductionTag('gpoint-production-test', sha, {
    push: () => { pushes += 1; throw new Error('Connection timed out during banner exchange'); },
    lookup: () => { lookups += 1; return null; },
    sleep: milliseconds => delays.push(milliseconds),
  }), /three bounded transport attempts/);
  assert.equal(pushes, 3);
  assert.equal(lookups, 3);
  assert.deepEqual(delays, [1_000, 3_000]);
});

test('tag push fails closed when an existing remote tag points elsewhere', () => {
  let pushes = 0;
  assert.throws(() => pushProductionTag('gpoint-production-test', sha, {
    push: () => { pushes += 1; throw new Error('Connection closed during push'); },
    lookup: () => '0'.repeat(40),
    sleep: () => assert.fail('wrong remote tag must not be retried'),
  }), /different target/);
  assert.equal(pushes, 1);
});

test('tag push does not retry authentication, host-key, or rejected-push failures', () => {
  for (const [message, expected] of [
    ['Permission denied (publickey)', /authentication failed/],
    ['Host key verification failed', /host-key verification failed/],
    ['[rejected] non-fast-forward', /non-transient error/],
  ]) {
    let pushes = 0;
    assert.throws(() => pushProductionTag('gpoint-production-test', sha, {
      push: () => { pushes += 1; throw new Error(message); },
      lookup: () => assert.fail('non-transient failure must not query/retry'),
      sleep: () => assert.fail('non-transient failure must not sleep'),
    }), expected);
    assert.equal(pushes, 1);
  }
});

test('durable runner config is discovered in a fresh environment and explicit env wins', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-runner-config-test-'));
  const configFile = path.join(root, 'release-tools.env');
  fs.writeFileSync(configFile, 'GPOINT_REMOTE_RUNNER=$HOME/.local/bin/gpoint-vps-runner\nGPOINT_SCP_RUNNER=~/bin/gpoint-vps-scp-runner\n');
  try {
    assert.equal(resolveRunnerPath('GPOINT_REMOTE_RUNNER', { env: {}, configFile, home: root }), path.join(root, '.local/bin/gpoint-vps-runner'));
    assert.equal(resolveRunnerPath('GPOINT_SCP_RUNNER', { env: {}, configFile, home: root }), path.join(root, 'bin/gpoint-vps-scp-runner'));
    assert.equal(resolveRunnerPath('GPOINT_REMOTE_RUNNER', { env: { GPOINT_REMOTE_RUNNER: '/explicit/runner' }, configFile, home: root }), '/explicit/runner');
    assert.equal(resolveRunnerPath('UNKNOWN_RUNNER', { env: {}, configFile, home: root }), null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('GPoint SSH runner uses the dedicated identity and strict non-interactive options', () => {
  const runner = fs.readFileSync(new URL('./gpoint-vps-runner.sh', import.meta.url), 'utf8');
  for (const option of ['-i "$identity"', 'IdentitiesOnly=yes', 'StrictHostKeyChecking=yes', 'BatchMode=yes', 'ConnectTimeout=20', 'ConnectionAttempts=1']) {
    assert.ok(runner.includes(option), `missing SSH option ${option}`);
  }
  assert.ok(runner.includes('root@38.180.110.92'));
  assert.ok(!runner.includes('VPS_PASSWORD'));
  assert.ok(!runner.includes('expect'));
});

test('key-only runner discovers identity in a fresh process and preserves remote exit status/output', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-key-runner-test-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  const configDir = path.join(home, '.config/gpoint');
  const identity = path.join(home, '.ssh/gpoint_deploy_ed25519');
  const calls = path.join(root, 'ssh-call');
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.dirname(identity), { recursive: true });
  fs.writeFileSync(identity, 'test-private-key-placeholder');
  fs.writeFileSync(path.join(configDir, 'release-tools.env'), 'GPOINT_SSH_IDENTITY_FILE=$HOME/.ssh/gpoint_deploy_ed25519\n');
  const fakeSsh = path.join(bin, 'ssh');
  fs.writeFileSync(fakeSsh, `#!/bin/sh\nprintf '%s\\n' "$@" > "$SSH_CALL_FILE"\nprintf 'remote-out\\n'\nprintf 'remote-err\\n' >&2\nexit 17\n`, { mode: 0o700 });
  try {
    const result = spawnSync(path.join(path.dirname(new URL(import.meta.url).pathname), 'gpoint-vps-runner.sh'), [], {
      encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SSH_CALL_FILE: calls, REMOTE_CMD: 'exit 17' },
    });
    assert.equal(result.status, 17);
    assert.equal(result.stdout, 'remote-out\n');
    assert.equal(result.stderr, 'remote-err\n');
    const args = fs.readFileSync(calls, 'utf8').split(/\r?\n/);
    assert.ok(args.includes(identity));
    assert.ok(args.includes('IdentitiesOnly=yes'));
    assert.ok(args.includes('StrictHostKeyChecking=yes'));
    assert.ok(args.includes('BatchMode=yes'));
    assert.ok(args.includes('-E'));
    assert.ok(args.includes('-vv'));
    assert.ok(args.includes('exit 17'));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('missing SSH identity fails before invoking ssh and SCP uses the same key-only policy', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-key-scp-test-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  const configDir = path.join(home, '.config/gpoint');
  const calls = path.join(root, 'scp-call');
  const localFile = path.join(root, 'payload');
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.writeFileSync(localFile, 'harmless test payload');
  fs.writeFileSync(path.join(configDir, 'release-tools.env'), 'GPOINT_SSH_IDENTITY_FILE=$HOME/.ssh/missing-key\n');
  fs.writeFileSync(path.join(bin, 'scp'), `#!/bin/sh\nprintf '%s\\n' "$@" > "$SCP_CALL_FILE"\nexit 0\n`, { mode: 0o700 });
  const scpRunner = path.join(path.dirname(new URL(import.meta.url).pathname), 'gpoint-vps-scp-runner.sh');
  try {
    const missing = spawnSync(scpRunner, [], { encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SCP_CALL_FILE: calls, LOCAL_FILE: localFile, REMOTE_FILE: '/tmp/payload' } });
    assert.equal(missing.status, 78);
    assert.match(missing.stderr, /identity is missing or unreadable/);
    assert.equal(fs.existsSync(calls), false);

    const identity = path.join(home, '.ssh/existing-key');
    fs.mkdirSync(path.dirname(identity), { recursive: true });
    fs.writeFileSync(identity, 'test-private-key-placeholder');
    fs.writeFileSync(path.join(configDir, 'release-tools.env'), `GPOINT_SSH_IDENTITY_FILE=${identity}\n`);
    const result = spawnSync(scpRunner, [], { encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SCP_CALL_FILE: calls, LOCAL_FILE: localFile, REMOTE_FILE: '/tmp/payload' } });
    assert.equal(result.status, 0);
    const args = fs.readFileSync(calls, 'utf8');
    assert.ok(args.includes(identity));
    for (const option of ['IdentitiesOnly=yes', 'StrictHostKeyChecking=yes', 'BatchMode=yes', 'ConnectionAttempts=1']) assert.ok(args.includes(option));
    assert.ok(args.includes('root@38.180.110.92:/tmp/payload'));
    const largeTransfer = spawnSync(scpRunner, [], { encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SCP_CALL_FILE: calls, LOCAL_FILE: localFile, REMOTE_FILE: '/tmp/payload', GPOINT_SCP_COMMAND_TIMEOUT_SECONDS: '600' } });
    assert.equal(largeTransfer.status, 0);
    const unboundedTransfer = spawnSync(scpRunner, [], { encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SCP_CALL_FILE: calls, LOCAL_FILE: localFile, REMOTE_FILE: '/tmp/payload', GPOINT_SCP_COMMAND_TIMEOUT_SECONDS: '601' } });
    assert.equal(unboundedTransfer.status, 78);
    assert.match(unboundedTransfer.stderr, /outside the allowed range/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('key-only runner fails closed on rejected identity and host-key mismatch', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-key-failclosed-test-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  const configDir = path.join(home, '.config/gpoint');
  const identity = path.join(home, '.ssh/gpoint_deploy_ed25519');
  const modeFile = path.join(root, 'ssh-mode');
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.dirname(identity), { recursive: true });
  fs.writeFileSync(identity, 'test-private-key-placeholder');
  fs.writeFileSync(path.join(configDir, 'release-tools.env'), 'GPOINT_SSH_IDENTITY_FILE=$HOME/.ssh/gpoint_deploy_ed25519\n');
  fs.writeFileSync(path.join(bin, 'ssh'), `#!/bin/sh\nif [ "$(cat "$SSH_MODE_FILE")" = host ]; then echo 'Host key verification failed.' >&2; else echo 'root@38.180.110.92: Permission denied (publickey).' >&2; fi\nexit 255\n`, { mode: 0o700 });
  const runner = path.join(path.dirname(new URL(import.meta.url).pathname), 'gpoint-vps-runner.sh');
  try {
    for (const [mode, expected] of [['host', /Host key verification failed\./], ['auth', /Permission denied \(publickey\)/]]) {
      fs.writeFileSync(modeFile, mode);
      const result = spawnSync(runner, [], { encoding: 'utf8', env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, SSH_MODE_FILE: modeFile, REMOTE_CMD: 'hostname' } });
      assert.equal(result.status, 255);
      assert.match(result.stderr, expected);
      assert.doesNotMatch(result.stderr, /password:/i);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('unreachable SSH child is terminated by the wall-clock deadline', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-key-timeout-test-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  const configDir = path.join(home, '.config/gpoint');
  const identity = path.join(home, '.ssh/gpoint_deploy_ed25519');
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.dirname(identity), { recursive: true });
  fs.writeFileSync(identity, 'test-private-key-placeholder');
  fs.writeFileSync(path.join(configDir, 'release-tools.env'), 'GPOINT_SSH_IDENTITY_FILE=$HOME/.ssh/gpoint_deploy_ed25519\n');
  fs.writeFileSync(path.join(bin, 'ssh'), '#!/bin/sh\nexec sleep 10\n', { mode: 0o700 });
  const runner = path.join(path.dirname(new URL(import.meta.url).pathname), 'gpoint-vps-runner.sh');
  const started = Date.now();
  try {
    const result = spawnSync(runner, [], { encoding: 'utf8', timeout: 4000, env: { HOME: home, PATH: `${bin}:${process.env.PATH}`, GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS: '1', REMOTE_CMD: 'hostname' } });
    assert.equal(result.status, 124);
    assert.ok(Date.now() - started < 3500, 'SSH probe exceeded bounded wall-clock deadline');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('exact candidate gate blocks a nonexistent or malformed remote candidate', () => {
  assert.ok(validateCandidate({ candidateSha: 'not-a-sha', remoteSha: null }).length);
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: '0'.repeat(40) }).some(x => x.includes('remote')));
});
test('artifact parity blocks API SHA mismatch before migration', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, api: { sourceSha: '0'.repeat(40) } }), ['api artifact SHA mismatch']));
test('artifact parity blocks Worker SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, worker: { sourceSha: '0'.repeat(40) } }), ['worker artifact SHA mismatch']));
test('artifact parity blocks Bot SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, bot: { sourceSha: '0'.repeat(40) } }), ['bot artifact SHA mismatch']));
test('artifact parity blocks Mini App SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, miniapp: { sourceSha: '0'.repeat(40) } }), ['miniapp artifact SHA mismatch']));
test('dirty checkout blocks exact candidate gate', () => assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha, clean: false }).some(x => x.includes('dirty'))));
test('effective config parity blocks wrong API image', () => assert.deepEqual(checkEffectiveConfig(sha, { ...config, api: '0'.repeat(40) }), ['api effective image revision mismatch']));
test('effective config parity blocks wrong Mini App artifact', () => assert.deepEqual(checkEffectiveConfig(sha, { ...config, miniapp: '0'.repeat(40) }), ['miniapp effective artifact revision mismatch']));
test('migration failure is represented as a pre-switch block', () => assert.equal(validateCandidate({ candidateSha: sha, remoteSha: sha }).length, 0));
test('concurrent release and production drift are fail-closed conditions', () => {
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha }).length === 0);
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha, clean: false }).length > 0);
});
test('all parity gates pass only when every role shares the candidate', () => {
  assert.deepEqual(checkArtifactParity(sha, artifacts), []);
  assert.deepEqual(checkEffectiveConfig(sha, config), []);
});
test('backup failure blocks before migration', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, backupReady: false, recordedProductionSha: sha, liveProductionSha: sha }).includes('backup gate failed')));
test('migration failure blocks runtime switch', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, migrationOk: false, recordedProductionSha: sha, liveProductionSha: sha }).includes('migration gate failed')));
test('pre-switch Docker Compose preflight error is not misclassified as a runtime switch', () => {
  const failure = classifyReleaseFailure({ command: 'docker compose version failed', switchStarted: false });
  assert.equal(failure.switched, false);
  assert.equal(failure.releaseState, 'FAILED_PRE_SWITCH');
  assert.equal(failure.failureStage, 'PRE_SWITCH');
});
test('held release lock rejects a concurrent release', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, concurrent: true, recordedProductionSha: sha, liveProductionSha: sha }).includes('concurrent release lock is held')));
test('recorded/live production drift blocks', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: '0'.repeat(40), liveProductionSha: sha }).includes('live production SHA drift')));
test('remote production ref mismatch blocks completion', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: sha, liveProductionSha: sha, productionRefTarget: '0'.repeat(40) }).includes('production ref target mismatch')));
test('runtime identity blocks a previous image before release completion', () => {
  const imageId = 'sha256:' + 'a'.repeat(64);
  const previousImage = 'sha256:' + 'b'.repeat(64);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: previousImage, workerImage: imageId, botImage: imageId }), ['apiImage does not use candidate image']);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: imageId, workerImage: imageId, botImage: imageId }), []);
});

test('AGATA runtime requires candidate image parity and healthy API/worker', () => {
  const imageId = 'sha256:' + 'c'.repeat(64);
  const runtime = { imageId, buildSha: sha, apiImage: imageId, workerImage: imageId, apiRunning: 'true', apiHealth: 'healthy', workerRunning: 'true', workerHealth: 'healthy' };
  assert.deepEqual(validateAgataRuntimeIdentity(sha, runtime), []);
  assert.ok(validateAgataRuntimeIdentity(sha, { ...runtime, workerHealth: 'starting' }).some(x => x.includes('worker')));
  assert.ok(validateAgataRuntimeIdentity('0'.repeat(40), runtime).some(x => x.includes('build SHA')));
});

test('AGATA runtime identity parser maps uppercase snake case fields to camel case', () => {
  const imageId = 'sha256:' + 'c'.repeat(64);
  const runtime = parseAgataRuntimeIdentityLine(`AGATA_IMAGE_ID=${imageId} AGATA_BUILD_SHA=${sha} AGATA_API_IMAGE=${imageId} AGATA_API_RUNNING=true AGATA_API_HEALTH=healthy AGATA_WORKER_IMAGE=${imageId} AGATA_WORKER_RUNNING=true AGATA_WORKER_HEALTH=healthy AGATA_HEAD=agata0008`);
  assert.deepEqual(runtime, {
    imageId, buildSha: sha, apiImage: imageId, apiRunning: 'true', apiHealth: 'healthy',
    workerImage: imageId, workerRunning: 'true', workerHealth: 'healthy', head: 'agata0008',
  });
  assert.deepEqual(validateAgataRuntimeIdentity(sha, runtime), []);
});

const validAgataInternalConfig = {
  enabled: 'true', databaseUrlPresent: true, tenant: true, internalAuth: true, messageKey: true,
  requestHmac: true, retrievalHmac: true, responseHmac: true, shadowHmac: true,
};
const freshWorkerStatus = nowMs => ({
  enabled: true,
  queue_connectivity: 'reachable',
  last_heartbeat_at: new Date(nowMs - 2_000).toISOString(),
  last_poll_at: new Date(nowMs - 3_000).toISOString(),
});

test('AGATA disabled provider remains valid with local execution off and no LLM key', () => {
  const valid = { ...validAgataInternalConfig, provider: 'disabled', localEnabled: 'false', apiKeyPresent: false };
  assert.deepEqual(validateAgataProviderConfig(valid), []);
  assert.deepEqual(checkAgataEffectiveConfig({ ...valid, enabled: true }), []);
  const provision = agataInternalConfigProvisionCommand();
  const script = Buffer.from(provision.match(/printf '%s' '([^']+)'/)[1], 'base64').toString();
  assert.match(script, /provider=values\.get\('AGATA_LLM_PROVIDER'\) or 'disabled'/);
  assert.match(script, /values\['AGATA_LLM_PROVIDER'\]=provider/);
  assert.doesNotMatch(script, /values\['AGATA_LLM_PROVIDER'\]='disabled'/);
  assert.doesNotMatch(script, /AGATA_LLM_API_KEY=/);
});

test('mac_worker provider passes with local enablement, token, auth, fresh heartbeat/poll and healthy API', () => {
  const nowMs = Date.now();
  const config = {
    ...validAgataInternalConfig, provider: 'mac_worker', localEnabled: 'true', workerTokenPresent: true,
    agataApiHealthy: true, workerStatus: freshWorkerStatus(nowMs),
  };
  assert.deepEqual(validateAgataProviderConfig(config), []);
  assert.deepEqual(checkAgataEffectiveConfig({ ...config, enabled: true }), []);
  assert.deepEqual(validateMacWorkerStatus(config.workerStatus, { nowMs }), []);
});

test('mac_worker provider fails closed when local execution is disabled or token missing', () => {
  const nowMs = Date.now();
  const config = {
    ...validAgataInternalConfig, provider: 'mac_worker', localEnabled: 'true', workerTokenPresent: true,
    agataApiHealthy: true, workerStatus: freshWorkerStatus(nowMs),
  };
  assert.ok(validateAgataProviderConfig({ ...config, localEnabled: 'false' }).some(x => x.includes('not enabled')));
  assert.ok(validateAgataProviderConfig({ ...config, workerTokenPresent: false }).some(x => x.includes('token/config is missing')));
});

test('mac_worker provider fails closed for stale heartbeat/poll and auth failure', () => {
  const nowMs = Date.now();
  const config = {
    ...validAgataInternalConfig, provider: 'mac_worker', localEnabled: true, workerTokenPresent: true,
    agataApiHealthy: true, workerStatus: freshWorkerStatus(nowMs),
  };
  const stale = { ...config.workerStatus, last_poll_at: new Date(nowMs - 91_000).toISOString() };
  assert.ok(validateAgataProviderConfig({ ...config, workerStatus: stale }).some(x => x.includes('poll is missing or stale')));
  assert.ok(validateAgataProviderConfig({ ...config, workerStatus: { ...config.workerStatus, queue_connectivity: 'worker_gateway_auth_rejected' } }).some(x => x.includes('authentication/status')));
  assert.ok(validateAgataProviderConfig({ ...config, agataApiHealthy: false }).some(x => x.includes('AGATA API is not healthy')));
});

test('unsupported AGATA provider is rejected and release commands preserve provider mode', () => {
  const unsupported = { ...validAgataInternalConfig, provider: 'arbitrary_provider', localEnabled: false, apiKeyPresent: false };
  assert.ok(validateAgataProviderConfig(unsupported).some(x => x.includes('unsupported AGATA provider')));
  assert.ok(checkAgataEffectiveConfig({ provider: 'arbitrary_provider', enabled: true }).some(x => x.includes('unsupported')));
  const preflight = productionPreflightCommand();
  assert.match(preflight, /case "\$provider" in disabled\)/);
  assert.match(preflight, /mac_worker\)/);
  assert.match(preflight, /AGATA_PROVIDER_UNSUPPORTED/);
  assert.match(agataPreSwitchReadinessCommand(sha, 'mac_worker'), /AGATA_PROVIDER_VERIFIED=mac_worker/);
  assert.match(agataPreSwitchReadinessCommand(sha, 'disabled'), /AGATA_PROVIDER_VERIFIED=disabled/);
  assert.match(agataProviderRuntimeCommand('mac_worker'), /AGATA_WORKER_TOKEN_PRESENT/);
  assert.throws(() => agataPreSwitchReadinessCommand(sha, 'arbitrary_provider'), /supported AGATA provider/);
  for (const command of [preflight, agataPreSwitchReadinessCommand(sha, 'disabled'), agataPreSwitchReadinessCommand(sha, 'mac_worker'), agataProviderRuntimeCommand('mac_worker')]) {
    const syntax = spawnSync('/bin/sh', ['-n', '-c', command], { encoding: 'utf8' });
    assert.equal(syntax.status, 0, syntax.stderr);
  }
});

test('all disposable Prisma/AGATA database runners use shared_internal', () => {
  const adapter = fs.readFileSync(new URL('./release-gpoint-vps-adapter.mjs', import.meta.url), 'utf8');
  assert.equal(GPOINT_DATABASE_NETWORK, 'shared_internal');
  assert.equal(databaseNetworkArgs('gpoint-preflight'), '--network shared_internal --network-alias gpoint-preflight');
  assert.ok((adapter.match(/\$\{databaseNetworkArgs\(/g) ?? []).length >= 8);
  assert.doesNotMatch(adapter, /--network gpoint_app --network-alias gpoint-(?:preflight|migration|agata-preflight|agata-migration|agata-verify)/);
});

test('AGATA memory worker can resolve the shared PostgreSQL service', () => {
  const compose = fs.readFileSync(new URL('../infra/production/docker-compose.yml', import.meta.url), 'utf8');
  const worker = compose.match(/  agata-memory-worker:\n([\s\S]*?)(?=\n  [\w-]+:|\nnetworks:)/)?.[1];
  assert.ok(worker);
  assert.match(worker, /networks:\n\s+- gpoint_app\n\s+- shared_internal/);
});

test('production config and Mini App writes begin only after the runtime switch boundary', () => {
  const adapter = fs.readFileSync(new URL('./release-gpoint-vps-adapter.mjs', import.meta.url), 'utf8');
  const preparationStart = adapter.indexOf('const preparation = [');
  const deployStart = adapter.indexOf('const deploy = [', preparationStart);
  const prepareRun = adapter.indexOf("stage(timings, 'PRE_SWITCH_PREPARATION'", deployStart);
  const switchMarker = adapter.indexOf('switchStartMs=$(date +%s%3N)', deployStart);
  assert.ok(preparationStart >= 0 && deployStart > preparationStart && prepareRun > deployStart && switchMarker > deployStart);
  const preparation = adapter.slice(preparationStart, deployStart);
  const deploy = adapter.slice(deployStart, prepareRun);
  assert.doesNotMatch(preparation, /\/etc\/caddy\/Caddyfile|\/opt\/gpoint\/compose\/docker-compose\.yml|\/opt\/gpoint\/app\/miniapp/);
  assert.doesNotMatch(preparation, /sed -i[^\n]*\/opt\/gpoint\/env\/production\.env|>> \/opt\/gpoint\/env\/production\.env/);
  assert.match(deploy, /switchStartMs=\$\(date \+%s%3N\); set -e;[\s\S]*\/opt\/gpoint\/compose\/docker-compose\.yml/);
  assert.match(deploy, /\/opt\/gpoint\/app\/miniapp\.rollback-/);
  assert.match(adapter, /run\(preparation\.join\('; '\)\)/);
  assert.match(adapter, /run\(deploy\.join\('; '\)\)/);
});

test('release source archive excludes AppleDouble and Python cache artifacts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-source-archive-'));
  const checkout = path.join(root, 'checkout');
  const archive = path.join(root, 'source.tar.gz');
  fs.mkdirSync(path.join(checkout, 'services/agata/migrations/__pycache__'), { recursive: true });
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/env.py'), 'editable source');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/._env.py'), 'AppleDouble metadata');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/.DS_Store'), 'Finder metadata');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/__pycache__/env.cpython-312.pyc'), 'bytecode');
  try {
    createSourceArchive(checkout, archive);
    const entries = execFileSync('tar', ['-tzf', archive], { encoding: 'utf8' });
    assert.match(entries, /checkout\/services\/agata\/migrations\/env\.py/);
    assert.doesNotMatch(entries, /\._env\.py|\.DS_Store|__pycache__|\.pyc/);
    assert.ok(SOURCE_ARCHIVE_EXCLUDES.includes('--exclude=*/._*'));
    assert.ok(SOURCE_ARCHIVE_EXCLUDES.includes('--exclude=*/__pycache__'));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('production runtime image includes and builds AGATA client before API', () => {
  const dockerfile = fs.readFileSync(new URL('../infra/production/Gpoint.Dockerfile', import.meta.url), 'utf8');
  assert.match(dockerfile, /COPY packages\/agata-client\/package\.json packages\/agata-client\/package\.json/);
  assert.ok(dockerfile.indexOf('RUN pnpm --filter @gpoint/agata-client build') < dockerfile.indexOf('RUN pnpm --filter @gpoint/api build'));
});

test('nonzero compose after candidate switch enters switched-unverified recovery', () => {
  const image = 'sha256:' + 'a'.repeat(64);
  assert.equal(classifyPostSwitchState({ candidate: sha, runtime: { imageId: image, buildSha: sha, apiImage: image, workerImage: image, botImage: image }, composeExitCode: 1 }).state, 'SWITCHED_UNVERIFIED');
  assert.equal(releaseStateAfterError({ switched: true, runtimeVerified: false }), 'SWITCHED_UNVERIFIED');
});

test('mixed runtime is recovery-required and cannot create a ref', () => {
  const image = 'sha256:' + 'a'.repeat(64);
  const old = 'sha256:' + 'b'.repeat(64);
  const state = classifyPostSwitchState({ candidate: sha, runtime: { imageId: image, buildSha: sha, apiImage: image, workerImage: old, botImage: image }, composeExitCode: 0 });
  assert.equal(state.state, 'RECOVERY_REQUIRED');
  assert.equal(canCreateProductionRef({ releaseState: 'RECOVERY_REQUIRED', candidate: sha, actualSha: sha, gatesPass: true }), false);
});

test('recovery can finalize exactly once only after all gates pass', () => {
  assert.equal(shouldFinalizeRecovery({ releaseState: 'RECOVERY_REQUIRED', candidate: sha, actualSha: sha, gatesPass: true }), true);
  assert.equal(canCreateProductionRef({ releaseState: 'VERIFIED', candidate: sha, actualSha: sha, mixedRuntime: false, health: true, dbVerified: true }), true);
  assert.equal(canCreateProductionRef({ releaseState: 'VERIFIED', candidate: sha, actualSha: sha, mixedRuntime: true, health: true, dbVerified: true }), false);
});

test('recovery finalization rejects mixed or unhealthy runtime facts', () => {
  const facts = {
    LIVE_RELEASE_ENV_SHA: sha, LIVE_API_SHA: sha, LIVE_API_RUNNING: 'true', LIVE_API_HEALTH: 'healthy',
    LIVE_WORKER_SHA: sha, LIVE_WORKER_RUNNING: 'true', LIVE_BOT_SHA: sha, LIVE_BOT_RUNNING: 'true',
    LIVE_MINIAPP_SHA: sha, LIVE_PRISMA_HEAD: 'prisma-head', LIVE_AGATA_HEAD: 'agata0008',
    LIVE_AGATA_ENABLED: 'true', LIVE_AGATA_PROVIDER: 'disabled', LIVE_AGATA_CODEX_LOCAL_ENABLED: 'false',
    LIVE_CONFIG_AGATA_CODEX_LOCAL_ENABLED: 'YES', LIVE_CONFIG_AGATA_LLM_API_KEY: 'NO',
    LIVE_AGATA_API_SHA: sha, LIVE_AGATA_API_RUNNING: 'true', LIVE_AGATA_API_HEALTH: 'healthy',
    LIVE_AGATA_WORKER_SHA: sha, LIVE_AGATA_WORKER_RUNNING: 'true', LIVE_AGATA_WORKER_HEALTH: 'healthy',
    LIVE_AGATA_API_IMAGE_ID: 'sha256:shared', LIVE_AGATA_WORKER_IMAGE_ID: 'sha256:shared',
  };
  assert.deepEqual(recoveryRuntimeFailures(sha, facts, 'prisma-head'), []);
  assert.ok(recoveryRuntimeFailures(sha, { ...facts, LIVE_AGATA_WORKER_HEALTH: 'unhealthy' }, 'prisma-head').includes('AGATA worker is not healthy'));
  assert.ok(recoveryRuntimeFailures(sha, { ...facts, LIVE_MINIAPP_SHA: '0'.repeat(40) }, 'prisma-head').includes('Mini App SHA mismatch'));
});

test('recovery accepts mac_worker only with current local worker and runtime config evidence', () => {
  const facts = {
    LIVE_RELEASE_ENV_SHA: sha, LIVE_API_SHA: sha, LIVE_API_RUNNING: 'true', LIVE_API_HEALTH: 'healthy',
    LIVE_WORKER_SHA: sha, LIVE_WORKER_RUNNING: 'true', LIVE_BOT_SHA: sha, LIVE_BOT_RUNNING: 'true',
    LIVE_MINIAPP_SHA: sha, LIVE_PRISMA_HEAD: 'prisma-head', LIVE_AGATA_HEAD: 'agata0008',
    LIVE_AGATA_ENABLED: 'true', LIVE_AGATA_PROVIDER: 'mac_worker', LIVE_AGATA_CODEX_LOCAL_ENABLED: 'true',
    LIVE_CONFIG_AGATA_CODEX_LOCAL_ENABLED: 'YES', LIVE_CONFIG_GPOINT_AGATA_WORKER_TOKEN: 'YES',
    LIVE_AGATA_API_SHA: sha, LIVE_AGATA_API_RUNNING: 'true', LIVE_AGATA_API_HEALTH: 'healthy',
    LIVE_AGATA_WORKER_SHA: sha, LIVE_AGATA_WORKER_RUNNING: 'true', LIVE_AGATA_WORKER_HEALTH: 'healthy',
    LIVE_AGATA_API_IMAGE_ID: 'sha256:shared', LIVE_AGATA_WORKER_IMAGE_ID: 'sha256:shared',
  };
  const worker = freshWorkerStatus(Date.now());
  assert.deepEqual(recoveryRuntimeFailures(sha, facts, 'prisma-head', worker), []);
  assert.ok(recoveryRuntimeFailures(sha, facts, 'prisma-head').some(x => x.includes('authentication/status')));
});

test('recovery artifact identities are derived from verified live candidate facts', () => {
  const facts = {
    LIVE_API_IMAGE: 'sha256:api', LIVE_WORKER_IMAGE: 'sha256:worker', LIVE_BOT_IMAGE: 'sha256:bot',
    LIVE_MINIAPP_SHA: sha, LIVE_AGATA_API_IMAGE_ID: 'sha256:agata', LIVE_AGATA_WORKER_IMAGE_ID: 'sha256:agata',
  };
  assert.deepEqual(recoveryArtifactIdentities(sha, facts), {
    api: { sourceSha: sha, runtimeImageId: 'sha256:api' },
    worker: { sourceSha: sha, runtimeImageId: 'sha256:worker' },
    bot: { sourceSha: sha, runtimeImageId: 'sha256:bot' },
    miniapp: { sourceSha: sha, servedBuildSha: sha },
    agataApi: { sourceSha: sha, runtimeImageId: 'sha256:agata' },
    agataWorker: { sourceSha: sha, runtimeImageId: 'sha256:agata' },
  });
});

test('verified release reconciles activeRuntimeSha only after verified switch', () => {
  const prior = { currentProductionSha: 'a'.repeat(40), activeRuntimeSha: 'b'.repeat(40), activeRuntimeDbHead: 'old' };
  const next = verifiedRuntimeState(prior, { candidate: sha, dbHead: 'agata0008' });
  assert.equal(next.currentProductionSha, sha);
  assert.equal(next.activeRuntimeSha, sha);
  assert.equal(next.activeRuntimeDbHead, 'agata0008');
  assert.equal(prior.activeRuntimeSha, 'b'.repeat(40));
});

test('same candidate exact image is reusable and up-to-date migration is not redeployed', () => {
  assert.equal(shouldReuseCandidateImage({ candidate: sha, exactRuntimeImage: true }), true);
  assert.equal(migrationNeedsDeploy('Database schema is up to date!'), false);
  assert.equal(migrationNeedsDeploy('Following migrations have not yet been applied'), true);
});

test('release retention protects unresolved RECOVERY_REQUIRED candidate', () => {
  const plan = planReleaseRetention({
    currentSha: 'a'.repeat(40), rollbackSha: 'b'.repeat(40),
    candidates: [{ sha, status: 'RECOVERY_REQUIRED', recoveryRequired: true }],
    imageTags: [`gpoint-runtime:${sha}`, `gpoint-agata-runtime:${sha}`],
  });
  assert.deepEqual(plan.removableShas, []);
  assert.deepEqual(plan.protectedShas, ['a'.repeat(40), 'b'.repeat(40), sha].sort());
});

test('release retention removes a reconciled FAILED candidate and its attributable cache only', () => {
  const cacheId = 'cacheRecord_42';
  const plan = planReleaseRetention({
    currentSha: 'a'.repeat(40), rollbackSha: 'b'.repeat(40),
    candidates: [{ sha, status: 'FAILED_PRE_SWITCH', recoveryRequired: false }],
    imageTags: [`gpoint-runtime:${sha}`, `gpoint-agata-runtime:${sha}`],
    cacheRecords: [{ id: cacheId, candidateSha: sha, reclaimable: true }, { id: 'shared-cache', candidateSha: sha, reclaimable: false }],
  });
  assert.deepEqual(plan.removableShas, [sha]);
  assert.deepEqual(plan.removableCacheIds, [cacheId]);
});

test('release retention protects current and rollback SHAs', () => {
  const current = 'a'.repeat(40);
  const rollback = 'b'.repeat(40);
  const plan = planReleaseRetention({
    currentSha: current, rollbackSha: rollback,
    candidates: [{ sha: current, status: 'COMPLETE' }, { sha: rollback, status: 'COMPLETE' }],
    imageTags: [`gpoint-runtime:${current}`, `gpoint-runtime:${rollback}`],
  });
  assert.deepEqual(plan.removableShas, []);
});

test('release retention never plans volume removal', () => {
  const plan = planReleaseRetention({ candidates: [], imageTags: [], volumes: ['postgres-data'] });
  assert.deepEqual(plan.volumesToRemove, []);
  assert.equal(Object.hasOwn(plan, 'volumes'), false);
});

test('candidate cache cleanup is exact-SHA scoped and rejects ambiguous SHA', () => {
  const command = candidateBuildCacheCleanupCommand(sha);
  const encoded = command.match(/printf '%s' '([A-Za-z0-9+/=]+)'/)?.[1];
  const script = Buffer.from(encoded ?? '', 'base64').toString('utf8');
  assert.match(script, /docker','buildx/);
  assert.match(script, /CACHE_CLEANUP_SKIPPED=overlapping_unrelated_build/);
  assert.match(script, /id~=/);
  assert.match(script, /usage!='1'/);
  assert.throws(() => candidateBuildCacheCleanupCommand('not-a-sha'), /exact candidate SHA/);
});

test('detector identity is independent and rebuilds only when source inputs change', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-detector-test-'));
  const dir = path.join(root, 'infra/production/sensitive-detector');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'Dockerfile'), 'FROM node:22\n');
  fs.writeFileSync(path.join(dir, 'config.mjs'), 'export default {}\n');
  const first = detectorSourceHash(root);
  const same = detectorSourceHash(root);
  fs.writeFileSync(path.join(dir, 'config.mjs'), 'export default { port: 3009 }\n');
  const changed = detectorSourceHash(root);
  fs.rmSync(root, { recursive: true, force: true });
  assert.equal(first, same);
  assert.equal(detectorNeedsRebuild(first, same), false);
  assert.notEqual(first, changed);
  assert.equal(detectorNeedsRebuild(first, changed), true);
});

test('remote runner retains exact exit code and stderr without a parent pipe buffer', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-runner-test-'));
  const runner = path.join(root, 'runner.sh');
  fs.writeFileSync(runner, '#!/bin/sh\nprintf "bounded-output"\nprintf "diagnostic-stderr" >&2\nexit 17\n');
  fs.chmodSync(runner, 0o700);
  const previous = process.env.GPOINT_REMOTE_RUNNER;
  process.env.GPOINT_REMOTE_RUNNER = runner;
  let captured;
  assert.throws(() => runRemoteCommand('safe-test-command'), error => {
    captured = error;
    return error.exitCode === 17 && error.stderr.includes('diagnostic-stderr') && /^[0-9a-f]{64}$/.test(error.commandHash);
  });
  assert.equal(failureEvidence(captured, { stage: 'MIGRATION_APPLY' }).failureStage, 'MIGRATION_APPLY');
  assert.equal(failureEvidence(captured, { stage: 'MIGRATION_APPLY' }).failureExitCode, 17);
  if (previous === undefined) delete process.env.GPOINT_REMOTE_RUNNER; else process.env.GPOINT_REMOTE_RUNNER = previous;
  fs.rmSync(root, { recursive: true, force: true });
});

test('remote runner retries a proven banner timeout once and preserves the exact command', () => {
  const commands = [];
  const delays = [];
  const command = 'docker inspect compose-api-1';
  const result = runWithPreExecRetry(attempt => {
    commands.push(command);
    if (attempt === 1) throw Object.assign(new Error('banner timeout'), { classification: 'PRE_EXEC_TRANSPORT_FAILURE' });
    return 'healthy';
  }, { sleep: milliseconds => delays.push(milliseconds), jitter: (_low, high) => high });
  assert.equal(result, 'healthy');
  assert.deepEqual(commands, [command, command]);
  assert.deepEqual(delays, [1_000]);
});

test('three proven pre-exec failures stop after exactly three attempts with bounded backoff', () => {
  let attempts = 0;
  const delays = [];
  let now = 0;
  assert.throws(() => runWithPreExecRetry(() => {
    attempts += 1;
    throw Object.assign(new Error('Could not resolve host'), { classification: 'PRE_EXEC_TRANSPORT_FAILURE' });
  }, { sleep: milliseconds => { delays.push(milliseconds); now += milliseconds; }, now: () => now,
    jitter: (_low, high) => high, budgetMs: 3_500, emit: () => {} }), /NETWORK_UNAVAILABLE/);
  assert.equal(attempts, 3);
  assert.deepEqual(delays, [1_000, 2_000, 500]);
});

test('long GitHub TLS outage recovers within the bounded retry window and resumes the read', () => {
  let now = 0;
  let calls = 0;
  const events = [];
  const result = runWithBoundedGitHubRetries(() => {
    calls += 1;
    if (calls <= 27) throw Object.assign(new Error('TLS connection reset by peer'), { code: 'ECONNRESET' });
    return 'verified candidate metadata';
  }, { budgetMs: 1_800_000, now: () => now, sleep: ms => { now += ms; },
    jitter: (_low, high) => high, emit: event => events.push(event) });
  assert.equal(result, 'verified candidate metadata');
  assert.ok(now > 1_200_000 && now < 1_800_000);
  assert.ok(events.some(event => event.startsWith('NETWORK_RETRYING')));
  assert.ok(events.some(event => event.startsWith('NETWORK_RESTORED')));
  assert.equal(events.at(-1), 'RELEASE_CONTINUING stage=github_read');
});

test('remote command, auth, host-key, and ambiguous post-start failures never retry', () => {
  for (const [classification, message] of [
    ['REMOTE_COMMAND_FAILURE', 'docker command failed'],
    ['REMOTE_COMMAND_FAILURE', 'Permission denied (publickey)'],
    ['REMOTE_COMMAND_FAILURE', 'Host key verification failed'],
    ['AMBIGUOUS_POST_START_FAILURE', 'Connection reset after command start'],
  ]) {
    let attempts = 0;
    assert.throws(() => runWithPreExecRetry(() => {
      attempts += 1;
      throw Object.assign(new Error(message), { classification });
    }, { sleep: () => assert.fail('unsafe remote failure must not retry') }), new RegExp(message.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.equal(attempts, 1);
  }
});

test('pre-exec runner marker is emitted only for recognized SSH failure before command send', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-pre-exec-runner-test-'));
  const home = path.join(root, 'home');
  const bin = path.join(root, 'bin');
  const configDir = path.join(home, '.config/gpoint');
  const identity = path.join(home, '.ssh/gpoint_deploy_ed25519');
  const statusFile = path.join(root, 'status');
  const modeFile = path.join(root, 'mode');
  fs.mkdirSync(configDir, { recursive: true });
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.dirname(identity), { recursive: true });
  fs.writeFileSync(identity, 'test-private-key-placeholder');
  fs.writeFileSync(path.join(configDir, 'release-tools.env'), 'GPOINT_SSH_IDENTITY_FILE=$HOME/.ssh/gpoint_deploy_ed25519\n');
  fs.writeFileSync(path.join(bin, 'ssh'), `#!/bin/sh\ndebug=\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -E ]; then shift; debug=$1; fi; shift; done\ncase "$(cat "$MODE_FILE")" in\npreexec) echo 'Connection timed out during banner exchange' >&2; echo 'ssh: Connection timed out during banner exchange' >> "$debug"; exit 255;;\nambiguous) echo 'Connection reset by peer' >&2; echo 'debug1: Sending command: hostname' >> "$debug"; exit 255;;\nauth) echo 'Permission denied (publickey).' >&2; echo 'debug1: Offering public key' >> "$debug"; exit 255;;\nhostkey) echo 'Host key verification failed.' >&2; echo 'Host key verification failed.' >> "$debug"; exit 255;;\nremote) echo 'remote docker failure' >&2; echo 'debug1: Sending command: hostname' >> "$debug"; exit 17;;\nesac\n`, { mode: 0o700 });
  const runner = path.join(path.dirname(new URL(import.meta.url).pathname), 'gpoint-vps-runner.sh');
  const env = { HOME: home, PATH: `${bin}:${process.env.PATH}`, GPOINT_REMOTE_ATTEMPT_STATUS_FILE: statusFile, MODE_FILE: modeFile, REMOTE_CMD: 'hostname' };
  try {
    fs.writeFileSync(modeFile, 'preexec');
    const preexec = spawnSync(runner, [], { encoding: 'utf8', env });
    assert.equal(preexec.status, 75);
    assert.equal(fs.readFileSync(statusFile, 'utf8').trim(), 'PRE_EXEC_TRANSPORT_FAILURE:255');
    for (const mode of ['ambiguous', 'auth', 'hostkey', 'remote']) {
      fs.writeFileSync(modeFile, mode);
      fs.writeFileSync(statusFile, '');
      const result = spawnSync(runner, [], { encoding: 'utf8', env });
      assert.equal(result.status, mode === 'remote' ? 17 : 255, mode);
      assert.equal(fs.readFileSync(statusFile, 'utf8'), '', mode);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('pre-exec retry errors and logs do not expose command secrets', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-pre-exec-redaction-test-'));
  const runner = path.join(root, 'runner.sh');
  const countFile = path.join(root, 'count');
  const secret = 'synthetic-secret-value';
  let now = 0;
  fs.writeFileSync(runner, `#!/bin/sh\ncount=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\ncount=$((count + 1))\nprintf '%s' "$count" > "$COUNT_FILE"\nprintf 'TOKEN=${secret}\\n' >&2\nprintf '%s\\n' 'PRE_EXEC_TRANSPORT_FAILURE:255' > "$GPOINT_REMOTE_ATTEMPT_STATUS_FILE"\nexit 75\n`);
  fs.chmodSync(runner, 0o700);
  const previousRunner = process.env.GPOINT_REMOTE_RUNNER;
  const previousCount = process.env.COUNT_FILE;
  const messages = [];
  const previousLog = console.error;
  process.env.GPOINT_REMOTE_RUNNER = runner;
  process.env.COUNT_FILE = countFile;
  console.error = (...values) => messages.push(values.join(' '));
  try {
    assert.throws(() => runRemoteCommand('safe-command', {}, {
      now: () => now, sleep: milliseconds => { now += milliseconds; },
      jitter: (_low, high) => high, budgetMs: 3_500, emit: message => messages.push(message),
    }), error => {
      assert.equal(error.code, 'NETWORK_UNAVAILABLE');
      assert.doesNotMatch(error.message, new RegExp(secret));
      return true;
    });
    assert.equal(fs.readFileSync(countFile, 'utf8'), '3');
    assert.doesNotMatch(messages.join('\n'), new RegExp(secret));
  } finally {
    console.error = previousLog;
    if (previousRunner === undefined) delete process.env.GPOINT_REMOTE_RUNNER; else process.env.GPOINT_REMOTE_RUNNER = previousRunner;
    if (previousCount === undefined) delete process.env.COUNT_FILE; else process.env.COUNT_FILE = previousCount;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('remote migration failure evidence preserves output and exit while redacting secrets and command payloads', () => {
  const error = Object.assign(new Error('remote command failed (exit=1, command_sha256=abc)'), {
    exitCode: 1,
    command: 'sha256:abc',
    commandHash: 'abc',
    stdout: 'migration output',
    stderr: 'permission denied; AGATA_INTERNAL_API_TOKEN=secret-value DATABASE_URL=postgresql://gpoint:db-secret@db/gpoint',
  });
  const evidence = failureEvidence(error, { stage: 'MIGRATION_APPLY' });
  assert.equal(evidence.failureStage, 'MIGRATION_APPLY');
  assert.equal(evidence.failureExitCode, 1);
  assert.equal(evidence.failureCommand, 'MIGRATION_APPLY');
  assert.equal(evidence.failureStdout, 'migration output');
  assert.match(evidence.failureStderr, /permission denied/);
  assert.doesNotMatch(JSON.stringify(evidence), /secret-value|db-secret/);
});

test('apply checkout uses the exact local commit in a clean isolated worktree', () => {
  const candidate = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  const checkout = exactCheckout(candidate);
  try {
    assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: checkout, encoding: 'utf8' }).trim(), candidate);
    assert.equal(execFileSync('git', ['status', '--porcelain'], { cwd: checkout, encoding: 'utf8' }).trim(), '');
  } finally {
    execFileSync('git', ['worktree', 'remove', '--force', checkout], { stdio: 'ignore' });
  }
});

test('image uploads receive a size-aware bounded deadline while small uploads retain 60 seconds', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-image-upload-test-'));
  const runner = path.join(root, 'runner.sh');
  const local = path.join(root, 'images.tar.gz');
  fs.writeFileSync(runner, '#!/bin/sh\nprintf "%s" "$GPOINT_SCP_COMMAND_TIMEOUT_SECONDS"\n', { mode: 0o700 });
  const previous = process.env.GPOINT_SCP_RUNNER;
  process.env.GPOINT_SCP_RUNNER = runner;
  try {
    fs.writeFileSync(local, 'small source');
    assert.equal(upload(local, '/tmp/exact-images'), '60');
    fs.truncateSync(local, 348 * 1024 * 1024);
    assert.equal(upload(local, '/tmp/exact-images'), '378');
    fs.truncateSync(local, 2 * 1024 * 1024 * 1024);
    assert.equal(upload(local, '/tmp/exact-images'), '600');
  } finally {
    if (previous === undefined) delete process.env.GPOINT_SCP_RUNNER; else process.env.GPOINT_SCP_RUNNER = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('source upload retries a transient SCP failure and preserves the exact paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-upload-test-'));
  const runner = path.join(root, 'runner.sh');
  const count = path.join(root, 'count');
  const local = path.join(root, 'source');
  fs.writeFileSync(local, 'exact-source');
  fs.writeFileSync(runner, '#!/bin/sh\ncount=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\ncount=$((count + 1))\nprintf "%s" "$count" > "$COUNT_FILE"\nif [ "$count" -lt 2 ]; then echo "temporary connection failure" >&2; exit 255; fi\ntest "$LOCAL_FILE" = "$EXPECTED_LOCAL" && test "$REMOTE_FILE" = "$EXPECTED_REMOTE"\n');
  fs.chmodSync(runner, 0o700);
  const previous = { runner: process.env.GPOINT_SCP_RUNNER, count: process.env.COUNT_FILE, local: process.env.EXPECTED_LOCAL, remote: process.env.EXPECTED_REMOTE };
  Object.assign(process.env, { GPOINT_SCP_RUNNER: runner, COUNT_FILE: count, EXPECTED_LOCAL: local, EXPECTED_REMOTE: '/tmp/exact-source' });
  try {
    upload(local, '/tmp/exact-source');
    assert.equal(fs.readFileSync(count, 'utf8'), '2');
  } finally {
    for (const [key, value] of Object.entries({ GPOINT_SCP_RUNNER: previous.runner, COUNT_FILE: previous.count, EXPECTED_LOCAL: previous.local, EXPECTED_REMOTE: previous.remote })) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('source upload uses at most one controlled retry', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-upload-bound-test-'));
  const runner = path.join(root, 'runner.sh');
  const count = path.join(root, 'count');
  const local = path.join(root, 'source');
  fs.writeFileSync(local, 'exact-source');
  fs.writeFileSync(runner, '#!/bin/sh\ncount=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\ncount=$((count + 1))\nprintf "%s" "$count" > "$COUNT_FILE"\necho "synthetic bounded failure" >&2\nexit 255\n');
  fs.chmodSync(runner, 0o700);
  const previous = { runner: process.env.GPOINT_SCP_RUNNER, count: process.env.COUNT_FILE };
  Object.assign(process.env, { GPOINT_SCP_RUNNER: runner, COUNT_FILE: count });
  try {
    assert.throws(() => upload(local, '/tmp/exact-source'), /failed after 2 attempts/);
    assert.equal(fs.readFileSync(count, 'utf8'), '2');
  } finally {
    if (previous.runner === undefined) delete process.env.GPOINT_SCP_RUNNER; else process.env.GPOINT_SCP_RUNNER = previous.runner;
    if (previous.count === undefined) delete process.env.COUNT_FILE; else process.env.COUNT_FILE = previous.count;
    fs.rmSync(root, { recursive: true, force: true });
  }
});


test('large cleanup transports exact bytes through a verified script and bounded SSH command', () => {
  const command = 'echo cleanup;'.repeat(4000);
  let source, remotePath;
  const result = runCleanupCommand(command, {
    remoteUpload(local, remote) {
      source = local; remotePath = remote;
      assert.equal(fs.readFileSync(local, 'utf8'), command);
      assert.equal(fs.statSync(local).mode & 0o777, 0o600);
    },
    remoteRun(shortCommand) {
      assert.ok(Buffer.byteLength(shortCommand) < 1024);
      assert.ok(shortCommand.includes('sha256sum'));
      assert.ok(shortCommand.includes(remotePath));
      // Run the actual checksum guard locally on the uploaded bytes.
      fs.copyFileSync(source, remotePath);
      try { return execFileSync('sh', ['-c', shortCommand], {encoding:'utf8'}); }
      finally { fs.rmSync(remotePath, {force:true}); }
    },
  });
  assert.equal(result.trim().split('\n').length, 4000);
  assert.equal(fs.existsSync(source), false);
});

test('large cleanup removes its local script and preserves remote execution failures', () => {
  let source;
  const failure = new Error('cleanup execution failed');
  assert.throws(() => runCleanupCommand('true;'.repeat(8000), {
    remoteUpload(local) { source = local; },
    remoteRun() { throw failure; },
  }), error => error === failure);
  assert.equal(fs.existsSync(source), false);
});

test('cleanup checksum mismatch prevents execution of a changed remote script', () => {
  let remotePath;
  assert.throws(() => runCleanupCommand('true;'.repeat(8000), {
    remoteUpload(local, remote) { remotePath = remote; fs.writeFileSync(remote, 'echo SHOULD_NOT_EXECUTE'); },
    remoteRun(command) {
      try { return execFileSync('sh', ['-c', command], {encoding:'utf8', stdio:['ignore','pipe','pipe']}); }
      finally { fs.rmSync(remotePath, {force:true}); }
    },
  }), error => error.status !== 0 && String(error.stdout ?? '') === '');
});

test('agata0010 canonical gate requires verified role and authoritative inventory only for pending migration', async () => {
  const { inspectAgataMigrationReadiness } = await import('./release-gpoint-vps-adapter.mjs');
  assert.equal(typeof inspectAgataMigrationReadiness, 'function', 'pending migration readiness is absent');
  const evidence = { readiness: 'PASS', tenant_count: 2, inventory_sha256: 'a'.repeat(64), database: 'gpoint', head: 'agata0009' };
  let calls = 0;
  const execute = command => { calls++; assert.match(command, /python3/); return 'AGATA_MIGRATION_READINESS=' + JSON.stringify(evidence); };
  assert.deepEqual(inspectAgataMigrationReadiness(sha, 'agata0009', 'agata0010', execute), evidence);
  assert.equal(calls, 1);
  assert.equal(inspectAgataMigrationReadiness(sha, 'agata0010', 'agata0010', execute), null);
  assert.equal(calls, 1);
  assert.throws(() => inspectAgataMigrationReadiness(sha, 'agata0008', 'agata0010', execute), /agata0009/);
  assert.throws(() => inspectAgataMigrationReadiness(sha, 'agata0009', 'agata0010', () => ''), /readiness/);
  assert.throws(() => inspectAgataMigrationReadiness(sha, 'agata0009', 'agata0010', () => 'AGATA_MIGRATION_READINESS={"readiness":"FAIL"}'), /readiness/);
});

test('agata0011 canonical gate cannot bypass the guarded migration path', async () => {
  const { inspectAgataMigrationReadiness } = await import('./release-gpoint-vps-adapter.mjs');
  const evidence = { readiness: 'PASS', tenant_count: 2, inventory_sha256: 'a'.repeat(64), database: 'gpoint', head: 'agata0010' };
  let calls = 0;
  const execute = command => { calls++; assert.match(command, /agata-production-migration\.py/); return 'AGATA_MIGRATION_READINESS=' + JSON.stringify(evidence); };
  assert.deepEqual(inspectAgataMigrationReadiness(sha, 'agata0010', 'agata0011', execute), evidence);
  assert.equal(calls, 1);
  assert.throws(() => inspectAgataMigrationReadiness(sha, 'agata0009', 'agata0011', execute), /agata0010/);
  assert.throws(() => inspectAgataMigrationReadiness(sha, 'agata0010', 'agata0011', () => ''), /readiness/);
});

test('guarded migration retains complete backup and exact SHA evidence for the manifest', async () => {
  const { parseGuardedMigrationEvidence } = await import('./release-gpoint-vps-adapter.mjs');
  assert.equal(typeof parseGuardedMigrationEvidence, 'function');
  const backup = { candidate_sha: sha, database: 'gpoint', schemas: ['public','agata'], path: '/opt/gpoint/backups/test.dump', status: 'COMPLETE_PROTECTED', sha256: 'b'.repeat(64) };
  const baseline = { candidate_sha: sha, readiness: 'PASS', head_after: 'agata0010', tenant_count: 2, inventory_sha256: 'a'.repeat(64) };
  const lines = 'GPOINT_COMPLETE_BACKUP=' + JSON.stringify(backup) + '\nAGATA_MIGRATION_VERIFIED=' + JSON.stringify(baseline);
  assert.deepEqual(parseGuardedMigrationEvidence(lines, sha), { backup, baseline });
  assert.throws(() => parseGuardedMigrationEvidence(lines, 'c'.repeat(40)), /SHA/);
  assert.throws(() => parseGuardedMigrationEvidence('AGATA_MIGRATION_VERIFIED='+JSON.stringify(baseline),sha), /backup/);
});

test('guarded migration evidence accepts the agata0011 migration target and complete schema backup', async () => {
  const { parseGuardedMigrationEvidence } = await import('./release-gpoint-vps-adapter.mjs');
  const backup = { candidate_sha: sha, database: 'gpoint', schemas: ['public','agata','gsales','agata_identity'], path: '/opt/gpoint/backups/test.dump', status: 'COMPLETE_PROTECTED', sha256: 'b'.repeat(64) };
  const baseline = { candidate_sha: sha, readiness: 'PASS', head_after: 'agata0011', tenant_count: 2, inventory_sha256: 'a'.repeat(64) };
  const lines = 'GPOINT_COMPLETE_BACKUP=' + JSON.stringify(backup) + '\nAGATA_MIGRATION_VERIFIED=' + JSON.stringify(baseline);
  assert.deepEqual(parseGuardedMigrationEvidence(lines, sha), { backup, baseline });
});
