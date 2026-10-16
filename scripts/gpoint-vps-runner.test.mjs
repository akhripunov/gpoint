import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(new URL('./gpoint-vps-runner.sh', import.meta.url));

test('remote command runner forwards stdin without adding it to command arguments', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-ssh-stdin-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const key = path.join(root, 'key'); fs.writeFileSync(key, 'test');
  const config = path.join(root, 'release-tools.env'); fs.writeFileSync(config, `GPOINT_SSH_IDENTITY_FILE=${key}\n`);
  const bin = path.join(root, 'bin'); fs.mkdirSync(bin);
  const capture = path.join(root, 'stdin'); const args = path.join(root, 'args');
  fs.writeFileSync(path.join(bin, 'ssh'), `#!/bin/sh\nprintf '%s\\n' "$*" > "$GPOINT_TEST_ARGS"\ncat > "$GPOINT_TEST_STDIN"\nprintf 'REMOTE_OK\\n'\n`, { mode: 0o700 });
  const token = 'one-time-read-only-ghcr-token';
  const result = spawnSync('sh', [script], { input: token + '\n', encoding: 'utf8', env: {
    ...process.env, PATH: `${bin}:${process.env.PATH}`, GPOINT_RELEASE_CONFIG: config,
    GPOINT_SSH_IDENTITY_FILE: key, GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS: '10',
    GPOINT_TEST_STDIN: capture, GPOINT_TEST_ARGS: args, REMOTE_CMD: 'read-stdin-and-pull-digests',
  }});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(fs.readFileSync(capture, 'utf8'), token + '\n');
  assert.match(fs.readFileSync(args, 'utf8'), /read-stdin-and-pull-digests/);
  assert.doesNotMatch(fs.readFileSync(args, 'utf8'), /one-time-read-only-ghcr-token/);
  assert.doesNotMatch(result.stdout + result.stderr, /one-time-read-only-ghcr-token/);
});
