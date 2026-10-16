import test from 'node:test';
import assert from 'node:assert/strict';
import { validateTaskAuthorization, AuthorizationError, checkTaskAuthorizationState,
  consumeTaskAuthorization, authorizationStateRef, assertNoTaskScopedMigrations } from './release-authorization.mjs';
import {candidateScopeDiff} from './release-authorization.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

const id = 'gpoint-task-2026-10-06-001';
const baseline = 'a'.repeat(40);
const candidate = 'b'.repeat(40);
const record = (extra = {}) => ({
  schema_version: 3,
  owner_instruction_scope: 'FULL_PRODUCTION_CYCLE',
  project: 'gpoint',
  authorization_mode: 'TASK_SCOPED_AUTO',
  authorization_id: id,
  source_reference: 'linear:gpoint-release-handoff#owner-instruction-2026-10-06',
  production_baseline_sha: baseline,
  task: 'Change one user-facing menu label',
  allowed_paths: ['apps/web/components/Menu.tsx'],
  acceptance_criteria: ['the menu label is updated'],
  exclusions: ['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic', 'release_infrastructure'],
  state: 'ACTIVE', revoked: false, consumed: false,
  ...extra,
});
const validate = (overrides = {}) => validateTaskAuthorization({
  record: record(), project: 'gpoint', authorizationId: id,
  candidateSha: candidate, currentMainSha: candidate,
  productionSha: baseline, changedPaths: ['apps/web/components/Menu.tsx'],
  ...overrides,
});

test('task authorization accepts exact candidate, baseline and allowed path', () => {
  assert.equal(validate().authorizationId, id);
});

for (const path of ['.github/workflows/gpoint-deploy.yml', 'scripts/release-gpoint.mjs',
  'packages/db/prisma/migrations/0001/migration.sql', 'services/agata/app/main.py',
  'services/core/auth.ts', 'services/gpoint-ads/direct.ts', 'payments/checkout.ts']) {
  test(`task authorization rejects owner-only path ${path}`, () => {
    assert.throws(() => validate({ record: record({ allowed_paths: [path] }), changedPaths: [path] }),
      (error) => error instanceof AuthorizationError && /owner-only/.test(error.message));
  });
}

test('a complete pre-approved migration contract admits only its exact scoped migration path', () => {
  const migration = 'packages/db/prisma/migrations/0099_add_field/migration.sql';
  const risk = {risk_description: 'Add one nullable profile field with a bounded forward migration.',
    allowed_paths: [migration], prisma_target: '0099_add_field', agata_target: 'agata-1',
    gsales_migration_sha256: 'b'.repeat(64), production_prisma_head: 'prisma-1',
    production_agata_head: 'agata-1', production_gsales_head: 'gsales-1',
    production_gsales_migration_sha256: 'a'.repeat(64),
    backup_plan: 'Create and verify the canonical GPoint database backup before migration.',
    rollback_plan: 'Restore the verified backup or use the recorded forward-repair procedure.',
    stop_conditions: ['Stop if live baseline differs.', 'Stop if backup verification fails.'],
    required_gates: ['migration_preflight', 'backup_before_migration', 'rollback_readiness', 'tenant_isolation', 'postdeploy_health']};
  const proof = validate({record: record({allowed_paths: [migration], risk_contracts: {database_migration: risk}}),
    changedPaths: [migration]});
  assert.equal(proof.changedPaths, migration);
  assert.throws(() => validate({record: record({allowed_paths: [migration]}), changedPaths: [migration]}),
    /complete pre-approved database risk contract/);
  assert.throws(() => validate({record: record({allowed_paths: [migration], risk_contracts: {database_migration: risk}}),
    changedPaths: [migration, 'packages/db/prisma/migrations/0100_other/migration.sql']}),
    /complete pre-approved database risk contract/);
});

test('task authorization rejects unrelated paths and baseline drift', () => {
  assert.throws(() => validate({ changedPaths: ['apps/web/components/Menu.tsx', 'README.md'] }), /outside authorized scope/);
  assert.throws(() => validate({ productionSha: 'c'.repeat(40) }), /production baseline/);
  assert.throws(() => validate({ currentMainSha: 'c'.repeat(40) }), /exact current main/);
});

test('task authorization rejects incomplete, revoked and consumed records', () => {
  for (const extra of [{ source_reference: '' }, { acceptance_criteria: [] }, { allowed_paths: [] },
    { exclusions: ['database'] }, { state: 'REVOKED' }, { revoked: true }, { consumed: true }]) {
    assert.throws(() => validate({ record: record(extra) }), AuthorizationError);
  }
});

test('task authorization requires explicit full-cycle owner intent in the durable record', () => {
  assert.throws(() => validate({record: record({owner_instruction_scope: 'PREPARE_ONLY'})}), /explicitly authorize the full production cycle/);
  assert.throws(() => validate({record: record({schema_version: 1})}), /Unsupported task authorization record/);
});

test('task authorization rejects wrong project and authorization id', () => {
  assert.throws(() => validate({ project: 'maxibot' }), /project/);
  assert.throws(() => validate({ authorizationId: 'another-authorization' }), /authorization ID/);
});

function makeGitRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-task-auth-'));
  const git = (...args) => execFileSync('git', args, {cwd: root, encoding: 'utf8'}).trim();
  git('init', '-q'); git('config', 'user.name', 'Release test'); git('config', 'user.email', 'release-test@example.invalid');
  fs.writeFileSync(path.join(root, 'README.md'), 'baseline\n'); git('add', 'README.md'); git('commit', '-m', 'baseline');
  return {root, git, baseline: git('rev-parse', 'HEAD')};
}

test('scope diff requires the durable authorization commit before task code', () => {
  const {root, git, baseline} = makeGitRepo();
  const auth = path.join(root, '.release-authorizations'); fs.mkdirSync(auth);
  fs.writeFileSync(path.join(auth, `${id}.json`), '{}\n'); git('add', '.release-authorizations'); git('commit', '-m', 'record task authorization');
  fs.mkdirSync(path.join(root, 'apps/web/components'), {recursive: true});
  fs.writeFileSync(path.join(root, 'apps/web/components/Menu.tsx'), 'updated\n');
  git('add', 'apps/web/components/Menu.tsx'); git('commit', '-m', 'implement authorized task');
  assert.deepEqual(candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')), ['apps/web/components/Menu.tsx']);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope diff rejects authorization record added together with task code', () => {
  const {root, git, baseline} = makeGitRepo();
  const auth = path.join(root, '.release-authorizations'); fs.mkdirSync(auth);
  fs.writeFileSync(path.join(auth, `${id}.json`), '{}\n');
  fs.mkdirSync(path.join(root, 'apps/web/components'), {recursive: true});
  fs.writeFileSync(path.join(root, 'apps/web/components/Menu.tsx'), 'updated\n');
  git('add', '.release-authorizations', 'apps/web/components/Menu.tsx'); git('commit', '-m', 'combine record and task');
  assert.throws(() => candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')), /precede every implementation change/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope diff accepts a non-squashed PR merge when the authorization commit precedes code', () => {
  const {root, git, baseline} = makeGitRepo();
  git('branch', 'base', baseline); git('switch', '-c', 'authorized-task');
  const auth = path.join(root, '.release-authorizations'); fs.mkdirSync(auth);
  fs.writeFileSync(path.join(auth, `${id}.json`), '{}\n'); git('add', '.release-authorizations'); git('commit', '-m', 'record task authorization');
  fs.mkdirSync(path.join(root, 'apps/web/components'), {recursive: true});
  fs.writeFileSync(path.join(root, 'apps/web/components/Menu.tsx'), 'updated\n');
  git('add', 'apps/web/components/Menu.tsx'); git('commit', '-m', 'implement authorized task');
  git('switch', 'base'); git('merge', '--no-ff', 'authorized-task', '-m', 'merge authorized task');
  assert.deepEqual(candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')), ['apps/web/components/Menu.tsx']);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope diff rejects a squash merge that hides authorization-before-code ordering', () => {
  const {root, git, baseline} = makeGitRepo();
  git('branch', 'base', baseline); git('switch', '-c', 'authorized-task');
  const auth = path.join(root, '.release-authorizations'); fs.mkdirSync(auth);
  fs.writeFileSync(path.join(auth, `${id}.json`), '{}\n'); git('add', '.release-authorizations'); git('commit', '-m', 'record task authorization');
  fs.mkdirSync(path.join(root, 'apps/web/components'), {recursive: true});
  fs.writeFileSync(path.join(root, 'apps/web/components/Menu.tsx'), 'updated\n');
  git('add', 'apps/web/components/Menu.tsx'); git('commit', '-m', 'implement authorized task');
  git('switch', 'base'); git('merge', '--squash', 'authorized-task'); git('commit', '-m', 'squash authorized task');
  assert.throws(() => candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')), /precede every implementation change/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('authorization state check observes revoked, consumed and claimed markers', async () => {
  const paths = [];
  const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async url => { paths.push(url); return {status: 404, ok: false}; }});
  assert.deepEqual(state, {revoked: false, consumed: false, claimed: false});
  assert.equal(paths.length, 3);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async () => ({status: 200, ok: true, json: async () => ({object: {sha: candidate}})})}), /revoked or already consumed/);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async () => ({status: 404, ok: false})}), /claim is absent or belongs to another candidate/);
  const claimed = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async url => url.includes('/claimed')
      ? ({status: 200, ok: true, json: async () => ({object: {sha: candidate}})})
      : ({status: 404, ok: false})});
  assert.equal(claimed.claimed, true);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: '',
    fetchImpl: async () => assert.fail('must not query without token')}), /token is required/);
});

test('authorization consumption creates an immutable candidate-bound Git ref', async () => {
  let request;
  await consumeTaskAuthorization({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async (url, options) => { request = {url, options}; return {status: 201, ok: true}; }});
  assert.match(request.url, /\/git\/refs$/);
  assert.deepEqual(JSON.parse(request.options.body), {ref: authorizationStateRef('gpoint', id, 'consumed'), sha: candidate});
});

test('authorization consumption fails closed when GitHub rejects marker creation', async () => {
  await assert.rejects(consumeTaskAuthorization({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async () => ({status: 403, ok: false})}), /consumption failed \(HTTP 403\)/);
});

test('lost marker-creation response is reconciled by exact readback without retrying the write', async () => {
  const requests = [];
  await consumeTaskAuthorization({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async (url, options) => {
      requests.push({url, options});
      if (options.method === 'POST') throw new Error('network response lost');
      return {status: 200, ok: true, json: async () => ({object: {sha: candidate}})};
    }});
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(requests[1].options.method, undefined);
});

test('TASK_SCOPED_AUTO permits unchanged DB heads and only an exact pre-approved migration contract', () => {
  const liveSha = 'f'.repeat(64);
  const manifest = {status: 'PLANNED', dbHeadBefore: 'prisma-1', dbHeadAfter: null,
    agataDbHeadBefore: 'agata-1', gsalesDbHeadBefore: 'gsales-1', gsalesMigrationSha256Before: liveSha,
    previousProductionSha: baseline, rollbackSha: baseline,
    liveFacts: {LIVE_AGATA_HEAD: 'agata-1', LIVE_GSALES_HEAD: 'gsales-1',
      LIVE_GSALES_MIGRATION_SHA256: liveSha, LIVE_ROLLBACK_IMAGE_AVAILABLE: 'YES'}};
  assert.deepEqual(assertNoTaskScopedMigrations({manifest, prismaHead: 'prisma-1', agataHead: 'agata-1',
    gsalesMigrationSha256: liveSha}), {state: 'NO_MIGRATION_CHANGE'});
  const migrationPath = 'packages/db/prisma/migrations/0099_add_field/migration.sql';
  const contract = {allowed_paths: [migrationPath], prisma_target: 'prisma-2', agata_target: 'agata-1',
    gsales_migration_sha256: 'e'.repeat(64), production_prisma_head: 'prisma-1',
    production_agata_head: 'agata-1', production_gsales_head: 'gsales-1',
    production_gsales_migration_sha256: liveSha, risk_description: 'Add a reviewed nullable field.',
    backup_plan: 'Create and verify full protected backup before migration.',
    rollback_plan: 'Restore exact verified backup or stop for forward fix.',
    stop_conditions: ['Stop if database baseline differs.', 'Stop if backup validation fails.'],
    required_gates: ['migration_preflight', 'backup_before_migration', 'rollback_readiness', 'tenant_isolation', 'postdeploy_health']};
  assert.equal(assertNoTaskScopedMigrations({manifest, prismaHead: 'prisma-2', agataHead: 'agata-1',
    gsalesMigrationSha256: 'e'.repeat(64), record: {risk_contracts: {database_migration: contract}},
    changedPaths: [migrationPath]}).state, 'PREAPPROVED_MIGRATION');
  assert.throws(() => assertNoTaskScopedMigrations({manifest, prismaHead: 'prisma-2', agataHead: 'agata-1',
    gsalesMigrationSha256: 'e'.repeat(64), changedPaths: [migrationPath]}), /exact pre-approved risk contract/);
  assert.throws(() => assertNoTaskScopedMigrations({manifest: {...manifest, rollbackSha: 'c'.repeat(40)},
    prismaHead: 'prisma-2', agataHead: 'agata-1', gsalesMigrationSha256: 'e'.repeat(64),
    record: {risk_contracts: {database_migration: contract}}, changedPaths: [migrationPath]}), /rollback or readiness differ/);
});
