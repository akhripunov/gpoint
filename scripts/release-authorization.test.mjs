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
  schema_version: 1,
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
  assert.throws(() => candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')), /before implementation/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('authorization state check accepts only absent revoked and consumed refs', async () => {
  const paths = [];
  const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async url => { paths.push(url); return {status: 404, ok: false}; }});
  assert.deepEqual(state, {revoked: false, consumed: false});
  assert.equal(paths.length, 2);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async () => ({status: 200, ok: true})}), /revoked or already consumed/);
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

test('TASK_SCOPED_AUTO requires all live DB heads and checksums to match without migration apply', () => {
  const manifest = {dbHeadBefore: 'prisma-1', agataDbHeadBefore: 'agata-1', gsalesDbHeadBefore: '001_gsales',
    liveFacts: {LIVE_GSALES_MIGRATION_SHA256: 'f'.repeat(64)}};
  assert.doesNotThrow(() => assertNoTaskScopedMigrations({manifest, prismaHead: 'prisma-1', agataHead: 'agata-1',
    gsalesMigrationSha256: 'f'.repeat(64)}));
  for (const change of [
    {prismaHead: 'prisma-2'}, {agataHead: 'agata-2'},
    {manifest: {...manifest, gsalesDbHeadBefore: 'NONE'}},
    {gsalesMigrationSha256: 'e'.repeat(64)},
  ]) assert.throws(() => assertNoTaskScopedMigrations({manifest, prismaHead: 'prisma-1', agataHead: 'agata-1',
    gsalesMigrationSha256: 'f'.repeat(64), ...change}), /cannot apply or reconcile database migrations/);
});
