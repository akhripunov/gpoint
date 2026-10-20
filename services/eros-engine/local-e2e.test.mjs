import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  validateDockerEndpoint,
  validateErosImageInspection,
} from './local-test-policy.mjs';

const directory = new URL('.', import.meta.url);

test('local E2E topology has API, Eros, Worker and isolated PostgreSQL', () => {
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(compose, /gpoint-api:/);
  assert.match(compose, /gpoint-worker:/);
  assert.match(compose, /profiles:\s*\[test\]/);
  assert.match(compose, /eros:/);
  assert.match(compose, /postgres:/);
  assert.match(compose, /internal:\s*true/);
  assert.match(compose, /network_mode:\s*service:gpoint-api/);
  assert.match(compose, /gpoint-worker:[\s\S]*?profiles:\s*\[worker\]/);
  assert.match(compose, /read_only:\s*true/);
  assert.doesNotMatch(compose, /ports:|host\.docker\.internal|external:\s*true/i);
});

test('model config routes to internal GPOINT bridge without paid fallback', () => {
  const config = readFileSync(new URL('test-model-config.toml', directory), 'utf8');
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(config, /fallback_temperature\s*=\s*1(?:\.0)?/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/chat\/completions/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/embeddings/);
  assert.doesNotMatch(config, /openrouter|openai\.com|anthropic\.com|gemini\.googleapis\.com/i);
  assert.doesNotMatch(config, /^\[tasks\.(memory_extraction|insight_extraction)\]/m);
  assert.match(config, /memory extraction.*intentionally disabled/i);
  assert.match(runner, /LOCAL_E2E_MEMORY_EXTRACTION=NOT_CONFIGURED/);
  assert.match(runner, /LOCAL_E2E_REAL_INFERENCE=NOT_RUN/);
  assert.match(runner, /LOCAL_E2E_RLS_TENANT_ISOLATION=NOT_TESTED/);
});

test('local Worker fixture refuses non-isolated or non-local mode', () => {
  const worker = readFileSync(new URL('local-test-worker.py', directory), 'utf8');
  assert.match(worker, /GPOINT_AGATA_WORKER_LOCAL_TEST/);
  assert.match(worker, /http:\/\/127\.0\.0\.1:3000\/agata-worker/);
  assert.match(worker, /DETERMINISTIC_LOCAL_E2E_REPLY/);
});

test('Compose exposes no test service outside the internal Docker network', (context) => {
  const environment = {
    ...process.env,
    EROS_IMAGE: 'gpoint-eros:test',
    EROS_TEST_ADMIN_PASSWORD: 'synthetic-only-test-password',
    EROS_TEST_GPOINT_API_KEY: 'synthetic-only-api-key',
    EROS_TEST_BRIDGE_SIGNING_SECRET: 'synthetic-only-bridge-signing-secret-at-least-32-bytes',
    EROS_TEST_TENANT_ID: '7508f36f-774a-48aa-8c6b-42f6f160415d',
    EROS_TEST_SUPABASE_JWT_SECRET: 'synthetic-only-jwt-secret-at-least-32-bytes',
    EROS_TEST_WORKER_TOKEN: 'synthetic-only-worker',
    EROS_TEST_AGATA_TOKEN: 'synthetic-only-agata',
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
    () => validateDockerEndpoint('unix:///Users/synthetic/.docker/run/docker.sock', {
      home: '/Users/synthetic',
      socketExists: () => false,
    }),
    /docker_socket_unavailable/,
  );
});

test('Eros image reference, immutable ID and upstream patch provenance are pinned', () => {
  const reference = 'gpoint-eros:d1a140083321687212fb7b17c09cc0c3c9639f5e-ceadc44ee07b-linux-amd64';
  const metadata = {
    id: `sha256:${'a'.repeat(64)}`,
    os: 'linux',
    architecture: 'amd64',
    source: 'https://github.com/akhripunov/gpoint',
    upstreamRevision: 'd1a140083321687212fb7b17c09cc0c3c9639f5e',
    patchSha256: 'ceadc44ee07b1cc81d22b87f84c2736b70644e51e53b142914309df7664af71c',
    buildRevision: 'e'.repeat(40),
  };
  assert.equal(validateErosImageInspection(reference, metadata), metadata.id);
  assert.throws(() => validateErosImageInspection('gpoint-eros:latest', metadata), /pinned_eros_reference_required/);
  assert.throws(() => validateErosImageInspection(reference, { ...metadata, patchSha256: '0'.repeat(64) }), /eros_patch_provenance_mismatch/);
  assert.throws(() => validateErosImageInspection(reference, { ...metadata, id: 'mutable-tag' }), /immutable_image_id_required/);
});

test('restart-history assertions cover all turns and initial temporal facts', () => {
  const e2e = readFileSync(new URL('local-test-e2e.mjs', directory), 'utf8');
  assert.match(e2e, /assertCompleteHistory/);
  assert.match(e2e, /SYNTH_CITY_ZEPHYRBAY/);
  assert.match(e2e, /SYNTH_OLD_2026-10-22_18:40/);
  assert.match(e2e, /SYNTH_CURRENT_2026-10-23_19:15/);
  assert.match(e2e, /users\.length,\s*16/);
  assert.match(e2e, /assistants\.length,\s*16/);
});

test('cleanup path verifies the disposable Compose project is gone', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(runner, /cleanup_failed/);
  assert.match(runner, /compose down --volumes --remove-orphans/);
  assert.match(runner, /docker_local ps -aq --filter/);
  assert.match(runner, /docker_local network ls -q --filter/);
});

test('standalone PostgreSQL integration container is owned by and cleaned with its project', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(runner, /integration_container="\$\{project\}-worker-integration"/);
  assert.match(runner, /--label "com\.docker\.compose\.project=\$project"/);
  assert.match(runner, /docker_local inspect "\$integration_container"/);
  assert.match(runner, /docker_local rm -f "\$integration_container"/);
});
