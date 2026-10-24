import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {validateErosProductionImage, erosProvisionCommand, erosBackupCommand, erosStartCommand,
  erosRollbackCommand, erosComposeLayerCommand, erosSyntheticRouteReadbackCommand,
  erosSyntheticComposeConfigCheckCommand} from './eros-production-deployment.mjs';

const sha='a'.repeat(40), digest='sha256:'+'b'.repeat(64), config='sha256:'+'c'.repeat(64);
const bundle={candidateSha:sha,gates:{erosPilotImage:'PASS',erosPilotGhcrReadback:'PASS',erosStockMigrationApiE2E:'PASS'},
  pilotImages:{eros:{repository:'ghcr.io/akhripunov/gpoint-eros-pilot',digest,configDigest:config,sourceSha:sha,platform:'linux/amd64'}}};

test('permanent Eros accepts only the exact stock receipt with all Prepare gates',()=>{
  assert.equal(validateErosProductionImage(bundle,sha),`${bundle.pilotImages.eros.repository}@${digest}`);
  for (const invalid of [{...bundle,candidateSha:'d'.repeat(40)}, {...bundle,gates:{...bundle.gates,erosStockMigrationApiE2E:'FAIL'}},
    {...bundle,pilotImages:{eros:{...bundle.pilotImages.eros,repository:'other/image'}}}])
    assert.throws(()=>validateErosProductionImage(invalid,sha));
});

test('bootstrap and compose layer use the existing PostgreSQL and separate protected Eros env',()=>{
  const provision=erosProvisionCommand(sha,`${bundle.pilotImages.eros.repository}@${digest}`);
  assert.match(provision,/--ensure-postgres-network --provision-database/);
  assert.match(provision,/stat -c '%a:%u' \/opt\/gpoint\/env\/eros\.env/);
  const layer=erosComposeLayerCommand(sha);
  assert.match(layer,/docker-compose\.eros\.yml/);
  assert.match(layer,/--env-file \/opt\/gpoint\/env\/eros\.env/);
  assert.doesNotMatch(layer,/docker-compose\.eros-pilot\.yml|eros-pilot-api/);
});

test('permanent lifecycle backs up the existing eros database and gates stock migration and health',()=>{
  const backup=erosBackupCommand(sha), start=erosStartCommand(sha), rollback=erosRollbackCommand(sha);
  assert.match(backup,/shared_postgres_1/);
  assert.match(backup,/pg_dump.*-d eros -Fc/);
  assert.ok(start.indexOf('eros-model-verify') < start.indexOf('eros-migrate'));
  assert.ok(start.indexOf('eros-before-') < start.indexOf('eros-migrate'));
  assert.match(start,/compose-eros-1/);
  assert.match(start,/EROS_PERMANENT_HEALTH=PASS/);
  assert.match(start,/EROS_IMAGE_IDENTITY=PASS/);
  assert.match(start,/EROS_PRE_APPLY_MEMAVAILABLE_KIB/);
  assert.match(start,/EROS_POST_APPLY_MEMAVAILABLE_KIB/);
  assert.match(rollback,/pg_restore --exit-on-error/);
  assert.throws(()=>erosStartCommand('main'));
  assert.match(erosSyntheticRouteReadbackCommand(),/EROS_PRODUCTION_TEST_USER_IDS=/);
  assert.match(erosSyntheticRouteReadbackCommand(),/EROS_PRODUCTION_TEST_CREATOR_IDS=/);
  const composeGate=erosSyntheticComposeConfigCheckCommand(sha);
  const shell=spawnSync('bash',['-n'],{input:composeGate,encoding:'utf8'});
  assert.equal(shell.status,0,shell.stderr);
  const encoded=composeGate.match(/printf '%s' '([A-Za-z0-9+/=]+)'/)[1];
  const code=Buffer.from(encoded,'base64').toString();
  const compile=spawnSync('python3',['-c','import sys; compile(sys.stdin.read(), "<eros-compose-gate>", "exec")'],{input:code,encoding:'utf8'});
  assert.equal(compile.status,0,compile.stderr);
  assert.match(code,/EROS_PRODUCTION_TEST_USER_IDS/);
});
