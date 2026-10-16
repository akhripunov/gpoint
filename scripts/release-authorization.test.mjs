import test from 'node:test';
import assert from 'node:assert/strict';
import { validateTaskAuthorization, AuthorizationError, checkTaskAuthorizationState,
  ownerTaskCommitmentSha256,
  consumeTaskAuthorization, authorizationStateRef, assertNoTaskScopedMigrations } from './release-authorization.mjs';
import {candidateScopeDiff} from './release-authorization.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

const id = 'gpoint-task-2026-10-06-001';
const baseline = 'a'.repeat(40);
const candidate = 'b'.repeat(40);
const record = (extra = {}) => {
  const value = ({
  schema_version: 3,
  owner_instruction_scope: 'FULL_PRODUCTION_CYCLE',
  project: 'gpoint',
  environment: 'production',
  authorization_mode: 'TASK_SCOPED_AUTO',
  authorization_id: id,
  source_instruction_identifier: 'OWNER_TASK_UNIFIED_RELEASE_V1_2026_10_16',
  source_reference: 'https://linear.app/bestuzhev/issue/ME-31/task#comment-stale',
  candidate_selection_rule: 'EXACT_CURRENT_MAIN_WITHIN_AUTHORIZED_SCOPE',
  owner_task_reference: 'OWNER release task: ship the scoped GPoint menu change after required gates.',
  production_baseline_sha: baseline,
  task: 'Change one user-facing menu label',
  allowed_paths: ['apps/web/components/Menu.tsx'],
  allowed_operations: ['integrate', 'coalesce', 'prepare', 'preflight', 'deploy', 'verify', 'rollback', 'publish_evidence'],
  acceptance_criteria: ['the menu label is updated'],
  exclusions: ['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic', 'release_infrastructure'],
  state: 'ACTIVE', revoked: false, consumed: false,
  ...extra,
  });
  if (!Object.hasOwn(extra, 'owner_task_commitment_sha256')) {
    value.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(value);
  }
  return value;
};
const validate = (overrides = {}) => validateTaskAuthorization({
  record: record(), project: 'gpoint', authorizationId: id,
  candidateSha: candidate, currentMainSha: candidate,
  productionSha: baseline, changedPaths: ['apps/web/components/Menu.tsx'],
  ...overrides,
});

test('task authorization accepts exact candidate, baseline and allowed path', () => {
  assert.equal(validate().authorizationId, id);
});

test('direct OWNER release instruction authorizes scope without a live or current Linear reference', () => {
  const direct = record({source_instruction_identifier: 'OWNER_TASK_UNIFIED_RELEASE_V1_2026_10_16',
    source_reference: '', source_issue_identifier: '', source_comment_id: ''});
  const proof = validate({record: direct});
  assert.equal(proof.authorizationId, id);
  assert.match(proof.recordSha256, /^[a-f0-9]{64}$/);
});

test('direct OWNER authorization is independent of absent, stale, or unavailable Linear references', () => {
  const task = record({source_reference: '', source_issue_identifier: 'stale', source_comment_id: 'stale'});
  const proof = validate({record: task});
  assert.equal(proof.sourceInstructionIdentifier, task.source_instruction_identifier);
  assert.match(ownerTaskCommitmentSha256(task), /^[a-f0-9]{64}$/);
});

test('direct OWNER authorization requires its instruction identity and rejects commitment tampering', () => {
  assert.throws(() => validate({record: record({source_instruction_identifier: ''})}), /source instruction identifier/);
  assert.throws(() => validate({record: record({owner_task_commitment_sha256: '0'.repeat(64)})}), /commitment/);
});

test('task authorization requires dynamic exact-main candidate selection instead of a pinned stale SHA', () => {
  assert.throws(()=>validate({record:record({selected_candidate_sha:baseline})}),/must not pin a candidate SHA/);
  assert.throws(()=>validate({record:record({risk_contracts:{release_infrastructure:{selected_candidate_sha:baseline}}})}),/risk contracts must not pin/);
  assert.throws(()=>validate({record:record({candidate_selection_rule:'ANY_MAIN_SHA'})}),/candidate selection rule/);
});

for (const path of ['.github/workflows/gpoint-deploy.yml', 'scripts/release-gpoint.mjs',
  'packages/db/prisma/migrations/0001/migration.sql', 'services/agata/app/main.py',
  'services/core/auth.ts', 'services/gpoint-ads/direct.ts', 'payments/checkout.ts']) {
  test(`task authorization rejects owner-only path ${path}`, () => {
    assert.throws(() => validate({ record: record({ allowed_paths: [path] }), changedPaths: [path] }),
      (error) => error instanceof AuthorizationError && /(owner-only|risk contract)/.test(error.message));
  });
}

test('task authorization permits only explicitly contracted release-infrastructure paths', () => {
  const workflow = '.github/workflows/gpoint-deploy.yml';
  const risk = {
    allowed_paths: [workflow],
    baseline_sha: baseline,
    risk_description: 'Change the trusted GPoint release workflow under the owner release task.',
    allowed_operations: ['integrate', 'coalesce', 'prepare', 'preflight', 'deploy', 'verify', 'rollback', 'publish_evidence'],
    required_gates: ['exact_candidate', 'complete_scope', 'prepare_provenance', 'immutable_artifact',
      'security_review', 'preflight', 'rollback_readiness', 'worker_compatibility',
      'postdeploy_health', 'reconciliation'],
    backup_plan: 'Retain the previous immutable production manifest and delivery artifact.',
    rollback_plan: 'Restore the previous verified production manifest and reconcile partial apply.',
    stop_conditions: ['Stop if exact SHA or baseline differs.', 'Stop if rollback or health evidence is missing.'],
  };
  const exclusions = ['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic'];
  const proof = validate({
    record: record({allowed_paths: [workflow], exclusions, risk_contracts: {release_infrastructure: risk}}),
    changedPaths: [workflow],
  });
  assert.equal(proof.changedPaths, workflow);
  assert.throws(() => validate({record: record({allowed_paths: [workflow], exclusions}), changedPaths: [workflow]}),
    /risk contract/);
  assert.throws(() => validate({record: record({allowed_paths: [workflow], exclusions,
    risk_contracts: {release_infrastructure: risk}}), changedPaths: ['.github/workflows/other.yml']}),
  /risk contract/);
});

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
  for (const extra of [{ source_instruction_identifier: '' }, { owner_task_reference: '' }, { acceptance_criteria: [] }, { allowed_paths: [] },
    { exclusions: ['database'] }, { state: 'REVOKED' }, { revoked: true }, { consumed: true }]) {
    assert.throws(() => validate({ record: record(extra) }), AuthorizationError);
  }
});

test('task authorization requires explicit full-cycle owner intent in the durable record', () => {
  assert.throws(() => validate({record: record({owner_instruction_scope: 'PREPARE_ONLY'})}), /explicitly authorize the full production cycle/);
  assert.throws(() => validate({record: record({schema_version: 1})}), /Unsupported task authorization record/);
});

test('task authorization must be bound to production and development-only tasks cannot apply', () => {
  assert.throws(() => validate({record: record({environment: 'staging'})}), /production environment/);
  assert.throws(() => validate({record: record({owner_instruction_scope: 'PREPARE_ONLY'})}),
    /explicitly authorize the full production cycle/);
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

test('scope diff admits exact listed prepared commits before the task authorization', () => {
  const {root, git, baseline} = makeGitRepo();
  const preparedPath = '.github/workflows/gpoint-deploy.yml';
  fs.mkdirSync(path.dirname(path.join(root, preparedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, preparedPath), 'prepared release workflow\n');
  git('add', preparedPath); git('commit', '-m', 'owner-selected prepared release change');
  const preparedCommit = git('rev-parse', 'HEAD');
  const authPath = path.join(root, '.release-authorizations', `${id}.json`);
  fs.mkdirSync(path.dirname(authPath));
  fs.writeFileSync(authPath, JSON.stringify({preauthorized_commits: [preparedCommit],
    preauthorized_paths: [preparedPath], allowed_paths: [preparedPath, 'scripts/release-authorization.mjs']}) + '\n');
  git('add', '.release-authorizations'); git('commit', '-m', 'record owner release task authorization');
  fs.mkdirSync(path.join(root, 'scripts'), {recursive: true});
  fs.writeFileSync(path.join(root, 'scripts/release-authorization.mjs'), 'release auth fix\n');
  git('add', 'scripts/release-authorization.mjs'); git('commit', '-m', 'continue authorized integration');
  assert.deepEqual(candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')),
    [preparedPath, 'scripts/release-authorization.mjs']);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope diff rejects prepared history omitted from the authorization record', () => {
  const {root, git, baseline} = makeGitRepo();
  const preparedPath = '.github/workflows/gpoint-deploy.yml';
  fs.mkdirSync(path.dirname(path.join(root, preparedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, preparedPath), 'prepared release workflow\n');
  git('add', preparedPath); git('commit', '-m', 'unlisted prepared release change');
  const preparedCommit = git('rev-parse', 'HEAD');
  const authPath = path.join(root, '.release-authorizations', `${id}.json`);
  fs.mkdirSync(path.dirname(authPath));
  fs.writeFileSync(authPath, JSON.stringify({preauthorized_commits: [], preauthorized_paths: [],
    allowed_paths: [preparedPath, 'scripts/release-authorization.mjs']}) + '\n');
  git('add', '.release-authorizations'); git('commit', '-m', 'record owner release task authorization');
  fs.mkdirSync(path.join(root, 'scripts'), {recursive: true});
  fs.writeFileSync(path.join(root, 'scripts/release-authorization.mjs'), 'release auth fix\n');
  git('add', 'scripts/release-authorization.mjs'); git('commit', '-m', 'continue authorized integration');
  assert.throws(() => candidateScopeDiff(root, id, baseline, git('rev-parse', 'HEAD')),
    /candidate history is not exactly listed/);
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

test('a fresh mutation-boundary authorization read observes revocation after an earlier pass', async () => {
  let revoked = false;
  const fetchImpl = async url => {
    if (url.endsWith('/revoked') && revoked) return {status: 200, ok: true, json: async () => ({object: {sha: candidate}})};
    if (url.endsWith('/claimed')) return {status: 200, ok: true, json: async () => ({object: {sha: candidate}})};
    return {status: 404, ok: false};
  };
  assert.equal((await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl})).claimed, true);
  revoked = true;
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl}), /revoked or already consumed/);
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
