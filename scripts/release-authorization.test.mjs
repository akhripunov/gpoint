import test from 'node:test';
import assert from 'node:assert/strict';
import { validateTaskAuthorization, AuthorizationError, checkTaskAuthorizationState,
  ownerTaskCommitmentSha256,
  consumeTaskAuthorization, authorizationStateRef, assertNoTaskScopedMigrations } from './release-authorization.mjs';
import {candidateScopeDiff, candidateScopeAmendments, scopeAmendmentCommitmentSha256} from './release-authorization.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';

const id = 'gpoint-task-2026-10-06-001';
const scopeAddendumAuthorizationId = 'gpoint-unified-release-v1-20261016';
const scopeAddendumId = 'gpoint-unified-release-v1-summary-test-2026-10-17';
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

test('append-only owner scope addendum cannot hide an unauthorized path by mutating verified scope', () => {
  const {root, git, baseline} = makeGitRepo();
  const originalAuth = record({authorization_id: scopeAddendumAuthorizationId, production_baseline_sha: baseline});
  originalAuth.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(originalAuth);
  const authPath = `.release-authorizations/${scopeAddendumAuthorizationId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, authPath)), {recursive: true});
  fs.writeFileSync(path.join(root, authPath), `${JSON.stringify(originalAuth)}\n`);
  git('add', authPath); git('commit', '-m', 'record original owner task');
  const originalRecordBlob = git(`rev-parse`, `HEAD:${authPath}`);

  const scopedPath = 'tests/test_prepare_workflow_summary.py';
  fs.mkdirSync(path.dirname(path.join(root, scopedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, scopedPath), 'assert_shell_syntax()\n');
  git('add', scopedPath); git('commit', '-m', 'add exact release summary regression test');
  const sourceCommit = git('rev-parse', 'HEAD');
  const patch = execFileSync('git', ['show', '--format=', '--no-ext-diff', sourceCommit, '--', scopedPath], {cwd: root});
  const patchSha = createHash('sha256').update(patch).digest('hex');
  const fileSha = createHash('sha256').update(fs.readFileSync(path.join(root, scopedPath))).digest('hex');
  const blobSha = git('rev-parse', `${sourceCommit}:${scopedPath}`);
  const amendment = {
    schema_version: 1,
    amendment_id: scopeAddendumId,
    project: 'gpoint',
    authorization_id: scopeAddendumAuthorizationId,
    source_instruction_identifier: 'OWNER_SCOPE_ADDENDUM_GPOINT_RELEASE_20261017',
    owner_decision: 'Authorize only the Prepare workflow summary regression test path.',
    original_authorization_record_blob_sha: originalRecordBlob,
    scope_additions: [{path: scopedPath, source_commit_sha: sourceCommit,
      patch_sha256: patchSha, result_file_sha256: fileSha, result_blob_sha: blobSha}],
  };
  amendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(amendment);
  const amendmentsPath = `.release-authorizations/addenda/${scopeAddendumAuthorizationId}/${amendment.amendment_id}.json`;
  fs.mkdirSync(path.dirname(path.join(root, amendmentsPath)), {recursive: true});
  fs.writeFileSync(path.join(root, amendmentsPath), `${JSON.stringify(amendment)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'record exact owner scope addendum');
  const approvedCandidate = git('rev-parse', 'HEAD');
  const approvedPaths = candidateScopeDiff(root, scopeAddendumAuthorizationId, baseline, approvedCandidate);
  assert.deepEqual(approvedPaths, [scopedPath]);
  assert.doesNotThrow(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId: scopeAddendumAuthorizationId, candidateSha: approvedCandidate,
    currentMainSha: approvedCandidate, productionSha: baseline, changedPaths: approvedPaths}));
  const unauthorizedPath = 'tests/unapproved_scope_test.py';
  fs.mkdirSync(path.dirname(path.join(root, unauthorizedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, unauthorizedPath), 'unapproved\n');
  git('add', unauthorizedPath); git('commit', '-m', 'add unapproved release change');
  const candidate = git('rev-parse', 'HEAD');

  const changedPaths = candidateScopeDiff(root, scopeAddendumAuthorizationId, baseline, candidate);
  assert.deepEqual(changedPaths, [scopedPath, unauthorizedPath]);
  assert.throws(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId: scopeAddendumAuthorizationId, candidateSha: candidate, currentMainSha: candidate,
    productionSha: baseline, changedPaths}), /outside authorized scope/);
  assert.throws(() => changedPaths.splice(changedPaths.indexOf(unauthorizedPath), 1), TypeError,
    'callers must not be able to mutate the exact path list after scope proof is attached');
  assert.deepEqual(changedPaths, [scopedPath, unauthorizedPath]);
  const amendedPaths = candidateScopeAmendments(root, scopeAddendumAuthorizationId, baseline, candidate);
  assert.deepEqual(amendedPaths, [scopedPath]);
  assert.throws(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId: scopeAddendumAuthorizationId, candidateSha: candidate, currentMainSha: candidate,
    productionSha: baseline, changedPaths, amendedPaths}), /outside authorized scope/);
  assert.throws(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId: scopeAddendumAuthorizationId, candidateSha: 'f'.repeat(40), currentMainSha: 'f'.repeat(40),
    productionSha: baseline, changedPaths: [scopedPath], amendedPaths}),
  /outside authorized scope/);
  assert.throws(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId: scopeAddendumAuthorizationId, candidateSha: candidate, currentMainSha: candidate,
    productionSha: baseline, changedPaths}), /outside authorized scope/,
  'the normal candidate diff API must keep unapproved paths in the verified set');
  assert.equal(git('rev-parse', `${candidate}:${authPath}`), originalRecordBlob,
    'the original authorization record must remain byte-identical');
  fs.writeFileSync(path.join(root, scopedPath), 'modified after owner amendment\n');
  git('add', scopedPath); git('commit', '-m', 'change scoped test after its exact addendum');
  const driftedCandidate = git('rev-parse', 'HEAD');
  assert.throws(() => candidateScopeAmendments(root, scopeAddendumAuthorizationId, baseline, driftedCandidate),
    /diff, blob, or candidate file does not match/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope addendum remains valid when its source branch is integrated by a non-squash PR merge', () => {
  const {root, git, baseline: repoBaseline} = makeGitRepo();
  git('branch', 'base', repoBaseline);
  git('switch', '-c', 'authorized-release');
  const originalAuth = record({authorization_id: scopeAddendumAuthorizationId, production_baseline_sha: repoBaseline});
  originalAuth.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(originalAuth);
  const authPath = `.release-authorizations/${scopeAddendumAuthorizationId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, authPath)), {recursive: true});
  fs.writeFileSync(path.join(root, authPath), `${JSON.stringify(originalAuth)}\n`);
  git('add', authPath); git('commit', '-m', 'record original owner task');
  const originalRecordBlob = git('rev-parse', `HEAD:${authPath}`);

  const scopedPath = 'tests/test_prepare_workflow_summary.py';
  fs.mkdirSync(path.dirname(path.join(root, scopedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, scopedPath), 'assert_shell_syntax()\n');
  git('add', scopedPath); git('commit', '-m', 'add exact release summary regression test');
  const sourceCommit = git('rev-parse', 'HEAD');
  const patch = execFileSync('git', ['show', '--format=', '--no-ext-diff', sourceCommit, '--', scopedPath], {cwd: root});
  const amendment = {
    schema_version: 1,
    amendment_id: scopeAddendumId,
    project: 'gpoint',
    authorization_id: scopeAddendumAuthorizationId,
    source_instruction_identifier: 'OWNER_SCOPE_ADDENDUM_GPOINT_RELEASE_20261017',
    owner_decision: 'Authorize only the Prepare workflow summary regression test path.',
    original_authorization_record_blob_sha: originalRecordBlob,
    scope_additions: [{path: scopedPath, source_commit_sha: sourceCommit,
      patch_sha256: createHash('sha256').update(patch).digest('hex'),
      result_file_sha256: createHash('sha256').update(fs.readFileSync(path.join(root, scopedPath))).digest('hex'),
      result_blob_sha: git('rev-parse', `${sourceCommit}:${scopedPath}`)}],
  };
  amendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(amendment);
  const amendmentsPath = `.release-authorizations/addenda/${scopeAddendumAuthorizationId}/${amendment.amendment_id}.json`;
  fs.mkdirSync(path.dirname(path.join(root, amendmentsPath)), {recursive: true});
  fs.writeFileSync(path.join(root, amendmentsPath), `${JSON.stringify(amendment)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'record exact owner scope addendum');

  git('switch', 'base');
  git('merge', '--no-ff', 'authorized-release', '-m', 'merge authorized release');
  const candidate = git('rev-parse', 'HEAD');
  assert.deepEqual(candidateScopeAmendments(root, scopeAddendumAuthorizationId, repoBaseline, candidate), [scopedPath]);
  assert.deepEqual(candidateScopeDiff(root, scopeAddendumAuthorizationId, repoBaseline, candidate), [scopedPath]);
  fs.rmSync(root, {recursive: true, force: true});
});

test('scope addendum rejects a different path even when its proof and commitment are valid', () => {
  const {root, git, baseline} = makeGitRepo();
  const originalAuth = record({authorization_id: scopeAddendumAuthorizationId, production_baseline_sha: baseline});
  originalAuth.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(originalAuth);
  const authPath = `.release-authorizations/${scopeAddendumAuthorizationId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, authPath)), {recursive: true});
  fs.writeFileSync(path.join(root, authPath), `${JSON.stringify(originalAuth)}\n`);
  git('add', authPath); git('commit', '-m', 'record original owner task');
  const unauthorizedPath = 'tests/another_test.py';
  fs.mkdirSync(path.dirname(path.join(root, unauthorizedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, unauthorizedPath), 'unapproved test path\n');
  git('add', unauthorizedPath); git('commit', '-m', 'add another test path');
  const sourceCommit = git('rev-parse', 'HEAD');
  const patch = execFileSync('git', ['show', '--format=', '--no-ext-diff', sourceCommit, '--', unauthorizedPath], {cwd: root});
  const amendment = {schema_version: 1, amendment_id: scopeAddendumId, project: 'gpoint',
    authorization_id: scopeAddendumAuthorizationId,
    source_instruction_identifier: 'OWNER_SCOPE_ADDENDUM_GPOINT_RELEASE_20261017',
    owner_decision: 'Authorize only one exact regression test path for this release task.',
    original_authorization_record_blob_sha: git('rev-parse', `HEAD~1:${authPath}`),
    scope_additions: [{path: unauthorizedPath, source_commit_sha: sourceCommit,
      patch_sha256: createHash('sha256').update(patch).digest('hex'),
      result_file_sha256: createHash('sha256').update(fs.readFileSync(path.join(root, unauthorizedPath))).digest('hex'),
      result_blob_sha: git('rev-parse', `${sourceCommit}:${unauthorizedPath}`)}]};
  amendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(amendment);
  const amendmentsPath = `.release-authorizations/addenda/${scopeAddendumAuthorizationId}/${scopeAddendumId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, amendmentsPath)), {recursive: true});
  fs.writeFileSync(path.join(root, amendmentsPath), `${JSON.stringify(amendment)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'record exact owner scope addendum');

  assert.throws(() => candidateScopeAmendments(root, scopeAddendumAuthorizationId, baseline,
    git('rev-parse', 'HEAD')), /does not match the approved scope addition/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('legacy test-only addendum cannot authorize modification of a pre-existing file', () => {
  const {root, git} = makeGitRepo();
  const scopedPath = 'tests/test_prepare_workflow_summary.py';
  fs.mkdirSync(path.dirname(path.join(root, scopedPath)), {recursive: true});
  fs.writeFileSync(path.join(root, scopedPath), 'baseline test\n');
  git('add', scopedPath); git('commit', '-m', 'baseline existing test');
  const baselineSha = git('rev-parse', 'HEAD');
  const originalAuth = record({authorization_id: scopeAddendumAuthorizationId, production_baseline_sha: baselineSha});
  originalAuth.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(originalAuth);
  const authPath = `.release-authorizations/${scopeAddendumAuthorizationId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, authPath)), {recursive: true});
  fs.writeFileSync(path.join(root, authPath), `${JSON.stringify(originalAuth)}\n`);
  git('add', authPath); git('commit', '-m', 'record original owner task');
  const originalRecordBlob = git('rev-parse', `HEAD:${authPath}`);

  fs.writeFileSync(path.join(root, scopedPath), 'modified test\n');
  git('add', scopedPath); git('commit', '-m', 'modify existing test');
  const sourceCommit = git('rev-parse', 'HEAD');
  const patch = execFileSync('git', ['show', '--format=', '--no-ext-diff', sourceCommit, '--', scopedPath], {cwd: root});
  const amendment = {schema_version: 1, amendment_id: scopeAddendumId, project: 'gpoint',
    authorization_id: scopeAddendumAuthorizationId,
    source_instruction_identifier: 'OWNER_SCOPE_ADDENDUM_GPOINT_RELEASE_20261017',
    owner_decision: 'Authorize only adding the exact Prepare summary regression test path.',
    original_authorization_record_blob_sha: originalRecordBlob,
    scope_additions: [{path: scopedPath, source_commit_sha: sourceCommit,
      patch_sha256: createHash('sha256').update(patch).digest('hex'),
      result_file_sha256: createHash('sha256').update(fs.readFileSync(path.join(root, scopedPath))).digest('hex'),
      result_blob_sha: git('rev-parse', `${sourceCommit}:${scopedPath}`)}]};
  amendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(amendment);
  const amendmentsPath = `.release-authorizations/addenda/${scopeAddendumAuthorizationId}/${scopeAddendumId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, amendmentsPath)), {recursive: true});
  fs.writeFileSync(path.join(root, amendmentsPath), `${JSON.stringify(amendment)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'record attempted legacy scope amendment');
  assert.throws(() => candidateScopeAmendments(root, scopeAddendumAuthorizationId, baselineSha,
    git('rev-parse', 'HEAD')), /did not add the exact path/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('ME-38 owner addendum binds four exact sync paths and release infrastructure risk contract', () => {
  const {root, git, baseline: repoBaseline} = makeGitRepo();
  const authorizationId = 'gpoint-unified-release-v1-recovery-20261017';
  const amendmentId = 'gpoint-me38-sync-release-infrastructure-20261017';
  const sourceInstructionIdentifier = 'OWNER_SCOPE_ADDENDUM_GPOINT_ME38_20261017';
  const paths = ['.github/workflows/gpoint-linear-reconcile.yml',
    '.github/workflows/sync-agents-to-linear.yml', 'scripts/sync_agents_to_linear.py',
    'tests/test_sync_agents_to_linear.py'];
  for (const [index, scopedPath] of paths.entries()) {
    fs.mkdirSync(path.dirname(path.join(root, scopedPath)), {recursive: true});
    fs.writeFileSync(path.join(root, scopedPath), `baseline ${index}\n`);
  }
  git('add', ...paths); git('commit', '-m', 'baseline target paths');
  const measuredBaseline = git('rev-parse', 'HEAD');
  const originalAuth = record({authorization_id: authorizationId,
    source_instruction_identifier: 'OWNER_TASK_GPOINT_INFRASTRUCTURE_RECOVERY_20261017',
    production_baseline_sha: measuredBaseline,
    allowed_paths: ['scripts/release-authorization.mjs', 'scripts/release-authorization.test.mjs'],
    exclusions: ['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic'],
    risk_contracts: {release_infrastructure: {allowed_paths: [], baseline_sha: measuredBaseline}}});
  originalAuth.owner_task_commitment_sha256 = ownerTaskCommitmentSha256(originalAuth);
  const authPath = `.release-authorizations/${authorizationId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, authPath)), {recursive: true});
  fs.writeFileSync(path.join(root, authPath), `${JSON.stringify(originalAuth)}\n`);
  git('add', authPath); git('commit', '-m', 'record original owner task');
  const originalRecordBlob = git('rev-parse', `HEAD:${authPath}`);

  const additions = [];
  for (const [index, scopedPath] of paths.entries()) {
    fs.writeFileSync(path.join(root, scopedPath), `approved ME-38 path ${index}\n`);
    git('add', scopedPath); git('commit', '-m', `add approved ME-38 path ${index}`);
    const sourceCommitSha = git('rev-parse', 'HEAD');
    const patch = execFileSync('git', ['show', '--format=', '--no-ext-diff', sourceCommitSha, '--', scopedPath], {cwd: root});
    additions.push({path: scopedPath, source_commit_sha: sourceCommitSha,
      patch_sha256: createHash('sha256').update(patch).digest('hex'),
      result_file_sha256: createHash('sha256').update(fs.readFileSync(path.join(root, scopedPath))).digest('hex'),
      result_blob_sha: git('rev-parse', `${sourceCommitSha}:${scopedPath}`)});
  }
  const riskContract = {
    allowed_paths: paths,
    baseline_sha: measuredBaseline,
    risk_description: 'Change only the four named GPoint release publication and Linear synchronization paths.',
    allowed_operations: ['integrate', 'coalesce', 'prepare', 'preflight', 'deploy', 'verify', 'rollback', 'publish_evidence'],
    required_gates: ['exact_candidate', 'complete_scope', 'prepare_provenance', 'immutable_artifact',
      'security_review', 'preflight', 'rollback_readiness', 'worker_compatibility',
      'postdeploy_health', 'reconciliation'],
    backup_plan: 'Retain current production ledger and immutable release artifacts before any apply.',
    rollback_plan: 'Restore the previous immutable manifest and reconcile any uncertain apply before retry.',
    stop_conditions: ['Stop on candidate, scope, or production baseline drift.',
      'Stop if provenance, backup, rollback, or health evidence is incomplete.'],
  };
  const amendment = {schema_version: 1, amendment_id: amendmentId, project: 'gpoint',
    authorization_id: authorizationId, source_instruction_identifier: sourceInstructionIdentifier,
    owner_decision: 'Continue ME-38 by preparing an append-only authorization for exactly the four named paths and their complete release-infrastructure risk contract; GitHub publication requires the specified separate one-Prepare approval.',
    original_authorization_record_blob_sha: originalRecordBlob,
    scope_additions: additions, release_infrastructure_risk_contract: riskContract};
  amendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(amendment);
  const amendmentsPath = `.release-authorizations/addenda/${authorizationId}/${amendmentId}.json`;
  fs.mkdirSync(path.dirname(path.join(root, amendmentsPath)), {recursive: true});
  fs.writeFileSync(path.join(root, amendmentsPath), `${JSON.stringify(amendment)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'record ME-38 exact owner scope addendum');
  const candidateSha = git('rev-parse', 'HEAD');

  const approvedPaths = candidateScopeDiff(root, authorizationId, measuredBaseline, candidateSha);
  assert.deepEqual(approvedPaths, paths.sort());
  assert.doesNotThrow(() => validateTaskAuthorization({record: originalAuth, project: 'gpoint',
    authorizationId, candidateSha, currentMainSha: candidateSha, productionSha: measuredBaseline,
    changedPaths: approvedPaths}));
  assert.equal(git('rev-parse', `${candidateSha}:${authPath}`), originalRecordBlob,
    'the original immutable owner authorization must remain unchanged');

  const addendumBlobPath = path.join(root, amendmentsPath);
  git('switch', '-c', 'invalid-risk-contract', `${candidateSha}^`);
  fs.mkdirSync(path.dirname(addendumBlobPath), {recursive: true});
  fs.writeFileSync(addendumBlobPath, execFileSync('git', ['show', `${candidateSha}:${amendmentsPath}`], {cwd: root}));
  const invalidRisk = JSON.parse(fs.readFileSync(addendumBlobPath, 'utf8'));
  invalidRisk.release_infrastructure_risk_contract.rollback_plan = '';
  invalidRisk.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(invalidRisk);
  fs.writeFileSync(addendumBlobPath, `${JSON.stringify(invalidRisk)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'attempt incomplete risk amendment');
  assert.throws(() => candidateScopeDiff(root, authorizationId, measuredBaseline, git('rev-parse', 'HEAD')),
    /risk contract is incomplete/);

  git('switch', '-c', 'invalid-scope-addition', `${candidateSha}^`);
  fs.mkdirSync(path.dirname(addendumBlobPath), {recursive: true});
  fs.writeFileSync(addendumBlobPath, execFileSync('git', ['show', `${candidateSha}:${amendmentsPath}`], {cwd: root}));
  const invalidScope = JSON.parse(fs.readFileSync(addendumBlobPath, 'utf8'));
  invalidScope.scope_additions[0].path = 'tests/unapproved_scope_test.py';
  invalidScope.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(invalidScope);
  fs.writeFileSync(addendumBlobPath, `${JSON.stringify(invalidScope)}\n`);
  git('add', amendmentsPath); git('commit', '-m', 'attempt unapproved scope amendment');
  assert.throws(() => candidateScopeDiff(root, authorizationId, measuredBaseline, git('rev-parse', 'HEAD')),
    /approved scope additions/);

  git('switch', '-c', 'merge-parent', `${candidateSha}^`);
  fs.writeFileSync(path.join(root, 'README.md'), 'merge-side change\n');
  git('add', 'README.md'); git('commit', '-m', 'merge side change');
  git('merge', '--no-ff', candidateSha, '-m', 'integrate approved branch');
  const mergedAmendment = JSON.parse(fs.readFileSync(addendumBlobPath, 'utf8'));
  mergedAmendment.release_infrastructure_risk_contract.risk_description = 'Changed during merge after source authorization was created.';
  mergedAmendment.amendment_commitment_sha256 = scopeAmendmentCommitmentSha256(mergedAmendment);
  fs.writeFileSync(addendumBlobPath, `${JSON.stringify(mergedAmendment)}\n`);
  git('add', amendmentsPath); git('commit', '--amend', '--no-edit');
  assert.throws(() => candidateScopeAmendments(root, authorizationId, measuredBaseline, git('rev-parse', 'HEAD')),
    /blob changed after its immutable source commit/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('authorization state check observes revoked, consumed and claimed markers', async () => {
  const paths = [];
  const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async url => { paths.push(url); return {status: 404, ok: false}; }});
  assert.deepEqual(state, {revoked: false, consumed: false, claimed: false});
  assert.equal(paths.length, 3);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: 'test',
    fetchImpl: async () => ({status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: candidate}})})}), /revoked or already consumed/);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async () => ({status: 404, ok: false})}), /no active release claim/);
  const earlierCandidateClaim = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl: async url => url.includes('/claimed')
      ? ({status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: 'c'.repeat(40)}})})
      : ({status: 404, ok: false})});
  assert.equal(earlierCandidateClaim.claimedSha, 'c'.repeat(40));
  const claimed = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async url => url.includes('/claimed')
      ? ({status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: candidate}})})
      : ({status: 404, ok: false})});
  assert.equal(claimed.claimed, true);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, candidateSha: candidate, token: 'test',
    fetchImpl: async url => url.includes('/claimed')
      ? ({status: 200, ok: true, json: async () => ({object: {type: 'blob', sha: candidate}})})
      : ({status: 404, ok: false})}), /not a valid commit reference/);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id, token: '',
    fetchImpl: async () => assert.fail('must not query without token')}), /token is required/);
});

test('authorization evidence can verify an exact consumed candidate without treating it as reusable', async () => {
  const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, consumedCandidateSha: candidate, token: 'test',
    fetchImpl: async url => {
      if (url.endsWith('/claimed')) return {status: 200, ok: true,
        json: async () => ({object: {type: 'commit', sha: 'c'.repeat(40)}})};
      if (url.endsWith('/consumed')) return {status: 200, ok: true,
        json: async () => ({object: {type: 'commit', sha: candidate}})};
      return {status: 404, ok: false};
    }});
  assert.equal(state.consumed, true);
  assert.equal(state.consumedSha, candidate);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, consumedCandidateSha: 'd'.repeat(40), token: 'test',
    fetchImpl: async url => url.endsWith('/claimed')
      ? {status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: 'c'.repeat(40)}})}
      : url.endsWith('/consumed')
        ? {status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: candidate}})}
        : {status: 404, ok: false}}), /Consumed marker candidate differs from the exact authorization candidate/);
});

test('a fresh mutation-boundary authorization read observes revocation after an earlier pass', async () => {
  let revoked = false;
  const fetchImpl = async url => {
    if (url.endsWith('/revoked') && revoked) return {status: 200, ok: true, json: async () => ({object: {sha: candidate}})};
    if (url.endsWith('/claimed')) return {status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: candidate}})};
    return {status: 404, ok: false};
  };
  assert.equal((await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl})).claimed, true);
  revoked = true;
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl}), /revoked or already consumed/);
});

test('a claimed task authorization can cover a new exact candidate while revoked or consumed states still block', async () => {
  const earlierSha = 'c'.repeat(40);
  const fetchImpl = async url => {
    if (url.endsWith('/claimed')) return {status: 200, ok: true, json: async () => ({object: {type: 'commit', sha: earlierSha}})};
    return {status: 404, ok: false};
  };
  const state = await checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl});
  assert.equal(state.claimed, true);
  assert.equal(state.claimedSha, earlierSha);
  await assert.rejects(checkTaskAuthorizationState({project: 'gpoint', authorizationId: id,
    candidateSha: candidate, token: 'test', fetchImpl: async url => url.endsWith('/consumed')
      ? {status: 200, ok: true, json: async () => ({object: {sha: earlierSha}})} : fetchImpl(url)}),
  /consumed marker is not a valid commit reference|revoked or already consumed/);
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
