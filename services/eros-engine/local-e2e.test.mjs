import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
  validateDockerEndpoint,
  validateErosImageInspection,
} from './local-test-policy.mjs';
import { readProviderContextEvidence } from './provider-context-check.mjs';
import { isAgataMessageHistoryPath } from './local-test-api-routes.mjs';

const directory = new URL('.', import.meta.url);


test('local API test image copies the AGATA route helper used by its entrypoint', () => {
  const dockerfile = readFileSync(new URL('Dockerfile.api-local-test', directory), 'utf8');
  assert.match(dockerfile, /COPY services\/eros-engine\/local-test-api-routes\.mjs services\/eros-engine\/local-test-api-routes\.mjs/);
});

test('local AGATA fixture recognizes history requests with query parameters', () => {
  assert.equal(isAgataMessageHistoryPath('/internal/v1/users/synthetic-user/characters/synthetic-creator/messages'), true);
  assert.equal(isAgataMessageHistoryPath('/internal/v1/users/synthetic-user/characters/synthetic-creator/messages?limit=1'), true);
  assert.equal(isAgataMessageHistoryPath('/internal/v1/users/synthetic-user/characters/synthetic-creator/other?limit=1'), false);
});

test('local AGATA fixture provides an empty unread page so Eros unread counters are exercised', () => {
  const entrypoint = readFileSync(new URL('local-test-api-entrypoint.mjs', directory), 'utf8');
  assert.match(entrypoint, /\/internal\\\/v1\\\/users\\\/\[\^\/\]\+\\\/unread/);
  assert.match(entrypoint, /return send\(response, 200, \{ items: \[\] \}\)/);
});

test('local E2E topology has API, Eros, Worker and isolated PostgreSQL', () => {
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(compose, /gpoint-api:/);
  assert.match(compose, /gpoint-worker:/);
  assert.match(compose, /profiles:\s*\[test\]/);
  assert.match(compose, /eros:/);
  assert.match(compose, /postgres:/);
  assert.match(compose, /DATABASE_URL:[^\n]*@postgres:5432\/gpoint_test\?schema=public/);
  assert.doesNotMatch(compose, /EROS_ADAPTER_DATABASE_URL/);
  assert.match(compose, /eros-migrate:[\s\S]*?postgres:5432\/eros_test/);
  assert.match(compose, /eros:[\s\S]*?postgres:5432\/eros_test/);
  assert.match(compose, /test-personas:\/etc\/eros-engine\/test-personas:ro/);
  assert.match(compose, /internal:\s*true/);
  assert.match(compose, /network_mode:\s*service:gpoint-api/);
  assert.match(compose, /gpoint-worker:[\s\S]*?profiles:\s*\[worker\]/);
  assert.match(compose, /read_only:\s*true/);
  assert.doesNotMatch(compose, /ports:|host\.docker\.internal|external:\s*true/i);
});

test('only the disposable Eros migrator receives database CREATE privilege', () => {
  const init = readFileSync(new URL('local-test-init.sql', directory), 'utf8');
  const erosInit = init.split('\\connect gpoint_test')[0];
  assert.match(init, /GRANT CONNECT, CREATE ON DATABASE eros_test TO eros_migrator;/);
  assert.match(init, /GRANT USAGE, CREATE ON SCHEMA public TO eros_migrator;/);
  assert.match(init, /CREATE DATABASE gpoint_test OWNER eros_test_admin;/);
  assert.match(init, /CREATE ROLE eros_migrator LOGIN[\s\S]*?NOSUPERUSER NOCREATEDB NOBYPASSRLS;/);
  assert.match(init, /CREATE ROLE eros_runtime LOGIN[^;]*NOSUPERUSER NOCREATEDB BYPASSRLS;/);
  assert.match(init, /CREATE ROLE eros_runtime_nobypass LOGIN[^;]*NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;/);
  assert.doesNotMatch(erosInit, /CREATE SCHEMA engine/);
  assert.doesNotMatch(erosInit, /ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine/);
  assert.doesNotMatch(init, /GRANT [^;]* ON DATABASE eros_test TO (?!eros_migrator\b)[a-z_]+;/);
});

test('stock Eros migration creates engine schema before test runtime grants', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const postgres = runner.indexOf('compose up -d --wait --wait-timeout 240 postgres');
  const migration = runner.indexOf('compose up -d --no-deps eros-migrate');
  const wait = runner.indexOf('migrator_status=$(docker_local wait "$migrator_container")');
  const grants = runner.indexOf('GRANT USAGE ON SCHEMA engine TO eros_runtime, eros_runtime_nobypass;');
  const stack = runner.indexOf('compose up -d --wait --wait-timeout 240', grants + 1);
  assert.ok(postgres >= 0 && postgres < migration && migration < wait && wait < grants && grants < stack,
    'wait for stock migration exit before grants and remaining services');
  assert.match(runner, /ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eros_runtime_nobypass;/);
  assert.match(runner, /\[ "\$migrator_status" = 0 \]/);
});

test('GPoint test API has no connection or data grants to the Eros database', () => {
  const init = readFileSync(new URL('local-test-init.sql', directory), 'utf8');
  const erosInit = init.split('\\connect gpoint_test')[0];
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.doesNotMatch(erosInit, /GRANT [^;]* ON SCHEMA engine TO [^;]*gpoint_api_test/);
  assert.match(erosInit, /REVOKE CONNECT ON DATABASE eros_test FROM PUBLIC/);
  assert.doesNotMatch(erosInit, /(?:GRANT|ALTER DEFAULT PRIVILEGES)[^;]*TO gpoint_worker_test/);
  assert.doesNotMatch(erosInit, /ALTER DEFAULT PRIVILEGES[^;]*TO gpoint_api_test/);
  assert.doesNotMatch(erosInit, /GRANT [^;]* ON ALL TABLES IN SCHEMA [^;]* TO gpoint_api_test/);
  assert.doesNotMatch(erosInit, /GRANT [^;]* ON ALL SEQUENCES IN SCHEMA [^;]* TO gpoint_api_test/);
  assert.doesNotMatch(readFileSync(new URL('compose.local-test.yml', directory), 'utf8'), /EROS_ADAPTER_DATABASE_URL/);
  assert.match(runner, /has_column_privilege\('gpoint_api_test', c\.oid, a\.attnum, p\.privilege\)/);
  assert.match(runner, /has_table_privilege\('gpoint_api_test', c\.oid, p\.privilege\)/);
  assert.match(runner, /has_sequence_privilege\('gpoint_api_test', c\.oid, p\.privilege\)/);
  assert.match(runner, /has_database_privilege\('gpoint_api_test', 'eros_test', 'CONNECT'\)/);
  assert.match(runner, /LOCAL_E2E_EROS_DB_API_ISOLATION=PASS api_role_connect=false schema_usage=false table_privileges=0 column_privileges=0 sequence_privileges=0/);
});

test('GPoint-owned read receipts preserve selected-message semantics and survive Eros restart', () => {
  const e2e = readFileSync(new URL('local-test-e2e.mjs', directory), 'utf8');
  assert.match(e2e, /LOCAL_E2E_GPOINT_OWNED_READ_RECEIPTS=PASS/);
  assert.match(e2e, /foreignAck\.status, 404/);
  assert.match(e2e, /LOCAL_E2E_GPOINT_READ_RECEIPT_RESTART=PASS/);
  const schema = readFileSync(new URL('../../packages/db/prisma/schema.prisma', import.meta.url), 'utf8');
  const stateModel = schema.split('model ErosAssistantMessageState {')[1]?.split('\n}')[0] ?? '';
  assert.match(stateModel, /messageId String/);
  assert.match(stateModel, /readAt DateTime\?/);
  assert.doesNotMatch(stateModel, /content|text|memory|summary/i);
});

test('creator cleanup retains the GPoint-owned Eros relationship reference', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const profileDelete = runner.indexOf('DELETE FROM \"CreatorProfile\"');
  const creatorDelete = runner.indexOf('DELETE FROM \"Creator\"');
  assert.match(runner, /DELETE FROM \"CreatorProfile\"[^\n]*eros_test_profile/);
  const historyCheck = runner.indexOf('--history-only');
  assert.ok(historyCheck >= 0 && profileDelete > historyCheck && creatorDelete > profileDelete);
  assert.match(runner, /ErosRelationshipMapping_creatorId_fkey/);
});

test('GPoint consent and Eros mapping foreign keys preserve ownership records', () => {
  const schema = readFileSync(new URL('../../packages/db/prisma/schema.prisma', directory), 'utf8');
  const migration = readFileSync(new URL('../../packages/db/prisma/migrations/20261020153000_eros_external_relationship_mapping/migration.sql', directory), 'utf8');
  const model = schema.match(/model ErosRelationshipMapping \{[\s\S]*?\n\}/)?.[0] ?? '';
  const consent = schema.match(/model GpointChatConsent \{[\s\S]*?\n\}/)?.[0] ?? '';
  const age = schema.match(/model GpointAgeAssurance \{[\s\S]*?\n\}/)?.[0] ?? '';
  assert.match(model, /user User @relation\(fields: \[userId\], references: \[id\], onDelete: Restrict\)/);
  assert.match(model, /creator Creator @relation\(fields: \[creatorId\], references: \[id\], onDelete: Restrict\)/);
  assert.match(consent, /user User @relation\(fields: \[userId\], references: \[id\], onDelete: Restrict/);
  assert.match(age, /user User @relation\(fields: \[userId\], references: \[id\], onDelete: Restrict/);
  assert.match(migration, /ErosRelationshipMapping_userId_fkey[\s\S]*?ON DELETE RESTRICT/);
  assert.match(migration, /ErosRelationshipMapping_creatorId_fkey[\s\S]*?ON DELETE RESTRICT/);
  assert.doesNotMatch(migration, /ErosRelationshipMapping_(?:userId|creatorId)_fkey[^;]*ON DELETE CASCADE/);
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(runner, /LOCAL_E2E_GPOINT_MAPPING_OWNERSHIP=PASS/);
  assert.doesNotMatch(runner, /MAPPING_ERASURE_GUARD|blocked_without_stock_eros_erase/);
});

test('model config routes chat, embeddings and synthetic dreaming scan to the internal GPOINT bridge', () => {
  const config = readFileSync(new URL('test-model-config.toml', directory), 'utf8');
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(config, /fallback_temperature\s*=\s*1(?:\.0)?/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/chat\/completions/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/embeddings/);
  assert.match(compose, /GPOINT_API_KEY: \$\{EROS_TEST_GPOINT_API_KEY/);
  assert.doesNotMatch(config, /openrouter|openai\.com|anthropic\.com|gemini\.googleapis\.com/i);
  assert.match(config, /^\[tasks\.dreaming_scan\]/m);
  assert.match(config, /^\[tasks\.arc_rewrite\]/m);
  assert.doesNotMatch(config, /^\[tasks\.memory_extraction\]/m);
  assert.match(config, /filter_prompt\s*=\s*"""[\s\S]*SYNTHETIC_DREAMING_SCAN_RESULT/i);
  const dreaming = config.split('[tasks.dreaming_scan]')[1]?.split('\n[tasks.')[0] ?? '';
  assert.match(dreaming, /temperature\s*=\s*1\.0/);
  assert.match(compose, /DREAMING_TICK_SECS/);
  assert.match(compose, /DREAMING_IDLE_SECS/);
  assert.match(runner, /LOCAL_E2E_DREAMING_SCAN=PASS/);
  assert.match(runner, /LOCAL_E2E_REAL_INFERENCE=NOT_RUN/);
  assert.match(runner, /LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS fixture_role=gpoint_worker_test/);
  assert.match(runner, /c\.relkind IN \('r','p'\)/);
  assert.match(runner, /\[ "\$eros_rls_enabled" = "\$eros_tables" \]/);
  assert.match(runner, /LOCAL_E2E_EROS_DATABASE_RLS_LIMITATION=CONFIRMED tables=\$eros_tables.*runtime_role_rows=\$eros_admin_personas strict_role_rows=\$eros_strict_personas policies=\$eros_policy_count/);
  assert.match(runner, /LOCAL_E2E_EROS_DATABASE_USER_RLS=NOT_AVAILABLE stock_api_ownership_test=PASS/);
});

test('runtime Eros profiles keep only the dreaming scan output contract and parameters-only arc rewrite', () => {
  for (const name of ['eros-production-model-config.toml', 'eros-pilot-model-config.toml',
    'eros-layer7-staging-model-config.toml']) {
    const config = readFileSync(new URL(name, directory), 'utf8');
    const dreaming = config.split('[tasks.dreaming_scan]')[1]?.split('\n[tasks.')[0] ?? '';
    const arc = config.split('[tasks.arc_rewrite]')[1] ?? '';
    assert.match(config, /^\[tasks\.dreaming_scan\]/m, name);
    assert.match(config, /^\[tasks\.arc_rewrite\]/m, name);
    assert.doesNotMatch(config, /^\[tasks\.memory_extraction\]/m, name);
    assert.match(dreaming, /filter_prompt\s*=\s*"""/, name);
    assert.match(dreaming, /memories must be an array of objects with string content and category fields/i, name);
    assert.match(dreaming, /additional fields may be included and are stored by Eros as opaque metadata/i, name);
    assert.doesNotMatch(dreaming, /privacy rules|never retain|non-sensitive|return only|0â€“10|0-10|sensitive data/i, name);
    assert.doesNotMatch(arc, /filter_prompt\s*=/, name);
    if (name === 'eros-layer7-staging-model-config.toml') {
      assert.match(dreaming, /temperature\s*=\s*0\.2/, name);
      assert.match(arc, /temperature\s*=\s*0\.3/, name);
    } else {
      assert.match(dreaming, /temperature\s*=\s*1\.0/, name);
      assert.match(arc, /temperature\s*=\s*1\.0/, name);
    }
  }
  const fixture = readFileSync(new URL('test-model-config.toml', directory), 'utf8');
  const fixtureDreaming = fixture.split('[tasks.dreaming_scan]')[1]?.split('\n[tasks.')[0] ?? '';
  assert.match(fixtureDreaming, /SYNTHETIC_DREAMING_SCAN_RESULT/);
  assert.match(fixtureDreaming, /local deterministic fixture recognizes only its marked synthetic test inputs/i);
  assert.doesNotMatch(fixtureDreaming, /privacy rules|never retain .*credentials/i);
});

test('isolated tiered E2E exercises stock Eros recall at affinity levels 1 through 5', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const script = readFileSync(new URL('local-test-tier-memory-e2e.mjs', directory), 'utf8');
  const fixtures = readFileSync(new URL('local-test-tier-memory-fixtures.py', directory), 'utf8');
  const worker = readFileSync(new URL('local-test-worker.py', directory), 'utf8');
  const apiDockerfile = readFileSync(new URL('Dockerfile.api-local-test', directory), 'utf8');
  assert.match(apiDockerfile, /COPY services\/eros-engine\/local-test-tier-memory-e2e\.mjs/);
  assert.match(runner, /local-test-tier-memory-e2e\.mjs --prepare/);
  assert.match(runner, /local-test-tier-memory-fixtures\.py/);
  assert.match(runner, /-e "LOCAL_TIER_FIXTURES_B64=\$tier_fixture_b64" gpoint-api node \/workspace\/services\/eros-engine\/local-test-tier-memory-e2e\.mjs --verify/);
  const priorSessionGuard = runner.indexOf('[ "$session_count" = 1 ]');
  const matrixRun = runner.indexOf('local-test-tier-memory-e2e.mjs --prepare');
  assert.ok(priorSessionGuard >= 0 && matrixRun > priorSessionGuard,
    'the 10-session affinity matrix must run after the legacy single-session assertion');
  assert.match(runner, /compose down --volumes --remove-orphans/,
    'the affinity fixtures are isolated in the disposable stack and removed on exit');
  assert.match(script, /memory_scope/);
  assert.match(script, /bond5_chem1/);
  assert.match(script, /bond1_chem5/);
  assert.match(script, /scope_none/);
  assert.match(script, /scope_relationship_only/);
  assert.match(script, /scope_neutral_only/);
  assert.match(fixtures, /relationship_episodes/);
  assert.match(fixtures, /relationship_arcs/);
  assert.match(fixtures, /relationship_journal/);
  assert.match(fixtures, /metadata/);
  assert.match(worker, /LOCAL_TEST_TIER_CONTEXT/);
  assert.match(worker, /SYNTH_TIER_CASE/);
  assert.match(script, /LOCAL_E2E_AFFINITY_TIER=PASS/);
  assert.match(script, /LOCAL_E2E_MEMORY_SCOPE=PASS/);
  assert.match(script, /LOCAL_E2E_REAL_INFERENCE=NOT_RUN/);
});

test('GPoint adapter and injected dialogue prompts do not control Eros memory scope or use', () => {
  const adapter = readFileSync(new URL('../../apps/api/src/eros-adapter.ts', import.meta.url), 'utf8');
  const responsePrompt = readFileSync(new URL('../../apps/api/src/eros-response-prompt.js', import.meta.url), 'utf8');
  const workerContract = readFileSync(new URL('../../apps/api/src/eros-worker-contract.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(adapter, /memory_scope\s*:/);
  assert.doesNotMatch(responsePrompt, /memories|memory/i);
  const dialoguePrompt = workerContract.split('const GPOINT_EROS_DIALOGUE_ADAPTER = `')[1]?.split('`;')[0] ?? '';
  assert.ok(dialoguePrompt, 'the GPoint dialogue adapter prompt is present');
  assert.doesNotMatch(dialoguePrompt, /memories|memory/i);
});

test('Mac Ollama tunnel preserves its reverse forward and reconnects after SSH exits', () => {
  const tunnel = readFileSync(new URL('open-mac-ollama-tunnel.sh', directory), 'utf8');
  assert.doesNotMatch(tunnel, /ClearAllForwardings=yes/);
  assert.match(tunnel, /-R 127\.0\.0\.1:11435:127\.0\.0\.1:11434/);
  assert.match(tunnel, /while[\s\S]*ssh[\s\S]*sleep/);
  assert.match(tunnel, /ServerAliveInterval=30/);
  assert.match(tunnel, /ServerAliveCountMax=3/);
  assert.match(tunnel, /GPOINT_SSH_IDENTITY_FILE/);
  assert.match(tunnel, /-o IdentitiesOnly=yes/);
});

test('Mac Ollama tunnel retries SSH after a disconnect and stops cleanly on TERM', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'eros-tunnel-test-'));
  const bin = join(directory, 'bin');
  mkdirSync(bin);
  const calls = join(directory, 'ssh-calls');
  const identity = join(directory, 'synthetic-ssh-identity');
  writeFileSync(identity, 'synthetic identity marker\n', { mode: 0o600 });
  const script = fileURLToPath(new URL('open-mac-ollama-tunnel.sh', import.meta.url));
  const node = join(bin, 'node');
  const ssh = join(bin, 'ssh');
  writeFileSync(node, '#!/bin/sh\nexit 0\n');
  writeFileSync(ssh, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$EROS_TEST_SSH_CALLS"\nif [ "$(wc -l < "$EROS_TEST_SSH_CALLS")" -eq 1 ]; then exit 255; fi\nexec /bin/sleep 30\n');
  chmodSync(node, 0o700);
  chmodSync(ssh, 0o700);
  const tunnel = spawn('/bin/sh', [script], {
    env: {
      PATH: `${bin}:/usr/bin:/bin`,
      EROS_PILOT_VPS_SSH_TARGET: 'synthetic@vps.invalid',
      GPOINT_SSH_IDENTITY_FILE: identity,
      EROS_TEST_SSH_CALLS: calls,
    },
    stdio: 'ignore',
  });
  const exit = new Promise((resolve) => tunnel.once('exit', resolve));
  try {
    const deadline = Date.now() + 10_000;
    let lines = [];
    while (Date.now() < deadline) {
      if (existsSync(calls)) lines = readFileSync(calls, 'utf8').trim().split('\n');
      if (lines.length >= 2) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.equal(lines.length, 2, 'tunnel should retry the SSH reverse forward after a disconnect');
    assert.ok(lines.every((line) => line.includes('-R 127.0.0.1:11435:127.0.0.1:11434')));
    assert.ok(lines.every((line) => line.includes(`-i ${identity}`) && line.includes('-o IdentitiesOnly=yes')));
    assert.ok(lines.every((line) => !line.includes('ClearAllForwardings=yes')));
    tunnel.kill('SIGTERM');
    await exit;
  } finally {
    if (tunnel.exitCode === null) {
      tunnel.kill('SIGTERM');
      await exit;
    }
    rmSync(directory, { recursive: true, force: true });
  }
});

test('local Worker fixture refuses non-isolated or non-local mode', () => {
  const worker = readFileSync(new URL('local-test-worker.py', directory), 'utf8');
  assert.match(worker, /GPOINT_EROS_WORKER_LOCAL_TEST/);
  assert.match(worker, /http:\/\/127\.0\.0\.1:3000\/gpoint-worker/);
  assert.match(worker, /DETERMINISTIC_LOCAL_E2E_REPLY/);
  assert.match(worker, /SYNTHETIC_DREAMING_SCAN_RESULT/);
  assert.match(worker, /purpose == 'eros_chat_completion'/);
  assert.match(worker, /SYNTH_CITY_ZEPHYRBAY/);
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(compose, /GPOINT_CHAT_POLICY_TENANT_ID: \$\{EROS_TEST_TENANT_ID/);
  assert.match(compose, /GPOINT_EROS_WORKER_TOKEN: \$\{EROS_TEST_WORKER_TOKEN/);
  assert.match(compose, /GPOINT_EROS_WORKER_URL: http:\/\/127\.0\.0\.1:3000\/gpoint-worker/);
  assert.doesNotMatch(compose, /GPOINT_AGATA_WORKER_(?:URL|TOKEN|LOCAL_TEST)/);
});

test('local Worker fixture treats SIGTERM as a clean stop, not a job timeout', () => {
  const worker = readFileSync(new URL('./local-test-worker.py', import.meta.url), 'utf8');
  assert.match(worker, /if not stopped and time\.monotonic\(\) >= deadline:/);
});

test('local Eros E2E uses persisted GPoint age and explicit consent state', () => {
  const runner = readFileSync(new URL('./run-local-e2e.sh', import.meta.url), 'utf8');
  const e2e = readFileSync(new URL('./local-test-e2e.mjs', import.meta.url), 'utf8');
  assert.match(runner, /INSERT INTO "GpointChatPolicyProfile"/);
  assert.match(e2e, /\/age-assurance\/self-attest/);
  assert.match(e2e, /\/messages\/consent/);
  assert.match(e2e, /\/messages\/consent\/revoke/);
  assert.match(e2e, /CHAT_CONSENT_REQUIRED/);
  assert.doesNotMatch(e2e, /setFixtureState/);
});

test('Compose exposes no test service outside the internal Docker network', (context) => {
  const environment = {
    ...process.env,
    EROS_IMAGE: 'gpoint-eros:test',
    EROS_TEST_PROJECT: 'me36_synthetic_run',
    EROS_TEST_ADMIN_PASSWORD: 'synthetic-only-test-password',
    EROS_TEST_GPOINT_API_KEY: 'synthetic-only-api-key',
    EROS_TEST_BRIDGE_SIGNING_SECRET: 'synthetic-only-bridge-signing-secret-at-least-32-bytes',
    EROS_TEST_TENANT_ID: '7508f36f-774a-48aa-8c6b-42f6f160415d',
    EROS_TEST_SUPABASE_JWT_SECRET: 'synthetic-only-jwt-secret-at-least-32-bytes',
    EROS_TEST_WORKER_TOKEN: 'synthetic-only-worker',
    EROS_TEST_AGATA_TOKEN: 'synthetic-only-agata',
    EROS_TEST_OPENROUTER_API_KEY: '',
  };
  const result = spawnSync(
    'docker',
    ['compose', '-f', 'services/eros-engine/compose.local-test.yml', '--profile', 'test', '--profile', 'worker', 'config', '--format', 'json'],
    { encoding: 'utf8', env: environment },
  );

  if (result.error?.code === 'ENOENT') return context.skip('Docker CLI not installed');
  assert.equal(result.status, 0, result.stderr);
  const compose = JSON.parse(result.stdout);
  assert.equal(compose.services['gpoint-api'].ports, undefined);
  assert.equal(compose.services['gpoint-worker'].ports, undefined);
  assert.equal(compose.services.eros.environment.OPENROUTER_API_KEY, '');
  assert.equal(Object.hasOwn(compose.services.eros.environment, 'EROS_TEST_OPENROUTER_STARTUP_SENTINEL'), false);
  assert.equal(compose.networks.eros_test.internal, true);
});

test('Docker endpoint must be the local Mac Unix socket', () => {
  const socketExists = (path) => path === '/Users/synthetic/.docker/run/docker.sock';
  assert.equal(
    validateDockerEndpoint('unix:///Users/synthetic/.docker/run/docker.sock', {
      home: '/Users/synthetic',
      socketExists,
    }),
    '/Users/synthetic/.docker/run/docker.sock',
  );
  assert.throws(
    () => validateDockerEndpoint('tcp://192.0.2.7:2376', { home: '/Users/synthetic', socketExists }),
    /local_unix_socket_required/,
  );
  assert.throws(
    () => validateDockerEndpoint('unix:///tmp/forwarded-docker.sock', { home: '/Users/synthetic', socketExists }),
    /unapproved_docker_socket/,
  );
  assert.throws(
    () => validateDockerEndpoint('w¾<îÚ$z{-®éÜj×¹•ôÀ)±•…¹ÕÀ ¥ì(€ÉŒôü(€ÑÉ…À€´a%P!U@%9PQI4(€±•…¹ÕÁ}™…¥±•ôÀ(€¥˜l€ˆ‘ÉŒˆ€µ¹”€Àt€˜˜l€ˆ‘½µÁ½Í•}ÍÑ…ÉÑ•ˆ€µ•Ä€ÄtìÑ¡•¸(€€€µ¥É…Ñ½É}½¹Ñ…¥¹•Èô¡½µÁ½Í”ÁÌ€´µ…±°€´µÅÕ¥•Ð•É½Ìµµ¥É…Ñ”€Èø½‘•Ø½¹Õ±°ñðÑÉÕ”¤(€€€¥˜l€µ¸€ˆ‘µ¥É…Ñ½É}½¹Ñ…¥¹•ÈˆtìÑ¡•¸(€€€€€•¡¼1=1}É}5%IQ=I}1=}	%8€ø˜È(€€€€€‘½­•É}±½…°±½Ì€´µÑ…¥°€ÄÀÀ€ˆ‘µ¥É…Ñ½É}½¹Ñ…¥¹•Èˆ€Èø˜Äp(€€€€€€€ðÍ•€µ€µ”€ÌŒ¡Á½ÍÑÉ•Ì¡Å°¤üè¼¼¥myléÍÁ…”éut­ pÅmIQu œœp(€€€€€€€€€€µ”€Ì¼ ¡Á…ÍÍÝ½É‘ñÑ½­•¹ñÍ•É•Ð¥lôèt¬¥myléÍÁ…”éut¬½pÅmIQt½%œœ€ø˜ÈñðÑÉÕ”(€€€€€•¡¼1=1}É}5%IQ=I}1=}9€ø˜È(€€€™¤(€€€½µÁ½Í”±½Ì€´µ¹¼µ½±½ÈÁ½ÍÑÉ•ÌÁ½¥¹Ðµ…Á¤µµ¥É…Ñ”Á½¥¹Ðµ…Á¤•É½ÌÁ½¥¹ÐµÝ½É­•È€Èø½‘•Ø½¹Õ±°ðÉ•À€µ¤€µ€È€‰•ÉÉ½Éñ•á•ÁÑ¥½¹ñ™…¥±•‘ñ™…Ñ…±ñÁ…¹¥ñ‘•¹¥•‘ñ¹½Ð™½Õ¹‘ñÉ•™ÕÍ•ˆðÑ…¥°€´ÄÀÀ€ø˜ÈñðÑÉÕ”(€™¤(€¥˜l€ˆ‘½µÁ½Í•}ÍÑ…ÉÑ•ˆ€µ•Ä€ÄtìÑ¡•¸(€€€½µÁ½Í”‘½Ý¸€´µÙ½±Õµ•Ì€´µÉ•µ½Ù”µ½ÉÁ¡…¹Ì€ø½‘•Ø½¹Õ±°€Èø˜Äñð±•…¹ÕÁ}™…¥±•ôÄ(€™¤(€¥˜l€ˆ‘ÉÕ¹}½Ý¹•ˆ€µ•Ä€ÄtìÑ¡•¸(€€€¥˜¥¹Ñ•É…Ñ¥½¹}¥‘•¹Ñ¥Ñäô¡¥¹ÍÁ•Ñ}ÉÕ¹}½¹Ñ…¥¹•È€ˆ‘¥¹Ñ•É…Ñ¥½¹}½¹Ñ…¥¹•Èˆ¤ìÑ¡•¸(€€€€€¥¹Ñ•É…Ñ¥½¹}½Ý¹•Èô‘í¥¹Ñ•É…Ñ¥½¹}¥‘•¹Ñ¥ÑäŒ©ñô(€€€€€¥˜l€ˆ‘¥¹Ñ•É…Ñ¥½¹}½Ý¹•Èˆ€ô€ˆ‘ÁÉ½©•ÐˆtìÑ¡•¸(€€€€€€€‘½­•É}±½…°É´€µ˜€ˆ‘¥¹Ñ•É…Ñ¥½¹}½¹Ñ…¥¹•Èˆ€ø½‘•Ø½¹Õ±°ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€•±Í”(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€™¤(€€€•±Í”(€€€€€¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌôü(€€€€€l€ˆ‘¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌˆ€µ•Ä€Ètñð±•…¹ÕÁ}™…¥±•ôÄ(€€€™¤(€€€½¹Ñ…¥¹•ÉÌô¡‘½­•É}±½…°ÁÌ€µ…Ä€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€™½È½¹Ñ…¥¹•È¥¸€‘í½¹Ñ…¥¹•ÉÌèµôì‘¼(€€€€€½¹Ñ…¥¹•É}½Ý¹•Èô¡‘½­•É}±½…°¥¹ÍÁ•Ð€ˆ‘½¹Ñ…¥¹•Èˆ€´µ™½Éµ…Ð€íí¥¹‘•à€¹½¹™¥œ¹1…‰•±Ì€‰½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ð‰õôœ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€¥˜l€ˆ‘½¹Ñ…¥¹•É}½Ý¹•Èˆ€ô€ˆ‘ÁÉ½©•ÐˆtìÑ¡•¸(€€€€€€€‘½­•É}±½…°É´€µ˜€ˆ‘½¹Ñ…¥¹•Èˆ€ø½‘•Ø½¹Õ±°ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€•±Í”(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€™¤(€€€‘½¹”(€€€½¹Ñ…¥¹•ÉÌô¡‘½­•É}±½…°ÁÌ€µ…Ä€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€¹•ÑÝ½É­Ìô¡‘½­•É}±½…°¹•ÑÝ½É¬±Ì€µÄ€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€™½È¹•ÑÝ½É¬¥¸€‘í¹•ÑÝ½É­Ìèµôì‘¼(€€€€€¹•ÑÝ½É­}½Ý¹•Èô¡‘½­•É}±½…°¹•ÑÝ½É¬¥¹ÍÁ•Ð€ˆ‘¹•ÑÝ½É¬ˆ€´µ™½Éµ…Ð€íí¥¹‘•à€¹1…‰•±Ì€‰½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ð‰õôœ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€¹•ÑÝ½É­}¹…µ”ô¡‘½­•É}±½…°¹•ÑÝ½É¬¥¹ÍÁ•Ð€ˆ‘¹•ÑÝ½É¬ˆ€´µ™½Éµ…Ð€íì¹9…µ•õôœ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€¹•ÑÝ½É­}½¹Ñ…¥¹•ÉÌô¡‘½­•É}±½…°¹•ÑÝ½É¬¥¹ÍÁ•Ð€ˆ‘¹•ÑÝ½É¬ˆ€´µ™½Éµ…Ð€íí±•¸€¹½¹Ñ…¥¹•ÉÍõôœ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€¥˜l€ˆ‘¹•ÑÝ½É­}½Ý¹•Èˆ€ô€ˆ‘ÁÉ½©•Ðˆt€˜˜l€ˆ‘¹•ÑÝ½É­}¹…µ”ˆ€ô€ˆ‘íÁÉ½©•Ñõ}•É½Í}Ñ•ÍÐˆt€˜˜l€ˆ‘¹•ÑÝ½É­}½¹Ñ…¥¹•ÉÌˆ€ô€ÀtìÑ¡•¸(€€€€€€€‘½­•É}±½…°¹•ÑÝ½É¬É´€ˆ‘¹•ÑÝ½É¬ˆ€ø½‘•Ø½¹Õ±°ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€€€•±Í”(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€™¤(€€€‘½¹”(€€€Ù½±Õµ•Ìô¡‘½­•É}±½…°Ù½±Õµ”±Ì€µÄ€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤ñð±•…¹ÕÁ}™…¥±•ôÄ(€€€¥˜l€µ¸€ˆ‘í½¹Ñ…¥¹•ÉÌèµô‘í¹•ÑÝ½É­Ìèµô‘íÙ½±Õµ•ÌèµôˆtìÑ¡•¸±•…¹ÕÁ}™…¥±•ôÄì™¤(€™¤(€¥˜l€µ˜€ˆ‘ÉÕ¹}µ…¹¥™•ÍÐˆtìÑ¡•¸(€€€™½ÈÍ•ÉÙ¥”¥¸Á½¥¹Ðµ…Á¤Á½¥¹Ðµ…Á¤µµ¥É…Ñ”Á½¥¹ÐµÝ½É­•Èì‘¼(€€€€€¥µ…•}É•˜ôˆ‘íÁÉ½©•Ñô´‘íÍ•ÉÙ¥•ôé±…Ñ•ÍÐˆ(€€€€€µ…¹¥™•ÍÑ}•¹ÑÉäô¡…Ý¬€µðœ€µØÉ•˜ôˆ‘¥µ…•}É•˜ˆ€œÄ€ôôÉ•˜ìÁÉ¥¹Ðì•á¥Ðôœ€ˆ‘ÉÕ¹}µ…¹¥™•ÍÐˆ¤(€€€€€¥˜l€µè€ˆ‘µ…¹¥™•ÍÑ}•¹ÑÉäˆtìÑ¡•¸(€€€€€€€€ŒÑ…œ±•™Ð‰•¡¥¹…™Ñ•È„™…¥±•‰Õ¥±½¥¹ÍÁ•Ñ¥½¸¡…Ì¹¼Ù•É¥™¥•(€€€€€€€€Œ¥‘•¹Ñ¥Ñä¸AÉ•Í•ÉÙ”¥Ð…¹µ…­”±•…¹ÕÀÙ¥Í¥‰±ä™…¥°±½Í•¸(€€€€€€€¥˜¥µ…•}¥‘•¹Ñ¥Ñäô¡¥¹ÍÁ•Ñ}ÉÕ¹}¥µ…”€ˆ‘¥µ…•}É•˜ˆ¤ìÑ¡•¸(€€€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€€€€€•¡¼€‰1=1}É}%5}=]9IM!%@õU9YI%%Ñ…œô‘¥µ…•}É•˜É•…Í½¸õ¹½Ñ}¥¹}µ…¹¥™•ÍÐˆ€ø˜È(€€€€€€€•±Í”(€€€€€€€€€¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌôü(€€€€€€€€€¥˜l€ˆ‘¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌˆ€µ¹”€ÈtìÑ¡•¸±•…¹ÕÁ}™…¥±•ôÄì™¤(€€€€€€€™¤(€€€€€€€½¹Ñ¥¹Õ”(€€€€€™¤(€€€€€%LôðœÉ•…€µÈµ…¹¥™•ÍÑ}É•˜•áÁ•Ñ•‘}¥µ…•}¥¥µ…•}½Ý¹•È•áÁ•Ñ•‘}Í•ÉÙ¥”€ðñ=(‘µ…¹¥™•ÍÑ}•¹ÑÉä)=(€€€€€…Í”€ˆ‘¥µ…•}É•˜ˆ¥¸(€€€€€€€€ˆ‘íÁÉ½©•ÑôµÁ½¥¹Ðµ…Á¤é±…Ñ•ÍÐ‰ðˆ‘íÁÉ½©•ÑôµÁ½¥¹Ðµ…Á¤µµ¥É…Ñ”é±…Ñ•ÍÐ‰ðˆ‘íÁÉ½©•ÑôµÁ½¥¹ÐµÝ½É­•Èé±…Ñ•ÍÐˆ¤€ìì(€€€€€€€€¨¤±•…¹ÕÁ}™…¥±•ôÄì•¡¼€‰1=1}É}%5}=]9IM!%@õ%0ˆ€ø˜Èì½¹Ñ¥¹Õ”€ìì(€€€€€•Í…Œ(€€€€€¥˜¥µ…•}¥‘•¹Ñ¥Ñäô¡¥¹ÍÁ•Ñ}ÉÕ¹}¥µ…”€ˆ‘¥µ…•}É•˜ˆ¤ìÑ¡•¸(€€€€€€€€è(€€€€€•±Í”(€€€€€€€¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌôü(€€€€€€€¥˜l€ˆ‘¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌˆ€µ•Ä€ÈtìÑ¡•¸(€€€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€€€€€•¡¼€‰1=1}É}%5}%9MAPõ%0Ñ…œô‘¥µ…•}É•˜É•…Í½¸õµ…¹¥™•ÍÑ}¥µ…•}µ¥ÍÍ¥¹œˆ€ø˜È(€€€€€€€•±Í”(€€€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€€€™¤(€€€€€€€½¹Ñ¥¹Õ”(€€€€€™¤(€€€€€ÕÉÉ•¹Ñ}¥µ…•}¥ô‘í¥µ…•}¥‘•¹Ñ¥Ñä”•ð©ô(€€€€€¥µ…•}±…‰•±Ìô‘í¥µ…•}¥‘•¹Ñ¥ÑäŒ©ñô(€€€€€¥˜l€ˆ‘µ…¹¥™•ÍÑ}É•˜ˆ€„ô€ˆ‘¥µ…•}É•˜ˆtñðl€ˆ‘ÕÉÉ•¹Ñ}¥µ…•}¥ˆ€„ô€ˆ‘•áÁ•Ñ•‘}¥µ…•}¥ˆtñðl€ˆ‘¥µ…•}±…‰•±Ìˆ€„ô€ˆ‘ÁÉ½©•Ñð‘•áÁ•Ñ•‘}Í•ÉÙ¥”ˆtñðl€ˆ‘¥µ…•}½Ý¹•Èˆ€„ô€ˆ‘ÁÉ½©•Ðˆtñðl€ˆ‘•áÁ•Ñ•‘}Í•ÉÙ¥”ˆ€„ô€ˆ‘Í•ÉÙ¥”ˆtìÑ¡•¸(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€€€•¡¼€‰1=1}É}%5}=]9IM!%@õ%0Ñ…œô‘¥µ…•}É•˜É•…Í½¸õ¥‘•¹Ñ¥Ñå}¡…¹•ˆ€ø˜È(€€€€€€€½¹Ñ¥¹Õ”(€€€€€™¤(€€€€€¥µ…•}ÕÍ•ÉÌô¡‘½­•É}±½…°ÁÌ€µ…Ä€´µ™¥±Ñ•È€‰…¹•ÍÑ½Èô‘•áÁ•Ñ•‘}¥µ…•}¥ˆ¤ñðì±•…¹ÕÁ}™…¥±•ôÄì½¹Ñ¥¹Õ”ìô(€€€€€¥˜l€µ¸€ˆ‘¥µ…•}ÕÍ•ÉÌˆtìÑ¡•¸(€€€€€€€•¡¼€‰1=1}É}%5}IQ%9õ%9}UMÍ•ÉÙ¥•}Ñ…œô‘¥µ…•}É•˜ˆ€ø˜È(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€•±¥˜€„‘½­•É}±½…°¥µ…”É´€ˆ‘¥µ…•}É•˜ˆ€ø½‘•Ø½¹Õ±°ìÑ¡•¸(€€€€€€€±•…¹ÕÁ}™…¥±•ôÄ(€€€€€•±Í”(€€€€€€€•¡¼€‰1=1}É}%5}19U@õAMLÑ…œô‘¥µ…•}É•˜ˆ(€€€€€™¤(€€€‘½¹”(€™¤(€É´€µÉ˜€ˆ‘ÑµÀˆñð±•…¹ÕÁ}™…¥±•ôÄ(€¥˜l€ˆ‘±•…¹ÕÁ}™…¥±•ˆ€µ¹”€ÀtìÑ¡•¸(€€€•¡¼€‰1=1}É}19U@õ%0ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ€ø˜È(€€€l€ˆ‘ÉŒˆ€µ¹”€ÀtñðÉŒôÄ(€•±Í”(€€€•¡¼€‰1=1}É}19U@õAMLÁÉ½©•Ðô‘ÁÉ½©•Ðˆ(€™¤(€•á¥Ð€ˆ‘ÉŒˆ)ô)ÑÉ…À±•…¹ÕÀa%P!U@%9PQI4)™½ÈÍ•ÉÙ¥”¥¸Á½¥¹Ðµ…Á¤Á½¥¹Ðµ…Á¤µµ¥É…Ñ”Á½¥¹ÐµÝ½É­•Èì‘¼(€¥µ…•}É•˜ôˆ‘íÁÉ½©•Ñô´‘íÍ•ÉÙ¥•ôé±…Ñ•ÍÐˆ(€¥˜¥µ…•}¥‘•¹Ñ¥Ñäô¡¥¹ÍÁ•Ñ}ÉÕ¹}¥µ…”€ˆ‘¥µ…•}É•˜ˆ¤ìÑ¡•¸(€€€•¡¼€‰1=1}É}IM=UI}=11%M%=8õ%0¥µ…”ô‘¥µ…•}É•˜ˆ€ø˜È(€€€•á¥Ð€È(€•±Í”(€€€¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌôü(€€€l€ˆ‘¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌˆ€µ•Ä€Ètñðì•¡¼€‰1=1}É}%5}%9MAPõ%0Ñ…œô‘¥µ…•}É•˜Á¡…Í”õÁÉ•™±¥¡Ðˆ€ø˜Èì•á¥Ð€Èìô(€™¤)‘½¹”)™½ÈÉ•Í½ÕÉ•}­¥¹¥¸½¹Ñ…¥¹•È¹•ÑÝ½É¬Ù½±Õµ”ì‘¼(€…Í”€ˆ‘É•Í½ÕÉ•}­¥¹ˆ¥¸(€€€½¹Ñ…¥¹•È¤•á¥ÍÑ¥¹œô¡‘½­•É}±½…°ÁÌ€µ…Ä€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤€ìì(€€€¹•ÑÝ½É¬¤•á¥ÍÑ¥¹œô¡‘½­•É}±½…°¹•ÑÝ½É¬±Ì€µÄ€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤€ìì(€€€Ù½±Õµ”¤•á¥ÍÑ¥¹œô¡‘½­•É}±½…°Ù½±Õµ”±Ì€µÄ€´µ™¥±Ñ•È€‰±…‰•°õ½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ¤€ìì(€•Í…Œ(€l€µè€ˆ‘•á¥ÍÑ¥¹œˆtñðì•¡¼€‰1=1}É}IM=UI}=11%M%=8õ%0ÑåÁ”ô‘É•Í½ÕÉ•}­¥¹ˆ€ø˜Èì•á¥Ð€Èìô)‘½¹”)¥˜¥¹Ñ•É…Ñ¥½¹}¥‘•¹Ñ¥Ñäô¡¥¹ÍÁ•Ñ}ÉÕ¹}½¹Ñ…¥¹•È€ˆ‘¥¹Ñ•É…Ñ¥½¹}½¹Ñ…¥¹•Èˆ¤ìÑ¡•¸(€•¡¼1=1}É}IM=UI}=11%M%=8õ%0ÑåÁ”õ¥¹Ñ•É…Ñ¥½¹}½¹Ñ…¥¹•È€ø˜È(€•á¥Ð€È)•±Í”(€¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌôü(€l€ˆ‘¥¹ÍÁ•Ñ}ÍÑ…ÑÕÌˆ€µ•Ä€Ètñðì•¡¼1=1}É}=9Q%9I}%9MAPõ%0Á¡…Í”õÁÉ•™±¥¡Ð€ø˜Èì•á¥Ð€Èìô)™¤)ÉÕ¹}½Ý¹•ôÄ)µ­‘¥È€µ´€ÜÀÀ€ˆ‘ÑµÀ½¡½µ”ˆ)™½ÈÍ•ÉÙ¥”¥¸Á½¥¹Ðµ…Á¤Á½¥¹Ðµ…Á¤µµ¥É…Ñ”Á½¥¹ÐµÝ½É­•Èì‘¼(€¥µ…•}É•˜ôˆ‘íÁÉ½©•Ñô´‘íÍ•ÉÙ¥•ôé±…Ñ•ÍÐˆ(€•¡¼€‰1=1}É}%5}	U%1õ	%8Í•ÉÙ¥”ô‘Í•ÉÙ¥”ˆ(€½µÁ½Í”€´µÁÉ½™¥±”Ý½É­•È‰Õ¥±€ˆ‘Í•ÉÙ¥”ˆ(€¥µ…•}¥‘•¹Ñ¥Ñäô¡‘½­•É}±½…°¥µ…”¥¹ÍÁ•Ð€ˆ‘¥µ…•}É•˜ˆ€´µ™½Éµ…Ð€íì¹%‘õõñíí¥¹‘•à€¹½¹™¥œ¹1…‰•±Ì€‰½´¹Á½¥¹Ð¹µ”ÌØ¹ÉÕ¸‰õõñíí¥¹‘•à€¹½¹™¥œ¹1…‰•±Ì€‰½´¹Á½¥¹Ð¹µ”ÌØ¹Í•ÉÙ¥”‰õôœ¤(€•áÁ•Ñ•‘}¥‘•¹Ñ¥Ñäôˆ‘ÁÉ½©•Ñð‘Í•ÉÙ¥”ˆ(€l€ˆ‘í¥µ…•}¥‘•¹Ñ¥ÑäŒ©ñôˆ€ô€ˆ‘•áÁ•Ñ•‘}¥‘•¹Ñ¥Ñäˆtñðì•¡¼€‰1=1}É}%5}=]9IM!%@õ%0Ñ…œô‘¥µ…•}É•˜É•…Í½¸õ‰Õ¥±‘}±…‰•±}µ¥Íµ…Ñ ˆ€ø˜Èì•á¥Ð€Äìô(€€ŒI•½É•… ¥µ…”¥µµ•‘¥…Ñ•±ä…™Ñ•È¥ÑÌ½Ý¸‰Õ¥±…¹½Ý¹•ÉÍ¡¥ÀÙ…±¥‘…Ñ¥½¸°(€€ŒÍ¼„±…Ñ•ÈÍ•ÉÙ¥”‰Õ¥±™…¥±ÕÉ”…¹¹½Ð½ÉÁ¡…¸•…É±¥•ÈÙ•É¥™¥•¥µ…•Ì¸(€ÁÉ¥¹Ñ˜€œ•Íð•Íð•Íð•Íq¸œ€ˆ‘¥µ…•}É•˜ˆ€ˆ‘í¥µ…•}¥‘•¹Ñ¥Ñä”•ð©ôˆ€ˆ‘ÁÉ½©•Ðˆ€ˆ‘Í•ÉÙ¥”ˆ€øøˆ‘ÉÕ¹}µ…¹¥™•ÍÐˆ(€•¡¼€‰1=1}É}%5}	U%1õAMLÍ•ÉÙ¥”ô‘Í•ÉÙ¥”ˆ)‘½¹”)•¡¼1=1}É}=5A=M}MQIPõ	%8)½µÁ½Í•}ÍÑ…ÉÑ•ôÄ)½µÁ½Í”ÕÀ€µ€´µÝ…¥Ð€´µÝ…¥ÐµÑ¥µ•½ÕÐ€ÈÐÀÁ½ÍÑÉ•Ì)½µÁ½Í”ÕÀ€µ€´µ¹¼µ‘•ÁÌ•É½Ìµµ¥É…Ñ”)µ¥É…Ñ½É}½¹Ñ…¥¹•Èô¡½µÁ½Í”ÁÌ€´µ…±°€´µÅÕ¥•Ð•É½Ìµµ¥É…Ñ”¤)l€µ¸€ˆ‘µ¥É…Ñ½É}½¹Ñ…¥¹•Èˆtñðì•¡¼1=1}É}MQ=-}5%IQ%=9Lõ%0É•…Í½¸õ½¹Ñ…¥¹•É}µ¥ÍÍ¥¹œ€ø˜Èì•á¥Ð€Äìô)µ¥É…Ñ½É}ÍÑ…ÑÕÌô¡‘½­•É}±½…°Ý…¥Ð€ˆ‘µ¥É…Ñ½É}½¹Ñ…¥¹•Èˆ¤)l€ˆ‘µ¥É…Ñ½É}ÍÑ…ÑÕÌˆ€ô€Àtñðì•¡¼€‰1=1}É}MQ=-}5%IQ%=9Lõ%0•á¥Ñ}½‘”ô‘µ¥É…Ñ½É}ÍÑ…ÑÕÌˆ€ø˜Èì•á¥Ð€Äìô)•¡¼1=1}É}MQ=-}5%IQ%=9LõAML)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€ððME0œ)I9PUM=8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ”°•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌì)I9P10=810Q	1L%8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ”ì)I9P10=810MEU9L%8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ”ì)I9PaUQ=810U9Q%=9L%8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ”ì)1QHU1PAI%Y%1L=HI=1•É½Í}µ¥É…Ñ½È%8M!5•¹¥¹”I9P10=8Q	1LQ<•É½Í}ÉÕ¹Ñ¥µ”ì)1QHU1PAI%Y%1L=HI=1•É½Í}µ¥É…Ñ½È%8M!5•¹¥¹”I9P10=8MEU9LQ<•É½Í}ÉÕ¹Ñ¥µ”ì)1QHU1PAI%Y%1L=HI=1•É½Í}µ¥É…Ñ½È%8M!5•¹¥¹”I9PaUQ=8U9Q%=9LQ<•É½Í}ÉÕ¹Ñ¥µ”ì)I9PM1P°%9MIP°UAQ°1Q=810Q	1L%8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌì)I9PUM°M1P°UAQ=810MEU9L%8M!5•¹¥¹”Q<•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌì)1QHU1PAI%Y%1L=HI=1•É½Í}µ¥É…Ñ½È%8M!5•¹¥¹”I9PM1P°%9MIP°UAQ°1Q=8Q	1LQ<•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌì)1QHU1PAI%Y%1L=HI=1•É½Í}µ¥É…Ñ½È%8M!5•¹¥¹”I9PUM°M1P°UAQ=8MEU9LQ<•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌì)ME0)½µÁ½Í”ÕÀ€µ€´µÝ…¥Ð€´µÝ…¥ÐµÑ¥µ•½ÕÐ€ÈÐÀ)•¡¼1=1}É}=5A=M}MQIPõAML)•É½Í}…Á¥}ÁÉ¥Ù¥±••Ìô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÐ€ððME0œ)]%Q É•±…Ñ¥½¹}ÁÉ¥Ù¥±••ÌL€ (€M1PŒ¹½¥°Œ¹É•±­¥¹°À¹ÁÉ¥Ù¥±•”(€I=4Á}±…ÍÌŒ(€)=%8Á}¹…µ•ÍÁ…”¸=8¸¹½¥€ôŒ¹É•±¹…µ•ÍÁ…”(€I=ML)=%8€¡Y1UL€ M1Pœ¤°€ %9MIPœ¤°€ UAQœ¤°€ 1Qœ¤°€ QIU9Qœ¤°€ II9Lœ¤°€ QI%Hœ¤¤LÀ¡ÁÉ¥Ù¥±•”¤(€]!I¸¹¹ÍÁ¹…µ”9=P1%-€Áq|”œ9¸¹¹ÍÁ¹…µ”€ðø€¥¹™½Éµ…Ñ¥½¹}Í¡•µ„œ(€€€9Œ¹É•±­¥¹%8€ Èœ°€Àœ°€Øœ°€´œ°€˜œ¤(€€€9¡…Í}Ñ…‰±•}ÁÉ¥Ù¥±•” Á½¥¹Ñ}…Á¥}Ñ•ÍÐœ°Œ¹½¥°À¹ÁÉ¥Ù¥±•”¤(¤°½±Õµ¹}ÁÉ¥Ù¥±••ÌL€ (€M1PŒ¹½¥(€I=4Á}±…ÍÌŒ(€)=%8Á}¹…µ•ÍÁ…”¸=8¸¹½¥€ôŒ¹É•±¹…µ•ÍÁ…”(€)=%8Á}…ÑÑÉ¥‰ÕÑ”„=8„¹…ÑÑÉ•±¥€ôŒ¹½¥9„¹…ÑÑ¹Õ´€ø€À99=P„¹…ÑÑ¥Í‘É½ÁÁ•(€I=ML)=%8€¡Y1UL€ M1Pœ¤°€ %9MIPœ¤°€ UAQœ¤°€ II9Lœ¤¤LÀ¡ÁÉ¥Ù¥±•”¤(€]!I¸¹¹ÍÁ¹…µ”9=P1%-€Áq|”œ9¸¹¹ÍÁ¹…µ”€ðø€¥¹™½Éµ…Ñ¥½¹}Í¡•µ„œ(€€€9Œ¹É•±­¥¹%8€ Èœ°€Àœ°€Øœ°€´œ°€˜œ¤(€€€9¡…Í}½±Õµ¹}ÁÉ¥Ù¥±•” Á½¥¹Ñ}…Á¥}Ñ•ÍÐœ°Œ¹½¥°„¹…ÑÑ¹Õ´°À¹ÁÉ¥Ù¥±•”¤(¤°Í•ÅÕ•¹•}ÁÉ¥Ù¥±••ÌL€ (€M1PŒ¹½¥(€I=4Á}±…ÍÌŒ(€)=%8Á}¹…µ•ÍÁ…”¸=8¸¹½¥€ôŒ¹É•±¹…µ•ÍÁ…”(€I=ML)=%8€¡Y1UL€ UMœ¤°€ M1Pœ¤°€ UAQœ¤¤LÀ¡ÁÉ¥Ù¥±•”¤(€]!I¸¹¹ÍÁ¹…µ”9=P1%-€Áq|”œ9¸¹¹ÍÁ¹…µ”€ðø€¥¹™½Éµ…Ñ¥½¹}Í¡•µ„œ(€€€9Œ¹É•±­¥¹€ô€Lœ9¡…Í}Í•ÅÕ•¹•}ÁÉ¥Ù¥±•” Á½¥¹Ñ}…Á¥}Ñ•ÍÐœ°Œ¹½¥°À¹ÁÉ¥Ù¥±•”¤(¤)M1P(€¡…Í}‘…Ñ…‰…Í•}ÁÉ¥Ù¥±•” Á½¥¹Ñ}…Á¥}Ñ•ÍÐœ°€•É½Í}Ñ•ÍÐœ°€=99Pœ¤èéÑ•áÐñð€ðœñð(€¡…Í}Í¡•µ…}ÁÉ¥Ù¥±•” Á½¥¹Ñ}…Á¥}Ñ•ÍÐœ°€•¹¥¹”œ°€UMœ¤èéÑ•áÐñð€ðœñð(€€¡M1P½Õ¹Ð ¨¤I=4É•±…Ñ¥½¹}ÁÉ¥Ù¥±••Ì¤èéÑ•áÐñð€ðœñð(€€¡M1P½Õ¹Ð ¨¤I=4½±Õµ¹}ÁÉ¥Ù¥±••Ì¤èéÑ•áÐñð€ðœñð(€€¡M1P½Õ¹Ð ¨¤I=4Í•ÅÕ•¹•}ÁÉ¥Ù¥±••Ì¤èéÑ•áÐì)ME0(¤)l€ˆ‘•É½Í}…Á¥}ÁÉ¥Ù¥±••Ìˆ€ô€™…±Í•ñ™…±Í•ðÁðÁðÀœtñðì(€•¡¼€‰1=1}É}I=M}	}A%}%M=1Q%=8õ%0¡•­Ìô‘•É½Í}…Á¥}ÁÉ¥Ù¥±••Ì•áÁ•Ñ•õ™…±Í•ñ™…±Í•ðÁðÁðÀˆ€ø˜È(€•á¥Ð€Ä)ô)•¡¼€‰1=1}É}I=M}	}A%}%M=1Q%=8õAML…Á¥}É½±•}½¹¹•Ðõ™…±Í”Í¡•µ…}ÕÍ…”õ™…±Í”Ñ…‰±•}ÁÉ¥Ù¥±••ÌôÀ½±Õµ¹}ÁÉ¥Ù¥±••ÌôÀÍ•ÅÕ•¹•}ÁÉ¥Ù¥±••ÌôÀˆ)Í••‘}½ÕÑÁÕÐô¡½µÁ½Í”•á•Œ€µP•É½Ì€½ÕÍÈ½±½…°½‰¥¸½•É½Ìµ•¹¥¹”Í••µÁ•ÉÍ½¹…Ì€½•ÑŒ½•É½Ìµ•¹¥¹”½Ñ•ÍÐµÁ•ÉÍ½¹…Ì€Èø˜Ä¤ñðì(€ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Í••‘}½ÕÑÁÕÐˆ€ø˜È(€•¡¼1=1}É}MQ=-}AIM=9}Mõ%0€ø˜È(€•á¥Ð€Ä)ô)ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Í••‘}½ÕÑÁÕÐˆ)•¹½µ•}¥ô¡ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Í••‘}½ÕÑÁÕÐˆðÉ•À€Må¹Ñ¡•Ñ¥ŒÉ½ÌA•ÉÍ½¹„œðÉ•À€µ¼€lÀ´å„µ™uìáôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÄÉôœðÑ…¥°€´Ä¤)ml€ˆ‘•¹½µ•}¥ˆ€õøylÀ´å„µ™uìáôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÄÉôutñðì(€•¡¼1=1}É}MQ=-}AIM=9}Mõ%0É•…Í½¸õ•¹½µ•}¥‘}µ¥ÍÍ¥¹œ€ø˜È(€•á¥Ð€Ä)ô)•¡¼1=1}É}MQ=-}AIM=9}MõAML)•É½Í}É±Í}ÍÑ…ÑÌô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒ€‰M1P½Õ¹Ð ¨¤ñð€ðœñð½Õ¹Ð ¨¤%1QH€¡]!IŒ¹É•±É½ÝÍ•ÕÉ¥Ñä¤ñð€ðœñð½Õ¹Ð ¨¤%1QH€¡]!IŒ¹É•±™½É•É½ÝÍ•ÕÉ¥Ñä¤ñð€ðœñð€¡M1P½Õ¹Ð ¨¤I=4Á}Á½±¥¥•Ì]!IÍ¡•µ…¹…µ”ô•¹¥¹”œ¤I=4Á}±…ÍÌŒ)=%8Á}¹…µ•ÍÁ…”¸=8¸¹½¥õŒ¹É•±¹…µ•ÍÁ…”]!I¸¹¹ÍÁ¹…µ”ô•¹¥¹”œ9Œ¹É•±­¥¹%8€ Èœ°Àœ¤ìˆ¤)%LôðœÉ•…€µÈ•É½Í}Ñ…‰±•Ì•É½Í}É±Í}•¹…‰±••É½Í}É±Í}™½É••É½Í}Á½±¥å}½Õ¹Ð€ðñ=(‘•É½Í}É±Í}ÍÑ…ÑÌ)=)•É½Í}ÉÕ¹Ñ¥µ•}‰åÁ…ÍÌô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒ€‰M1PÉ½±‰åÁ…ÍÍÉ±ÌI=4Á}É½±•Ì]!IÉ½±¹…µ”ô•É½Í}ÉÕ¹Ñ¥µ”œìˆ¤)•É½Í}…‘µ¥¹}Á•ÉÍ½¹…Ìô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÅÑŒ€MPI=1•É½Í}ÉÕ¹Ñ¥µ”ìM1P½Õ¹Ð ¨¤I=4•¹¥¹”¹Á•ÉÍ½¹…}•¹½µ•Ììœ¤)•É½Í}ÍÑÉ¥Ñ}Á•ÉÍ½¹…Ìô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÅÑŒ€MPI=1•É½Í}ÉÕ¹Ñ¥µ•}¹½‰åÁ…ÍÌìM1P½Õ¹Ð ¨¤I=4•¹¥¹”¹Á•ÉÍ½¹…}•¹½µ•Ììœ¤)l€ˆ‘•É½Í}ÉÕ¹Ñ¥µ•}‰åÁ…ÍÌˆ€ôÐt€˜˜l€ˆ‘•É½Í}…‘µ¥¹}Á•ÉÍ½¹…Ìˆ€µÐ€Àt€˜˜l€ˆ‘•É½Í}ÍÑÉ¥Ñ}Á•ÉÍ½¹…Ìˆ€ô€Àt€˜˜l€ˆ‘•É½Í}Ñ…‰±•Ìˆ€µÐ€Àt€˜˜l€ˆ‘•É½Í}É±Í}•¹…‰±•ˆ€ô€ˆ‘•É½Í}Ñ…‰±•Ìˆt€˜˜l€ˆ‘•É½Í}É±Í}™½É•ˆ€ô€Àt€˜˜l€ˆ‘•É½Í}Á½±¥å}½Õ¹Ðˆ€ô€Àtñðì(€•¡¼€‰1=1}É}I=M}Q	M}I1M}1%5%QQ%=8õU9aAQÑ…‰±•Ìô‘•É½Í}Ñ…‰±•Ì•¹…‰±•ô‘•É½Í}É±Í}•¹…‰±•™½É•ô‘•É½Í}É±Í}™½É•ÉÕ¹Ñ¥µ•}É½±•}É½ÝÌô‘•É½Í}…‘µ¥¹}Á•ÉÍ½¹…ÌÍÑÉ¥Ñ}É½±•}É½ÝÌô‘•É½Í}ÍÑÉ¥Ñ}Á•ÉÍ½¹…ÌÁ½±¥¥•Ìô‘•É½Í}Á½±¥å}½Õ¹ÐÉÕ¹Ñ¥µ•}‰åÁ…ÍÌô‘•É½Í}ÉÕ¹Ñ¥µ•}‰åÁ…ÍÌˆ€ø˜È(€•á¥Ð€Ä)ô)•¡¼€‰1=1}É}I=M}Q	M}I1M}1%5%QQ%=8õ=9%I5Ñ…‰±•Ìô‘•É½Í}Ñ…‰±•Ì•¹…‰±•ô‘•É½Í}É±Í}•¹…‰±•™½É•ô‘•É½Í}É±Í}™½É•ÉÕ¹Ñ¥µ•}É½±•}É½ÝÌô‘•É½Í}…‘µ¥¹}Á•ÉÍ½¹…ÌÍÑÉ¥Ñ}É½±•}É½ÝÌô‘•É½Í}ÍÑÉ¥Ñ}Á•ÉÍ½¹…ÌÁ½±¥¥•Ìô‘•É½Í}Á½±¥å}½Õ¹ÐÉÕ¹Ñ¥µ•}‰åÁ…ÍÌô‘•É½Í}ÉÕ¹Ñ¥µ•}‰åÁ…ÍÌˆ)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€ððME0œ)I9PM1P°%9MIP°UAQ°1Q=810Q	1L%8M!5•¹¥¹”Q<Á½¥¹Ñ}Ý½É­•É}Ñ•ÍÐì)I9PUM°M1P°UAQ=810MEU9L%8M!5•¹¥¹”Q<Á½¥¹Ñ}Ý½É­•É}Ñ•ÍÐì)ME0)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€ððME0œ)%9MIP%9Q<€‰UÍ•Èˆ€¡¥°‰Ñ•±•É…µUÍ•É%ˆ°‰Ñ•±•É…µUÍ•É¹…µ”ˆ°‰™¥ÉÍÑ9…µ”ˆ°‰É•…Ñ•‘Ðˆ°‰ÕÁ‘…Ñ•‘Ðˆ¤Y1UL€ œÄÀÀÀÀÀÀÀ´ÀÀÀÀ´ÐÀÀÀ´àÀÀÀ´ÀÀÀÀÀÀÀÀÀÀÀÄœ°äÀÀÀÀÀÀÀÄ°•É½Í}Ñ•ÍÑ}‘¥…±½Õ”œ°Må¹Ñ¡•Ñ¥Œœ±¹½Ü ¤±¹½Ü ¤¤ì)%9MIP%9Q<€‰É•…Ñ½Èˆ€¡¥°‰‘¥ÍÁ±…å9…µ”ˆ±Í±Õœ°‰É•…Ñ•‘Ðˆ¤Y1UL€ œÈÀÀÀÀÀÀÀ´ÀÀÀÀ´ÐÀÀÀ´àÀÀÀ´ÀÀÀÀÀÀÀÀÀÀÀÈœ°Må¹Ñ¡•Ñ¥ŒÉ½ÌA•ÉÍ½¹„œ°•É½ÌµÑ•ÍÐµÁ•ÉÍ½¹„œ±¹½Ü ¤¤ì)ME0)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€ðñME0)%9MIP%9Q<€‰Á½¥¹Ñ¡…ÑA½±¥åAÉ½™¥±”ˆ(€€¡¥°‰Í½ÕÉ•Q•¹…¹Ñ%ˆ±Ù•ÉÍ¥½¸°‰Ñ•ÉµÍY•ÉÍ¥½¸ˆ°‰ÁÉ¥Ù…åY•ÉÍ¥½¸ˆ±ÍÑ…ÑÕÌ°‰•™™•Ñ¥Ù•É½´ˆ±ÉÕ±•Ì¤)Y1UL(€€ •É½Í}Ñ•ÍÑ}Á½¥¹Ñ}Á½±¥äœ°œ‘Ñ•¹…¹Ðœ°Ä°Á½¥¹ÐµÑ•ÉµÌµØÄœ°Á½¥¹ÐµÁÉ¥Ù…äµØÄœ°…Ñ¥Ù”œ±¹½Ü ¤°(€€€ì‰µ¥¹¥µÕµ}…”ˆèÄà°‰…¥}‘¥Í±½ÍÕÉ•}É•ÅÕ¥É•ˆéÑÉÕ”°‰Í•±™}…ÑÑ•ÍÑ…Ñ¥½¹}…±±½Ý•ˆéÑÉÕ”°‰…‘Õ±Ñ}½¹±å}™•…ÑÕÉ•Ìˆémt°‰‘¥Í±½ÍÕÉ•}É•Á•…Ñ}¡½ÕÉÌˆèÈÑôœèé©Í½¹ˆ¤ì)ME0)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€µŒp(€€‰%9MIP%9Q<p‰É•…Ñ½ÉAÉ½™¥±•pˆ€¡¥±p‰É•…Ñ½É%‘pˆ±ÍÑ…ÑÕÌ±‰¥¼±p‰Ñ…Í)Í½¹pˆ±p‰•É½Í•¹½µ•%‘pˆ¤Y1UL€ •É½Í}Ñ•ÍÑ}ÁÉ½™¥±”œ°œÈÀÀÀÀÀÀÀ´ÀÀÀÀ´ÐÀÀÀ´àÀÀÀ´ÀÀÀÀÀÀÀÀÀÀÀÈœ°AAI=Yœ°1½…°Íå¹Ñ¡•Ñ¥ŒÑ•ÍÐÁ•ÉÍ½¹„œ°mtœ°œ‘•¹½µ•}¥œèéÕÕ¥¤ìˆ)Á½ÍÑÉ•Í}¥ô¡½µÁ½Í”ÁÌ€µÄÁ½ÍÑÉ•Ì¤)…Á¥}¥µ…”ô¡½µÁ½Í”¥µ…•Ì€µÄÁ½¥¹Ðµ…Á¤¤(€‘½­•É}±½…°ÉÕ¸€´µ¹…µ”€ˆ‘¥¹Ñ•É…Ñ¥½¹}½¹Ñ…¥¹•Èˆ€´µ±…‰•°€‰½´¹‘½­•È¹½µÁ½Í”¹ÁÉ½©•Ðô‘ÁÉ½©•Ðˆ€´µ¹•ÑÝ½É¬€‰½¹Ñ…¥¹•Èè‘Á½ÍÑÉ•Í}¥ˆp(€€µ”9=}9XõÑ•ÍÐp(€€µ”Q	M}UI0ôÁ½ÍÑÉ•ÍÅ°è¼½Á½¥¹Ñ}Ý½É­•É}Ñ•ÍÐé±½…°µ½¹±äµÁ½¥¹ÐµÝ½É­•ÈµÑ•ÍÑ ÄÈÜ¸À¸À¸ÄèÔÐÌÈ½Á½¥¹Ñ}Ñ•ÍÐýÍ¡•µ„õÁÕ‰±¥Œœp(€€µ”I=M}]=I-I}%MA=M	1}QMQ}ôÄp(€€µØ€ˆ‘É•Á¼½…ÁÁÌ½…Á¤½Ñ•ÍÐè½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤½Ñ•ÍÐéÉ¼ˆp(€€µØ€ˆ‘É•Á¼½Á…­…•Ì½‘ˆ½ÁÉ¥Íµ„½µ¥É…Ñ¥½¹Ìè½Ý½É­ÍÁ…”½Á…­…•Ì½‘ˆ½ÁÉ¥Íµ„½µ¥É…Ñ¥½¹ÌéÉ¼ˆp(€€´µ•¹ÑÉåÁ½¥¹ÐÁ¹Á´€ˆ‘…Á¥}¥µ…”ˆ€´µ™¥±Ñ•ÈÁ½¥¹Ð½…Á¤•á•Œ¹½‘”€´µ¥µÁ½ÉÐÑÍà€´µÑ•ÍÐp(€Ñ•ÍÐ½•É½ÌµÝ½É­•Èµ½¹ÑÉ…Ð¹Ñ•ÍÐ¹ÑÌÑ•ÍÐ½•É½ÌµÝ½É­•Èµ…¹•°¹¥¹Ñ•É…Ñ¥½¸¹Ñ•ÍÐ¹ÑÌ)•¡¼1=1}É}A=%9Q}EUU}I1M}Q99Q}%M=1Q%=8õAML™¥áÑÕÉ•}É½±”õÁ½¥¹Ñ}Ý½É­•É}Ñ•ÍÐ)½µÁ½Í”€´µÁÉ½™¥±”Ý½É­•ÈÕÀ€µ€´µ¹¼µ‘•ÁÌÁ½¥¹ÐµÝ½É­•È)¥˜€„½µÁ½Í”•á•Œ€µÜ€½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤€µPÁ½¥¹Ðµ…Á¤¹½‘”€´µ¥µÁ½ÉÐÑÍà€½Ý½É­ÍÁ…”½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµ”É”¹µ©Ì€øˆ‘ÑµÀ½”É”¹½ÕÐˆ€Èø˜ÄìÑ¡•¸Ñ…¥°€´ÐÀ€ˆ‘ÑµÀ½”É”¹½ÕÐˆ€ø˜Èì•á¥Ð€Äì™¤)…Ð€ˆ‘ÑµÀ½”É”¹½ÕÐˆ)É•±…Ñ¥½¹Í¡¥Á}µ…ÁÁ¥¹}½Õ¹Ðô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µÑŒ€M1P½Õ¹Ð ¨¤I=4€‰É½ÍI•±…Ñ¥½¹Í¡¥Á5…ÁÁ¥¹œˆìœ¤)l€ˆ‘É•±…Ñ¥½¹Í¡¥Á}µ…ÁÁ¥¹}½Õ¹Ðˆ€ô€Ätñðì•¡¼€‰1=1}É}A=%9Q}I1Q%=9M!%A}5AA%9õ%0½Õ¹Ðô‘É•±…Ñ¥½¹Í¡¥Á}µ…ÁÁ¥¹}½Õ¹Ðˆ€ø˜Èì•á¥Ð€Äìô)•¡¼1=1}É}A=%9Q}I1Q%=9M!%A}5AA%9õAML)Í•ÍÍ¥½¹}½Õ¹Ðô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒ€‰M1P½Õ¹Ð ¨¤I=4•¹¥¹”¹¡…Ñ}Í•ÍÍ¥½¹Ì]!I¡…¹¹•°€ðø€É½ÕÀœìˆ¤)l€ˆ‘Í•ÍÍ¥½¹}½Õ¹Ðˆ€ô€Ätñðì•¡¼€‰1=1}É}55=Ie}%aQUI}MMM%=9Lõ%0½Õ¹Ðô‘Í•ÍÍ¥½¹}½Õ¹Ðˆ€ø˜Èì•á¥Ð€Äìô)Í•ÍÍ¥½¹}¥ô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒ€‰M1P¥I=4•¹¥¹”¹¡…Ñ}Í•ÍÍ¥½¹Ì]!I¡…¹¹•°€ðø€É½ÕÀœ=IH	dÉ•…Ñ•‘}…ÐM1%5%P€Äìˆ¤)l€µ¸€ˆ‘Í•ÍÍ¥½¹}¥ˆtñðì•¡¼1=1}É}55=Ie}%aQUI}MMM%=8õ%0€ø˜Èì•á¥Ð€Äìô)Í•ÍÍ¥½¹}¥ô¡ÁåÑ¡½¸Ì€µŒ€¥µÁ½ÉÐÍåÌ±ÕÕ¥ìÁÉ¥¹Ð¡ÕÕ¥¹UU%¡ÍåÌ¹…ÉÙlÅt¤¤œ€ˆ‘Í•ÍÍ¥½¹}¥ˆ¤ñðì•¡¼1=1}É}55=Ie}%aQUI}MMM%=8õ%0€ø˜Èì•á¥Ð€Äìô)Í…¹}ÍÑ…Ñ•}Á•¹‘¥¹œô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€µÑŒp(€€‰]%Q …•‘}Í…¹}ÍÑ…Ñ”L€¡UAQ•¹¥¹”¹É•±…Ñ¥½¹Í¡¥Á}Í…¹}ÍÑ…Ñ”MP±…ÍÑ}ÑÕÉ¹}…Ð€ô¹½Ü ¤€´¥¹Ñ•ÉÙ…°€œÈµ¥¹ÕÑ•Ìœ]!I¥¹ÍÑ…¹•}¥€ô€¡M1P¥¹ÍÑ…¹•}¥I=4•¹¥¹”¹¡…Ñ}Í•ÍÍ¥½¹Ì]!I¥€ô€œ‘Í•ÍÍ¥½¹}¥œèéÕÕ¥¤9Á•¹‘¥¹}ÑÕÉ¹Ì€øô€ÌIQUI9%9Á•¹‘¥¹}ÑÕÉ¹Ì¤M1PÁ•¹‘¥¹}ÑÕÉ¹ÌI=4…•‘}Í…¹}ÍÑ…Ñ”ìˆ¤)ml€ˆ‘Í…¹}ÍÑ…Ñ•}Á•¹‘¥¹œˆ€õøylÀ´åt¬ut€˜˜l€ˆ‘Í…¹}ÍÑ…Ñ•}Á•¹‘¥¹œˆ€µ”€Ìtñðì(€•¡¼€‰1=1}É}55=Ie}%aQUI}M9}MQQõ%0Á•¹‘¥¹}ÑÕÉ¹Ìô‘Í…¹}ÍÑ…Ñ•}Á•¹‘¥¹œˆ€ø˜È(€•á¥Ð€Ä)ô)µ•µ½Éå}ÍÑ…Ñ”ô)™½È…ÑÑ•µÁÐ¥¸€¡Í•Ä€Ä€ØÀ¤ì‘¼(€µ•µ½Éå}ÍÑ…Ñ”ô¡½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒp(€€€€‰M1P½Õ¹Ð ¨¤I=4•¹¥¹”¹½µÁ…¹¥½¹}µ•µ½É¥•Ì]!IÍ•ÍÍ¥½¹}¥€ô€œ‘Í•ÍÍ¥½¹}¥œèéÕÕ¥9…Ñ•½Éä%L9=P9U109½¹Ñ•¹Ð1%-€Me9Q!|”œìˆ¤(€l€ˆ‘µ•µ½Éå}ÍÑ…Ñ”ˆ€ô€ˆÈˆt€˜˜‰É•…¬(€Í±••À€Ä)‘½¹”)¥˜l€ˆ‘µ•µ½Éå}ÍÑ…Ñ”ˆ€„ô€ˆÈˆtìÑ¡•¸(€•¡¼€‰1=1}É}UI	1}55=Idõ%0ÍÑ…Ñ”ô‘µ•µ½Éå}ÍÑ…Ñ”ˆ€ø˜È(€½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µÑŒp(€€€€‰M1P€I=M}EUU}%9=MQ%ôœñð©½‰}ÑåÁ”ñð€œèœñðÍÑ…ÑÕÌñð€œèœñð½…±•Í”¡•ÉÉ½É}½‘”°€¹½¹”œ¤ñð€œé…ÑÑ•µÁÑÌôœñð…ÑÑ•µÁÑÌI=4•¹¥¹”¹•É½Í}¥¹™•É•¹•}©½‰Ì=IH	dÉ•…Ñ•‘}…Ðìˆ€ø˜ÈñðÑÉÕ”(€‘¥…¹½ÍÑ¥}Ý½É­•Èô¡½µÁ½Í”€´µÁÉ½™¥±”Ý½É­•ÈÁÌ€µ„€µÄÁ½¥¹ÐµÝ½É­•È¤(€¥˜l€µ¸€ˆ‘‘¥…¹½ÍÑ¥}Ý½É­•ÈˆtìÑ¡•¸(€€€•¡¼1=1}É}]=I-I}%9=MQ%}	%8€ø˜È(€€€‘½­•É}±½…°±½Ì€´µÑ…¥°€àÀ€ˆ‘‘¥…¹½ÍÑ¥}Ý½É­•Èˆ€ø˜ÈñðÑÉÕ”(€€€•¡¼1=1}É}]=I-I}%9=MQ%}9€ø˜È(€™¤(€•á¥Ð€Ä)™¤)•¡¼1=1}É}I5%9}M8õAML…¹‘¥‘…Ñ•ÌôÈ)•¡¼1=1}É}UI	1}55=IdõAML…¹‘¥‘…Ñ•ÌôÈ)¥˜€„½µÁ½Í”•á•Œ€µÜ€½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤€µPÁ½¥¹Ðµ…Á¤¹½‘”€´µ¥µÁ½ÉÐÑÍà€½Ý½É­ÍÁ…”½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµ”É”¹µ©Ì€´µÉ•…±°µ½¹±ä€øˆ‘ÑµÀ½É•…±°¹½ÕÐˆ€Èø˜ÄìÑ¡•¸Ñ…¥°€´ÐÀ€ˆ‘ÑµÀ½É•…±°¹½ÕÐˆ€ø˜Èì•á¥Ð€Äì™¤)…Ð€ˆ‘ÑµÀ½É•…±°¹½ÕÐˆ)Ý½É­•É}¥ô¡½µÁ½Í”€´µÁÉ½™¥±”Ý½É­•ÈÁÌ€µ„€µÄÁ½¥¹ÐµÝ½É­•È¤)l€µ¸€ˆ‘Ý½É­•É}¥ˆtñðì•¡¼1=1}É}]=I-I}=9Q%9I}5%MM%9õ%0€ø˜Èì•á¥Ð€Äìô)‘½­•É}±½…°­¥±°€´µÍ¥¹…°QI4€ˆ‘Ý½É­•É}¥ˆ€ø½‘•Ø½¹Õ±°€Èø˜ÄñðÑÉÕ”)Ý½É­•É}ÍÑ…ÑÕÌô¡‘½­•É}±½…°Ý…¥Ð€ˆ‘Ý½É­•É}¥ˆ¤)l€ˆ‘Ý½É­•É}ÍÑ…ÑÕÌˆ€ô€Àtñðì•¡¼1=1}É}]=I-I}a%Põ%0€ø˜Èì‘½­•É}±½…°±½Ì€ˆ‘Ý½É­•É}¥ˆ€ø˜Èì•á¥Ð€Äìô)Ý½É­•É}±½œô¡‘½­•É}±½…°±½Ì€ˆ‘Ý½É­•É}¥ˆ¤(€€€½µÁ±•Ñ•‘}©½‰Ìô¡ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Ý½É­•É}±½œˆðÉ•À€µŒ€1=1}QMQ}]=I-I}=5A1QôÄœñðÑÉÕ”¤)l€ˆ‘½µÁ±•Ñ•‘}©½‰Ìˆ€µ”€ÄØtñðì•¡¼€‰1=1}É}]=I-I})=	Lõ%0½Õ¹Ðô‘½µÁ±•Ñ•‘}©½‰Ìˆ€ø˜ÈìÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Ý½É­•É}±½œˆ€ø˜Èì•á¥Ð€Äìô)½¹Ñ•áÑ}É•ÍÕ±Ðô¡ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Ý½É­•É}±½œˆð¹½‘”€ˆ‘É•Á¼½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½ÁÉ½Ù¥‘•Èµ½¹Ñ•áÐµ¡•¬¹µ©Ìˆ¤)ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘½¹Ñ•áÑ}É•ÍÕ±Ðˆ(€€€¥˜ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘½¹Ñ•áÑ}É•ÍÕ±ÐˆðÉ•À€µÄ€y1=1}É}AI=Y%I}=9QaPõAML€œìÑ¡•¸(€µ•µ½Éå}É•…±±}ÍÑ…ÑÕÌõAML)•±Í”(€µ•µ½Éå}É•…±±}ÍÑ…ÑÕÌõ	1=-)™¤(€€€ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Ý½É­•É}±½œˆðÉ•À€µ€1=1}QMQ}AI=Y%I}=9QaQñ1=1}QMQ}]=I-I}=5A1Qôœ)•¡¼1=1}É}a%MQ%9}A=%9Q}]=I-I}EUUõAML)‘½­•É}±½…°É´€ˆ‘Ý½É­•É}¥ˆ€ø½‘•Ø½¹Õ±°ìÝ½É­•É}¥ô)½µÁ½Í”É•ÍÑ…ÉÐ•É½Ì)½µÁ½Í”ÕÀ€µ€´µÝ…¥Ð€´µÝ…¥ÐµÑ¥µ•½ÕÐ€äÀ•É½Ì)½µÁ½Í”•á•Œ€µÜ€½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤€µPÁ½¥¹Ðµ…Á¤¹½‘”€´µ¥µÁ½ÉÐÑÍà€½Ý½É­ÍÁ…”½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµ”É”¹µ©Ì€´µ¡¥ÍÑ½Éäµ½¹±ä)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µÑŒ€‰M1P€I=M}!Q}5MMLôœñð½Õ¹Ð ¨¤I=4•¹¥¹”¹¡…Ñ}µ•ÍÍ…•ÌìM1P€I=M}UI	1}55=Ie}I=]Lôœñð½Õ¹Ð ¨¤I=4•¹¥¹”¹½µÁ…¹¥½¹}µ•µ½É¥•Ì]!I…Ñ•½Éä%L9=P9U109½¹Ñ•¹Ð1%-€Me9Q!|”œìˆ)¥˜½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€µŒ€1QI=4€‰UÍ•Èˆ]!I¥€ô€pœœÄÀÀÀÀÀÀÀ´ÀÀÀÀ´ÐÀÀÀ´àÀÀÀ´ÀÀÀÀÀÀÀÀÀÀÀÄpœœœ€øˆ‘ÑµÀ½ÕÍ•Èµ‘•±•Ñ”¹½ÕÐˆ€Èø˜ÄìÑ¡•¸(€•¡¼1=1}É}A=%9Q}5AA%9}=]9IM!%@õ%0ÕÍ•É}É•™•É•¹•}Ý…Í}¹½Ñ}É•ÍÑÉ¥Ñ•€ø˜È(€•á¥Ð€Ä)™¤)É•À€µÄ€É½ÍI•±…Ñ¥½¹Í¡¥Á5…ÁÁ¥¹}ÕÍ•É%‘}™­•äœ€ˆ‘ÑµÀ½ÕÍ•Èµ‘•±•Ñ”¹½ÕÐˆñðì•¡¼1=1}É}A=%9Q}5AA%9}=]9IM!%@õ%0ÕÍ•É}É•™•É•¹•}½¹ÍÑÉ…¥¹Ñ}µ¥ÍÍ¥¹œ€ø˜Èì•á¥Ð€Äìô)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€µŒ€1QI=4€‰É•…Ñ½ÉAÉ½™¥±”ˆ]!I¥€ô€pœ•É½Í}Ñ•ÍÑ}ÁÉ½™¥±”pœœœ€ø½‘•Ø½¹Õ±°)¥˜½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€µŒ€1QI=4€‰É•…Ñ½Èˆ]!I¥€ô€pœœÈÀÀÀÀÀÀÀ´ÀÀÀÀ´ÐÀÀÀ´àÀÀÀ´ÀÀÀÀÀÀÀÀÀÀÀÈpœœœ€øˆ‘ÑµÀ½É•…Ñ½Èµ‘•±•Ñ”¹½ÕÐˆ€Èø˜ÄìÑ¡•¸(€•¡¼1=1}É}A=%9Q}5AA%9}=]9IM!%@õ%0É•…Ñ½É}É•™•É•¹•}Ý…Í}¹½Ñ}É•ÍÑÉ¥Ñ•€ø˜È(€•á¥Ð€Ä)™¤)É•À€µÄ€É½ÍI•±…Ñ¥½¹Í¡¥Á5…ÁÁ¥¹}É•…Ñ½É%‘}™­•äœ€ˆ‘ÑµÀ½É•…Ñ½Èµ‘•±•Ñ”¹½ÕÐˆñðì•¡¼1=1}É}A=%9Q}5AA%9}=]9IM!%@õ%0É•…Ñ½É}É•™•É•¹•}½¹ÍÑÉ…¥¹Ñ}µ¥ÍÍ¥¹œ€ø˜Èì•á¥Ð€Äìô)•¡¼1=1}É}A=%9Q}5AA%9}=]9IM!%@õAML…½Õ¹Ñ}É•…Ñ½É}É•™•É•¹•Í}ÁÉ•Í•ÉÙ•)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µÁ½¥¹Ñ}Ñ•ÍÐ€µÑŒ€‰M1P€A=%9Q}I=M}EUU})=	Lôœñð½Õ¹Ð ¨¤I=4•¹¥¹”¹•É½Í}¥¹™•É•¹•}©½‰Ììˆ)¥˜l€ˆ‘µ•µ½Éå}É•…±±}ÍÑ…ÑÕÌˆ€ôAMLtìÑ¡•¸(€•¡¼1=1}É}55=Ie}I10õAML)•±Í”(€•¡¼1=1}É}55=Ie}I10õ	1=-É•…Í½¸õÁÉ½Ù¥‘•É}½¹Ñ•áÑ}µ¥ÍÍ¥¹œ)™¤)•¡¼1=1}É}I1}%9I9õ9=Q}IU8)•¡¼1=1}É}I=M}Q	M}UMI}I1Lõ9=Q}Y%1	1ÍÑ½­}…Á¥}½Ý¹•ÉÍ¡¥Á}Ñ•ÍÐõAML)•¡¼1=1}É}%MA=M	1}MQ-}19U@õI5)Ñ¥•É}™¥áÑÕÉ•}©Í½¸ô¡½µÁ½Í”•á•Œ€µÜ€½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤€µP€µ”€‰I=M}QMQ}9=5}%ô‘•¹½µ•}¥ˆp(€Á½¥¹Ðµ…Á¤¹½‘”€½Ý½É­ÍÁ…”½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµÑ¥•Èµµ•µ½Éäµ”É”¹µ©Ì€´µÁÉ•Á…É”¤ñðì(€•¡¼1=1}É}%9%Qe}%aQUI}AIAIõ%0€ø˜È(€•á¥Ð€Ä)ô)ÁÉ¥¹Ñ˜€œ•Íq¸œ€ˆ‘Ñ¥•É}™¥áÑÕÉ•}©Í½¸ˆ€øˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹©Í½¸ˆ)ÁåÑ¡½¸Ì€µŒ€¥µÁ½ÉÐ©Í½¸±ÍåÌìÉ½ÝÌõ©Í½¸¹±½…¡½Á•¸¡ÍåÌ¹…ÉÙlÅt¤¤ì…ÍÍ•ÉÐ±•¸¡É½ÝÌ¤ôôÄÀì…ÍÍ•ÉÐ…±°¡±•¸¡É½Ýl‰ÕÍ•É}¥‰t¤ôôÌØ…¹±•¸¡É½Ýl‰Í•ÍÍ¥½¹}¥‰t¤ôôÌØ…¹±•¸¡É½Ýl‰¥¹ÍÑ…¹•}¥‰t¤ôôÌØ™½ÈÉ½Ü¥¸É½ÝÌ¤ìÁÉ¥¹Ð ‰1=1}É}%9%Qe}%aQUI}AIAIõAMLÍ•¹…É¥½ÌôÄÀˆ¤œ€ˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹©Í½¸ˆ)ÁåÑ¡½¸Ì€ˆ‘É•Á¼½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµÑ¥•Èµµ•µ½Éäµ™¥áÑÕÉ•Ì¹Áäˆ€ˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹©Í½¸ˆ€øˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹ÍÅ°ˆ)½µÁ½Í”•á•Œ€µPÁ½ÍÑÉ•ÌÁÍÅ°€µT•É½Í}Ñ•ÍÑ}…‘µ¥¸€µ•É½Í}Ñ•ÍÐ€µØ=9}II=I}MQ=@ôÄ€ðˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹ÍÅ°ˆ€øˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹½ÕÐˆ)É•À€y1=1}É}%9%Qe}%aQUIôœ€ˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹½ÕÐˆ)Ñ¥•É}™¥áÑÕÉ•}ˆØÐô¡ÁåÑ¡½¸Ì€µŒ€¥µÁ½ÉÐ‰…Í”ØÐ±ÍåÌìÁÉ¥¹Ð¡‰…Í”ØÐ¹ˆØÑ•¹½‘”¡½Á•¸¡ÍåÌ¹…ÉÙlÅt°‰Éˆˆ¤¹É•… ¤¤¹‘•½‘” ¤¤œ€ˆ‘ÑµÀ½Ñ¥•Èµ™¥áÑÕÉ•Ì¹©Í½¸ˆ¤)¥˜€„½µÁ½Í”•á•Œ€µÜ€½Ý½É­ÍÁ…”½…ÁÁÌ½…Á¤€µP€µ”€‰1=1}Q%I}%aQUIM}ØÐô‘Ñ¥•É}™¥áÑÕÉ•}ˆØÐˆÁ½¥¹Ðµ…Á¤¹½‘”€½Ý½É­ÍÁ…”½Í•ÉÙ¥•Ì½•É½Ìµ•¹¥¹”½±½…°µÑ•ÍÐµÑ¥•Èµµ•µ½Éäµ”É”¹µ©Ì€´µÙ•É¥™äp(€€øˆ‘ÑµÀ½Ñ¥•Èµ”É”¹½ÕÐˆ€Èø˜ÄìÑ¡•¸(€Ñ…¥°€´àÀ€ˆ‘ÑµÀ½Ñ¥•Èµ”É”¹½ÕÐˆ€ø˜È(€•¡¼1=1}É}%9%Qe}Q%Hõ%0€ø˜È(€•á¥Ð€Ä)™¤)…Ð€ˆ‘ÑµÀ½Ñ¥•Èµ”É”¹½ÕÐˆ(