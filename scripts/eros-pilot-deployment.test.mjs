import test from 'node:test';
import assert from 'node:assert/strict';
import {assertPilotCapacity, MINIMUM_PILOT_AVAILABLE_MEMORY_KIB, parsePilotEnvironment, pilotEnvironmentText, pilotStartCommand,
  pilotBackupCommand, pilotStateFingerprintCommand, validatePilotServiceSelection, executePilotApply, PILOT_SERVICES} from './eros-pilot-deployment.mjs';

const sha = 'a'.repeat(40);
const env = Object.fromEntries([
  'EROS_PILOT_GPOINT_API_KEY', 'EROS_PILOT_BRIDGE_SIGNING_SECRET', 'EROS_PILOT_POSTGRES_ADMIN_PASSWORD',
  'EROS_PILOT_MIGRATOR_PASSWORD', 'EROS_PILOT_RUNTIME_PASSWORD', 'EROS_PILOT_SUPABASE_JWT_SECRET',
].map(key => [key, 'synthetic-secret-value']));
Object.assign(env, {EROS_PILOT_TENANT_ID: '12345678-1234-1234-1234-123456789012',
  EROS_PILOT_TEST_USER_ID: '22345678-1234-1234-1234-123456789012',
  EROS_PILOT_TEST_CREATOR_ID: '32345678-1234-1234-1234-123456789012'});

test('pilot env accepts only required pilot variables and redacted values are not returned in errors', () => {
  assert.deepEqual(parsePilotEnvironment(Buffer.from(Object.entries(env).map(([k,v]) => `${k}=${v}`).join('\n')).toString('base64')), env);
  assert.throws(() => parsePilotEnvironment(''), /secret is missing/);
  assert.throws(() => parsePilotEnvironment(Buffer.from(Object.entries({...env, DATABASE_URL:'secret'}).map(([k,v]) => `${k}=${v}`).join('\n')).toString('base64')), /out-of-scope/);
  assert.throws(() => parsePilotEnvironment(Buffer.from(`${Object.entries(env).map(([k,v]) => `${k}=${v}`).join('\n')}\nEROS_PILOT_GPOINT_API_KEY=y`).toString('base64')), /duplicate/);
  assert.throws(() => parsePilotEnvironment(Buffer.from(Object.entries({...env,
    EROS_PILOT_GPOINT_API_KEY:'bad$dotenv-substitution'}).map(([k,v]) => `${k}=${v}`).join('\n')).toString('base64')), /unsafe for a Compose dotenv/);
});

test('pilot environment binds exact SHA and immutable stock Eros digest', () => {
  const text = pilotEnvironmentText(env, {sourceSha: sha, runtimeImageSha: sha,
    erosImage: `ghcr.io/akhripunov/gpoint-eros-pilot@sha256:${'b'.repeat(64)}`});
  assert.match(text, new RegExp(`EROS_PILOT_SOURCE_SHA=${sha}`));
  assert.match(text, /EROS_PILOT_IMAGE=ghcr\.io\/akhripunov\/gpoint-eros-pilot@sha256:/);
  assert.throws(() => pilotEnvironmentText(env, {sourceSha: sha, runtimeImageSha: sha, erosImage: 'latest'}), /immutable/);
});

test('pilot compose has a dedicated project and can only start the pilot service allowlist', () => {
  const command = pilotStartCommand(sha);
  assert.match(command, /docker compose -p gpoint-eros-pilot/);
  assert.ok(command.indexOf('up -d postgres eros-pilot-embedding-relay eros-pilot-api') < command.indexOf('up eros-migrate'));
  assert.ok(command.indexOf('up eros-migrate') < command.indexOf('up ollama-model-verify'));
  assert.ok(command.indexOf('up ollama-model-verify') < command.indexOf('up -d eros'));
  assert.doesNotMatch(command, /up -d[^;]*eros-migrate[^;]*ollama-model-verify/);
  assert.doesNotMatch(command, /--force-recreate|prisma migrate deploy|compose-api-1|worker bot/);
  assert.throws(() => pilotStartCommand('latest'), /exact source SHA/);
  assert.equal(validatePilotServiceSelection(PILOT_SERVICES), true);
  assert.throws(() => validatePilotServiceSelection([...PILOT_SERVICES, 'api']), /allowlist/);
});

test('pilot backup is confined to its own named project, database and protected backup directory', () => {
  const command = pilotBackupCommand(sha);
  assert.match(command, /-p gpoint-eros-pilot/);
  assert.match(command, /pg_dump -U eros_pilot_admin -d eros_pilot -Fc/);
  assert.match(command, /pg_restore --list/);
  assert.match(command, /\/opt\/gpoint\/backups\/eros-pilot/);
  assert.doesNotMatch(command, /compose -p compose|gpoint-production|--force-recreate/);
});

test('fresh RAM and disk are mandatory and production fingerprint covers the existing Compose project', () => {
  assert.equal(assertPilotCapacity({availableMemoryKiB: 1_500_000, freeDiskKiB: 7_000_000}).status, 'PASS');
  assert.equal(MINIMUM_PILOT_AVAILABLE_MEMORY_KIB, 1024 * 1024);
  assert.equal(assertPilotCapacity({availableMemoryKiB: 1024 * 1024, freeDiskKiB: 7_000_000}).status, 'PASS');
  assert.throws(() => assertPilotCapacity({availableMemoryKiB: 1024 * 1024 - 1, freeDiskKiB: 7_000_000}), /memory headroom/);
  assert.throws(() => assertPilotCapacity({availableMemoryKiB: 300_000, freeDiskKiB: 7_000_000}), /memory headroom/);
  assert.throws(() => assertPilotCapacity({availableMemoryKiB: 1_500_000, freeDiskKiB: 4_000_000}), /free disk/);
  const fingerprint = pilotStateFingerprintCommand();
  assert.match(fingerprint, /label=com\.docker\.compose\.project=compose/);
  assert.match(fingerprint, /compose-gsales-api-1 compose-gsales-agata-api-1 compose-gsales-agata-memory-worker-1/);
  assert.match(fingerprint, /GPOINT_PRODUCTION_SERVICE_NOT_INTENTIONALLY_STOPPED/);
  assert.match(fingerprint, /compose-api-1 compose-gpoint-sensitive-detector-1 compose-agata-api-1 compose-agata-memory-worker-1/);
  assert.match(fingerprint, /shared_postgres_1 shared_redis_1/);
  assert.match(fingerprint, /GPOINT_PRODUCTION_SERVICE_UNHEALTHY/);
  assert.match(fingerprint, /compose-worker-1 compose-bot-1/);
});

test('isolated Apply stops only pilot services after a health gate fails and retains the data volume', () => {
  const commands = [];
  assert.throws(() => executePilotApply({sourceSha: sha, beforeSnapshot: 'a'.repeat(64), run: command => {
    commands.push(command);
    if (command === 'snapshot') return `EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=${'a'.repeat(64)}\nEROS_PILOT_AVAILABLE_MEMORY_KIB=1500000\nEROS_PILOT_FREE_DISK_KIB=7000000`;
    if (command.includes('EROS_PILOT_HEALTH')) throw new Error('synthetic health failure');
    return 'ok';
  }, healthCommand: 'echo EROS_PILOT_HEALTH=PASS', preApplySnapshotCommand: 'snapshot', postSnapshotCommand: 'snapshot'}), error => {
    assert.equal(error.rollbackStatus, 'PASS_STOPPED_ONLY_PILOT_SERVICES');
    assert.equal(error.pilotApplyStarted, true);
    return /synthetic health failure/.test(error.message);
  });
  assert.equal(commands.some(command => command.includes('up -d --wait postgres eros-pilot-embedding-relay eros-pilot-api')),
    true);
  const rollback = commands.at(-1);
  assert.match(rollback, /stop eros-pilot-api postgres eros-migrate eros-pilot-embedding-relay ollama-model-verify eros$/);
  assert.doesNotMatch(rollback, /down -v|--force-recreate|compose-api-1|worker bot/);
});

test('isolated Apply accepts success only with complete memory readback and unchanged production fingerprint', () => {
  const commands = [];
  const output = executePilotApply({sourceSha: sha, beforeSnapshot: 'a'.repeat(64), run: command => {
    commands.push(command);
    if (command === 'snapshot') return `EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=${'a'.repeat(64)}\nEROS_PILOT_AVAILABLE_MEMORY_KIB=1500000\nEROS_PILOT_FREE_DISK_KIB=7000000`;
    if (command === 'health') return 'EROS_PILOT_HEALTH=PASS\n' + ['eros', 'postgres', 'api', 'relay'].map(x => `gpoint-eros-pilot-${x} 10MiB / 100MiB`).join('\n');
    return 'ok';
  }, healthCommand: 'health', preApplySnapshotCommand: 'snapshot', postSnapshotCommand: 'snapshot'});
  assert.equal(output.memoryUsage.length, 4);
  assert.equal(output.productionSnapshotSha256, 'a'.repeat(64));
  assert.equal(commands.some(command => /\bstop\b/.test(command)), false);
});
