import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
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

test('local E2E topology has API, Eros, Worker and isolated PostgreSQL', () => {
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(compose, /gpoint-api:/);
  assert.match(compose, /gpoint-worker:/);
  assert.match(compose, /profiles:\s*\[test\]/);
  assert.match(compose, /eros:/);
  assert.match(compose, /postgres:/);
  assert.match(compose, /DATABASE_URL:[^\n]*@postgres:5432\/gpoint_test\?schema=public/);
  assert.match(compose, /EROS_ADAPTER_DATABASE_URL:[^\n]*@postgres:5432\/eros_test\?schema=engine/);
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
  assert.match(init, /GRANT CONNECT, CREATE ON DATABASE eros_test TO eros_migrator;/);
  assert.match(init, /GRANT USAGE, CREATE ON SCHEMA public TO eros_migrator;/);
  assert.match(init, /CREATE DATABASE gpoint_test OWNER eros_test_admin;/);
  assert.match(init, /CREATE ROLE eros_migrator LOGIN[\s\S]*?NOSUPERUSER NOCREATEDB NOBYPASSRLS;/);
  assert.doesNotMatch(init, /GRANT [^;]* ON DATABASE eros_test TO (?!eros_migrator\b)[a-z_]+;/);
});

test('creator erasure fixture removes dependent profile before asserting relationship mapping guard', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const profileDelete = runner.indexOf('DELETE FROM \"CreatorProfile\"');
  const creatorDelete = runner.indexOf('DELETE FROM \"Creator\"');
  assert.match(runner, /DELETE FROM \"CreatorProfile\"[^\n]*eros_test_profile/);
  const historyCheck = runner.indexOf('--history-only');
  assert.ok(historyCheck >= 0 && profileDelete > historyCheck && creatorDelete > profileDelete);
  assert.match(runner, /ErosRelationshipMapping_creatorId_fkey/);
});

test('GPoint mapping deletion is blocked until Eros has a supported data-erasure lifecycle', () => {
  const schema = readFileSync(new URL('../../packages/db/prisma/schema.prisma', directory), 'utf8');
  const migration = readFileSync(new URL('../../packages/db/prisma/migrations/20261020153000_eros_external_relationship_mapping/migration.sql', directory), 'utf8');
  const model = schema.match(/model ErosRelationshipMapping \{[\s\S]*?\n\}/)?.[0] ?? '';
  assert.match(model, /user User @relation\(fields: \[userId\], references: \[id\], onDelete: Restrict\)/);
  assert.match(model, /creator Creator @relation\(fields: \[creatorId\], references: \[id\], onDelete: Restrict\)/);
  assert.match(migration, /ErosRelationshipMapping_userId_fkey[\s\S]*?ON DELETE RESTRICT/);
  assert.match(migration, /ErosRelationshipMapping_creatorId_fkey[\s\S]*?ON DELETE RESTRICT/);
  assert.doesNotMatch(migration, /ErosRelationshipMapping_(?:userId|creatorId)_fkey[^;]*ON DELETE CASCADE/);
});

test('model config routes chat, embeddings and synthetic memory extraction to the internal GPOINT bridge', () => {
  const config = readFileSync(new URL('test-model-config.toml', directory), 'utf8');
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const compose = readFileSync(new URL('compose.local-test.yml', directory), 'utf8');
  assert.match(config, /fallback_temperature\s*=\s*1(?:\.0)?/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/chat\/completions/);
  assert.match(config, /http:\/\/gpoint-api:3000\/eros-worker\/v1\/embeddings/);
  assert.match(compose, /GPOINT_API_KEY: \$\{EROS_TEST_GPOINT_API_KEY/);
  assert.doesNotMatch(config, /openrouter|openai\.com|anthropic\.com|gemini\.googleapis\.com/i);
  assert.match(config, /^\[tasks\.memory_extraction\]/m);
  assert.match(config, /filter_prompt\s*=\s*".*synthetic/i);
  assert.doesNotMatch(config, /temperature\s*=\s*0(?:\.0)?/);
  assert.match(compose, /DREAMING_TICK_SECS/);
  assert.match(compose, /DREAMING_IDLE_SECS/);
  assert.match(runner, /LOCAL_E2E_MEMORY_EXTRACTION=PASS/);
  assert.match(runner, /LOCAL_E2E_REAL_INFERENCE=NOT_RUN/);
  assert.match(runner, /LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS fixture_role=gpoint_worker_test/);
  assert.match(runner, /LOCAL_E2E_EROS_DATABASE_USER_RLS=NOT_AVAILABLE stock_api_ownership_test=PASS/);
});

test('local Worker fixture refuses non-isolated or non-local mode', () => {
  const worker = readFileSync(new URL('local-test-worker.py', directory), 'utf8');
  assert.match(worker, /GPOINT_AGATA_WORKER_LOCAL_TEST/);
  assert.match(worker, /http:\/\/127\.0\.0\.1:3000\/agata-worker/);
  assert.match(worker, /DETERMINISTIC_LOCAL_E2E_REPLY/);
  assert.match(worker, /SYNTHETIC_MEMORY_EXTRACTION_RESULT/);
  assert.match(worker, /purpose == 'eros_chat_completion'/);
  assert.match(worker, /SYNTH_CITY_ZEPHYRBAY/);
});

test('local Worker fixture treats SIGTERM as a clean stop, not a job timeout', () => {
  const worker = readFileSync(new URL('./local-test-worker.py', import.meta.url), 'utf8');
  assert.match(worker, /if not stopped and time\.monotonic\(\) >= deadline:/);
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
    () => validateDockerEndpoint('unix:///Users/synthetic/.docker/run/docker.sock', {
      home: '/Users/synthetic',
      socketExists: () => false,
    }),
    /docker_socket_unavailable/,
  );
});

test('Eros image requires stock upstream provenance and rejects downstream patches', () => {
  const reference = 'gpoint-eros:stock-d1a140083321687212fb7b17c09cc0c3c9639f5e';
  const metadata = {
    id: `sha256:${'a'.repeat(64)}`,
    os: 'linux',
    architecture: 'amd64',
    source: 'https://github.com/etherfunlab/eros-engine',
    upstreamRevision: 'd1a140083321687212fb7b17c09cc0c3c9639f5e',
    patchSha256: 'none',
    buildRevision: 'd1a140083321687212fb7b17c09cc0c3c9639f5e',
  };
  assert.equal(validateErosImageInspection(reference, metadata), metadata.id);
  assert.throws(() => validateErosImageInspection('gpoint-eros:latest', metadata), /stock_eros_image_tag_required/);
  assert.throws(() => validateErosImageInspection(reference, { ...metadata, patchSha256: 'sha256-of-a-patch' }), /eros_downstream_patch_forbidden/);
  assert.throws(() => validateErosImageInspection(reference, { ...metadata, id: 'mutable-tag' }), /immutable_image_id_required/);
});

test('the Eros patch applier is disabled and the stock builder pins a clean source', () => {
  const applier = readFileSync(new URL('apply-patches.sh', directory), 'utf8');
  const builder = readFileSync(new URL('build-stock-image.sh', directory), 'utf8');
  assert.match(applier, /fails closed|patches_disabled/i);
  assert.doesNotMatch(applier, /git .* apply/);
  assert.match(builder, /eros_source_not_clean/);
  assert.match(builder, /d1a140083321687212fb7b17c09cc0c3c9639f5e/);
  assert.match(builder, /patch-set\.sha256=none/);
});

test('the local fixture provisions stock persona data with the official seed CLI and maps it in GPoint', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const persona = readFileSync(new URL('test-personas/synthetic-eros-persona.toml', directory), 'utf8');
  const schema = readFileSync(new URL('../../packages/db/prisma/schema.prisma', directory), 'utf8');
  assert.match(runner, /eros-engine seed-personas \/etc\/eros-engine\/test-personas/);
  assert.match(runner, /LOCAL_E2E_STOCK_PERSONA_SEED=PASS/);
  assert.match(runner, /grep 'Synthetic Eros Persona'/);
  assert.match(runner, /CreatorProfile.*erosGenomeId/s);
  assert.match(persona, /name = "Synthetic Eros Persona"/);
  assert.match(schema, /model ErosRelationshipMapping/);
  assert.match(schema, /erosGenomeId String\? @unique @db\.Uuid/);
});

test('restart-history assertions cover all turns and initial temporal facts', () => {
  const e2e = readFileSync(new URL('local-test-e2e.mjs', directory), 'utf8');
  assert.match(e2e, /assertCompleteHistory/);
  assert.match(e2e, /SYNTH_CITY_ZEPHYRBAY/);
  assert.match(e2e, /SYNTH_OLD_2026-10-22_18:40/);
  assert.match(e2e, /SYNTH_CURRENT_2026-10-23_19:15/);
  assert.match(e2e, /users\.length,\s*expectedTurns/);
  assert.match(e2e, /assistants\.length,\s*expectedTurns/);
  assert.match(e2e, /--recall-only/);
  assert.match(e2e, /SYNTH_RECALL_CONFIRMED/);
  assert.equal((e2e.match(/LOCAL_E2E_RESTART_HISTORY=PASS/g) ?? []).length, 1);
});

test('stock Eros API E2E exercises owner-bound session listing and foreign-session denial', () => {
  const e2e = readFileSync(new URL('local-test-e2e.mjs', directory), 'utf8');
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(e2e, /LOCAL_E2E_STOCK_API_USER_ISOLATION=PASS/);
  assert.match(e2e, /foreignUserId/);
  assert.match(e2e, /client\.sessions\(foreignUserId\)/);
  assert.match(e2e, /client\.history\(foreignUserId, ownSession\.sessionId/);
  assert.match(e2e, /error\.status === 403/);
  assert.match(runner, /exec -w \/workspace\/apps\/api -T gpoint-api node --import tsx \/workspace\/services\/eros-engine\/local-test-e2e\.mjs/);
  assert.match(runner, /LOCAL_E2E_GPOINT_QUEUE_RLS_TENANT_ISOLATION=PASS fixture_role=gpoint_worker_test/);
  assert.match(runner, /LOCAL_E2E_EROS_DATABASE_USER_RLS=NOT_AVAILABLE stock_api_ownership_test=PASS/);
});

test('provider context evidence reports PASS when one context contains every recall fact', () => {
  const log = JSON.stringify({
    event: 'LOCAL_TEST_PROVIDER_CONTEXT',
    message_count: 14,
    recent_history_count: 12,
    user_message_count: 7,
    last_user_is_recall_probe: true,
    city_durable_memory_section_present: true,
    old_meeting_durable_memory_section_present: true,
    current_meeting_durable_memory_section_present: true,
    city_absent_from_recent_history: true,
    old_meeting_absent_from_recent_history: true,
    current_meeting_absent_from_recent_history: true,
  });
  assert.deepEqual(readProviderContextEvidence(log), {
    status: 'PASS',
    record_count: 1,
    ignored_record_count: 0,
    message_count: 14,
    recent_history_count: 12,
    missing_facts: [],
  });
});

test('provider context evidence rejects absent or malformed recall records', () => {
  const complete = {
    event: 'LOCAL_TEST_PROVIDER_CONTEXT',
    message_count: 14,
    recent_history_count: 12,
    user_message_count: 7,
    last_user_is_recall_probe: true,
    city_durable_memory_section_present: true,
    old_meeting_durable_memory_section_present: true,
    current_meeting_durable_memory_section_present: true,
    city_absent_from_recent_history: true,
    old_meeting_absent_from_recent_history: true,
    current_meeting_absent_from_recent_history: true,
  };
  assert.throws(() => readProviderContextEvidence(''), /provider_context_record_missing/);
  assert.throws(() => readProviderContextEvidence('{"event":"LOCAL_TEST_PROVIDER_CONTEXT"'), /provider_context_record_malformed/);
  assert.throws(() => readProviderContextEvidence(JSON.stringify({ ...complete, city_durable_memory_section_present: 'yes' })), /provider_context_fact_invalid:city_durable_memory_section_present/);
  assert.throws(() => readProviderContextEvidence(JSON.stringify({ ...complete, message_count: 0 })), /provider_context_message_count_invalid/);
  assert.throws(() => readProviderContextEvidence(JSON.stringify({ ...complete, recent_history_count: -1 })), /provider_context_recent_history_count_invalid/);
  assert.throws(() => readProviderContextEvidence(JSON.stringify({ ...complete, last_user_is_recall_probe: 'yes' })), /provider_context_probe_flag_invalid/);
});

test('provider context fixture attributes old facts to durable memory sections outside recent history', () => {
  const worker = readFileSync(new URL('./local-test-worker.py', import.meta.url), 'utf8');
  assert.match(worker, /durable_memory_section_present/);
  assert.match(worker, /get\('role'\) == 'system'/);
  assert.match(worker, /recent_history/);
  assert.match(worker, /absent_from_recent_history/);
  assert.match(worker, /\[user_profile\]/);
});

test('multiple provider contexts report BLOCKED when any lacks a recall fact', () => {
  const complete = {
    event: 'LOCAL_TEST_PROVIDER_CONTEXT',
    message_count: 14,
    recent_history_count: 12,
    user_message_count: 7,
    last_user_is_recall_probe: true,
    city_durable_memory_section_present: true,
    old_meeting_durable_memory_section_present: true,
    current_meeting_durable_memory_section_present: true,
    city_absent_from_recent_history: true,
    old_meeting_absent_from_recent_history: true,
    current_meeting_absent_from_recent_history: true,
  };
  const repeated = `${JSON.stringify(complete)}\n${JSON.stringify({ ...complete, message_count: 15, current_meeting_durable_memory_section_present: false })}`;
  assert.deepEqual(readProviderContextEvidence(repeated), {
    status: 'BLOCKED',
    record_count: 2,
    ignored_record_count: 0,
    message_count: 15,
    recent_history_count: 12,
    missing_facts: ['current_meeting_durable_memory_section_present'],
  });
  assert.deepEqual(readProviderContextEvidence(JSON.stringify({ ...complete, city_absent_from_recent_history: false })), {
    status: 'BLOCKED', record_count: 1, ignored_record_count: 0, message_count: 14, recent_history_count: 12, missing_facts: ['city_absent_from_recent_history'],
  });
});

test('provider context ignores Eros system tasks whose latest user message is not the recall probe', () => {
  const relevant = {
    event: 'LOCAL_TEST_PROVIDER_CONTEXT', message_count: 14, recent_history_count: 12,
    user_message_count: 7, last_user_is_recall_probe: true,
    city_durable_memory_section_present: true, old_meeting_durable_memory_section_present: true,
    current_meeting_durable_memory_section_present: true, city_absent_from_recent_history: true,
    old_meeting_absent_from_recent_history: true, current_meeting_absent_from_recent_history: true,
  };
  const irrelevant = { ...relevant, message_count: 2, user_message_count: 1, last_user_is_recall_probe: false,
    recent_history_count: 1,
    city_durable_memory_section_present: false, old_meeting_durable_memory_section_present: false,
    current_meeting_durable_memory_section_present: false };
  assert.deepEqual(readProviderContextEvidence(`${JSON.stringify(irrelevant)}\n${JSON.stringify(relevant)}`), {
    status: 'PASS', record_count: 1, ignored_record_count: 1, message_count: 14,
    recent_history_count: 12, missing_facts: [],
  });
  assert.throws(() => readProviderContextEvidence(JSON.stringify({ ...irrelevant, last_user_is_recall_probe: true })), /provider_context_probe_record_missing/);
});

test('cleanup path verifies the disposable Compose project is gone', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.ok(runner.indexOf('compose restart eros') < runner.indexOf('--history-only'), 'history pass must follow an Eros restart');
  assert.match(runner, /cleanup_failed/);
  assert.match(runner, /compose down --volumes --remove-orphans/);
  assert.match(runner, /for container in \$\{containers:-\}/);
  assert.match(runner, /docker_local inspect "\$container" --format/);
  assert.match(runner, /if \[ "\$container_owner" = "\$project" \]/);
  assert.match(runner, /docker_local rm -f "\$container"/);
  assert.match(runner, /docker_local ps -aq --filter/);
  assert.match(runner, /docker_local network ls -q --filter/);
  assert.match(runner, /network_name.*\$\{project\}_eros_test/);
  assert.match(runner, /network_containers" = 0/);
  assert.match(runner, /docker_local network rm "\$network"/);
  assert.match(runner, /LOCAL_E2E_CLEANUP=PASS/);
});

test('memory extraction, durable retrieval and provider context are checked by the runner', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(runner, /context_result=\$\(printf/);
  assert.match(runner, /LOCAL_E2E_PROVIDER_CONTEXT=PASS/);
  assert.match(runner, /LOCAL_E2E_MEMORY_EXTRACTION=PASS/);
  assert.match(runner, /LOCAL_E2E_DURABLE_MEMORY=PASS/);
  assert.match(runner, /--recall-only/);
  assert.match(runner, /classified_at IS NOT NULL/);
});

test('standalone PostgreSQL integration container is owned by and cleaned with its project', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  assert.match(runner, /integration_container="\$\{project\}-worker-integration"/);
  assert.match(runner, /--label "com\.docker\.compose\.project=\$project"/);
  assert.match(runner, /inspect_run_container "\$integration_container"/);
  assert.match(runner, /docker_local rm -f "\$integration_container"/);
});

test('runner records each verified image immediately and fails closed on unmanifested partial builds', () => {
  const runner = readFileSync(new URL('run-local-e2e.sh', directory), 'utf8');
  const imageTools = readFileSync(new URL('local-e2e-image-tools.sh', directory), 'utf8');
  assert.match(runner, /run_manifest=/);
  assert.match(runner, /\$\{project\}-gpoint-api:latest/);
  assert.match(runner, /\$\{project\}-gpoint-api-migrate:latest/);
  assert.match(runner, /\$\{project\}-gpoint-worker:latest/);
  assert.match(runner, /case "\$image_ref" in/);
  assert.match(runner, /compose --profile worker build "\$service"/);
  assert.match(runner, /manifest_entry=\$\(awk/);
  assert.match(runner, /LOCAL_E2E_IMAGE_OWNERSHIP=UNVERIFIED/);
  assert.match(runner, /not_in_manifest/);
  assert.match(runner, /local-e2e-image-tools\.sh/);
  assert.match(imageTools, /No such image: \$1/);
  assert.match(imageTools, /reason=daemon_or_cli_error/);
  assert.match(imageTools, /No such object: \$1/);
  assert.match(imageTools, /LOCAL_E2E_CONTAINER_INSPECT=FAIL/);
  assert.match(runner, /reason=manifest_image_missing/);
  assert.match(runner, /phase=preflight/);
  assert.match(runner, /image_identity=\$\(inspect_run_image "\$image_ref"\)/);
  assert.match(runner, /Record each image immediately/);
  assert.doesNotMatch(runner, /image_tags_eligible/);
  assert.match(runner, /image_id=/);
  assert.match(runner, /com\.gpoint\.me36\.run/);
  assert.match(runner, /expected_image_id/);
  assert.match(runner, /image_labels.*project/);
  assert.match(runner, /docker_local image rm "\$image_ref"/);
  assert.doesNotMatch(runner, /docker (?:system|builder) prune/);
  assert.doesNotMatch(runner, /docker_local image rm .*\*/);
});

test('image inspection distinguishes found, explicitly missing, empty, and daemon error results', () => {
  const helperPath = fileURLToPath(new URL('local-e2e-image-tools.sh', directory));
  const script = `
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
. "$1"
docker_local(){
  case "$MOCK_MODE" in
    found) printf 'sha256:abc|me36_run|gpoint-api' ;;
    missing) printf 'Error response from daemon: no such image: %s\\n' "$MOCK_TAG" >&2; return 1 ;;
    daemon) printf 'Cannot connect to the Docker daemon\\n' >&2; return 1 ;;
    empty) return 0 ;;
  esac
}
if output=$(inspect_run_image "$MOCK_TAG"); then status=0; else status=$?; fi
printf 'status=%s output=%s\\n' "$status" "$output"
`;
  const inspect = (mode) => spawnSync('sh', ['-c', script, 'sh', helperPath], {
    encoding: 'utf8',
    env: { ...process.env, MOCK_MODE: mode, MOCK_TAG: 'me36_test-gpoint-api:latest' },
  });
  const found = inspect('found');
  assert.equal(found.status, 0);
  assert.match(found.stdout, /status=0 output=sha256:abc\|me36_run\|gpoint-api/);
  const missing = inspect('missing');
  assert.equal(missing.status, 0);
  assert.equal(missing.stdout.trim(), 'status=2 output=');
  const daemon = inspect('daemon');
  assert.equal(daemon.status, 0);
  assert.equal(daemon.stdout.trim(), 'status=1 output=');
  assert.match(daemon.stderr, /reason=daemon_or_cli_error/);
  const empty = inspect('empty');
  assert.equal(empty.status, 0);
  assert.equal(empty.stdout.trim(), 'status=1 output=');
  assert.match(empty.stderr, /reason=empty_result/);
});

test('container inspection distinguishes found, explicitly missing, empty, and daemon error results', () => {
  const helperPath = fileURLToPath(new URL('local-e2e-image-tools.sh', directory));
  const script = `
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
. "$1"
docker_local(){
  case "$MOCK_MODE" in
    found) printf 'container-id|me36_run' ;;
    missing) printf 'error: no such object: %s\\n' "$MOCK_TAG" >&2; return 1 ;;
    daemon) printf 'Cannot connect to the Docker daemon\\n' >&2; return 1 ;;
    empty) return 0 ;;
  esac
}
if output=$(inspect_run_container "$MOCK_TAG"); then status=0; else status=$?; fi
printf 'status=%s output=%s\\n' "$status" "$output"
`;
  const inspect = (mode) => spawnSync('sh', ['-c', script, 'sh', helperPath], {
    encoding: 'utf8',
    env: { ...process.env, MOCK_MODE: mode, MOCK_TAG: 'me36_test-worker-integration' },
  });
  const found = inspect('found');
  assert.equal(found.status, 0);
  assert.equal(found.stdout.trim(), 'status=0 output=container-id|me36_run');
  const missing = inspect('missing');
  assert.equal(missing.status, 0);
  assert.equal(missing.stdout.trim(), 'status=2 output=');
  const daemon = inspect('daemon');
  assert.equal(daemon.status, 0);
  assert.equal(daemon.stdout.trim(), 'status=1 output=');
  assert.match(daemon.stderr, /reason=daemon_or_cli_error/);
  const empty = inspect('empty');
  assert.equal(empty.status, 0);
  assert.equal(empty.stdout.trim(), 'status=1 output=');
  assert.match(empty.stderr, /reason=empty_result/);
});
