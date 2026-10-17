/** Validate durable, task-scoped TASK_SCOPED_AUTO authorization records. */
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';

const SHA = /^[a-f0-9]{40}$/;
const MIGRATION_TARGET = /^[A-Za-z0-9_.-]{1,128}$/;
const AUTH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/;
const OWNER_INSTRUCTION_ID = /^OWNER_TASK_[A-Z0-9_]{8,96}$/;
const REQUIRED_EXCLUSIONS = new Set(['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic']);
const RELEASE_INFRASTRUCTURE_GATES = ['exact_candidate', 'complete_scope', 'prepare_provenance',
  'immutable_artifact', 'security_review', 'preflight', 'rollback_readiness',
  'worker_compatibility', 'postdeploy_health', 'reconciliation'];
const GPOINT_RELEASE_SCOPE_ADDENDUM = Object.freeze({
  authorizationId: 'gpoint-unified-release-v1-20261016',
  amendmentId: 'gpoint-unified-release-v1-summary-test-2026-10-17',
  sourceInstructionIdentifier: 'OWNER_SCOPE_ADDENDUM_GPOINT_RELEASE_20261017',
  path: 'tests/test_prepare_workflow_summary.py',
});
const VERIFIED_CANDIDATE_AMENDMENTS = new WeakMap();
const OWNER_ONLY_PREFIXES = [
  '.github/', 'packages/db/', 'migrations/', 'db/', 'deploy/', 'docker/',
  'docs/release/', 'scripts/release', 'scripts/prepare-release', 'scripts/select-release-run',
  'scripts/publish-release', 'services/agata/', 'services/core/', 'services/core-contracts/',
  'services/gpoint-ads/', 'services/ads/', 'services/traffic/', 'services/payments/', 'services/gsales/migrations/',
  'payments/', 'billing/', '.env', 'secrets/',
];

export class AuthorizationError extends Error {}

const canonicalize = value => Array.isArray(value) ? value.map(canonicalize) :
  value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([key, child]) => [key, canonicalize(child)])) : value;

export function ownerTaskCommitment(record) {
  return canonicalize({schema: 1, authorization_id: record.authorization_id,
    project: record.project, environment: record.environment,
    source_instruction_identifier: record.source_instruction_identifier,
    owner_task_reference: record.owner_task_reference,
    owner_instruction_scope: record.owner_instruction_scope,
    production_baseline_sha: record.production_baseline_sha,
    candidate_selection_rule: record.candidate_selection_rule,
    task: record.task, allowed_paths: [...(record.allowed_paths ?? [])].sort(),
    allowed_operations: [...(record.allowed_operations ?? [])].sort(),
    preauthorized_commits: [...(record.preauthorized_commits ?? [])].sort(),
    preauthorized_paths: [...(record.preauthorized_paths ?? [])].sort(),
    acceptance_criteria: [...(record.acceptance_criteria ?? [])].sort(),
    exclusions: [...(record.exclusions ?? [])].sort(),
    risk_contracts: record.risk_contracts ?? {}});
}

export function ownerTaskCommitmentSha256(record) {
  return crypto.createHash('sha256').update(JSON.stringify(ownerTaskCommitment(record)), 'utf8').digest('hex');
}

export function verifyOwnerTaskRecord(record) {
  const identifier = record?.source_instruction_identifier ?? '';
  if (!OWNER_INSTRUCTION_ID.test(identifier)) {
    throw new AuthorizationError('Direct OWNER source instruction identifier is missing or invalid');
  }
  const expectedCommitment = ownerTaskCommitmentSha256(record);
  if (record.owner_task_commitment_sha256 !== expectedCommitment) {
    throw new AuthorizationError('Direct OWNER task scope commitment does not match the durable record');
  }
  return {sourceInstructionIdentifier: identifier, commitmentSha256: expectedCommitment};
}

export function authorizationStateRef(project, authorizationId, state) {
  if (!['maxibot', 'gpoint'].includes(String(project).toLowerCase()) || !AUTH_ID.test(authorizationId)) {
    throw new AuthorizationError('Invalid project-scoped authorization ID');
  }
  if (!['revoked', 'consumed', 'claimed'].includes(state)) throw new AuthorizationError('Invalid authorization state marker');
  return `refs/tags/${String(project).toLowerCase()}-task-auth/${authorizationId}/${state}`;
}

export function authorizationRecordPath(authorizationId) {
  if (!AUTH_ID.test(authorizationId ?? '')) throw new AuthorizationError('Invalid task authorization ID');
  return `.release-authorizations/${authorizationId}.json`;
}

const amendmentDirectory = authorizationId => `.release-authorizations/addenda/${authorizationId}`;
const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const verifiedAmendedPaths = paths => Object.freeze([...new Set(paths)].sort());
const NO_SCOPE_AMENDMENTS = Object.freeze([]);

function amendmentCommitment(value) {
  return canonicalize({schema_version: value.schema_version, amendment_id: value.amendment_id,
    project: value.project, authorization_id: value.authorization_id,
    source_instruction_identifier: value.source_instruction_identifier,
    owner_decision: value.owner_decision,
    original_authorization_record_blob_sha: value.original_authorization_record_blob_sha,
    scope_additions: value.scope_additions});
}

export function scopeAmendmentCommitmentSha256(value) {
  return sha256(JSON.stringify(amendmentCommitment(value)));
}

/** Verify append-only, exact-file scope amendments without changing the original authorization. */
export function candidateScopeAmendments(repoRoot, authorizationId, baselineSha, candidateSha) {
  const dir = amendmentDirectory(authorizationId);
  const git = (...args) => execFileSync('git', args, {cwd: repoRoot, encoding: 'utf8', timeout: 20_000}).trim();
  const bytes = (...args) => execFileSync('git', args, {cwd: repoRoot, encoding: 'buffer', timeout: 20_000, maxBuffer: 4 * 1024 * 1024});
  try {
    const recordPath = authorizationRecordPath(authorizationId);
    const recordBytes = bytes('show', `${candidateSha}:${recordPath}`);
    const record = JSON.parse(recordBytes.toString('utf8'));
    const originalRecordBlob = git('rev-parse', `${candidateSha}:${recordPath}`);
    const names = git('diff', '--name-only', baselineSha, candidateSha, '--', dir).split('\n').filter(Boolean);
    const treeNames = git('ls-tree', '-r', '--name-only', candidateSha, '--', dir).split('\n').filter(Boolean);
    if (names.length === 0) return NO_SCOPE_AMENDMENTS;
    if (names.length !== 1 || treeNames.length !== 1 || names[0] !== treeNames[0]
        || !names[0].startsWith(`${dir}/`) || names[0].slice(dir.length + 1).includes('/')) {
      throw new AuthorizationError('Scope amendment must be a single new immutable file');
    }
    const amendmentPath = names[0];
    // Per-commit history is checked below; the candidate tree itself must contain exactly one amendment.
    const commits = git('rev-list', '--topo-order', '--reverse', '--ancestry-path', `${baselineSha}..${candidateSha}`).split('\n').filter(Boolean);
    const history = [];
    for (const commit of commits) {
      const parents = git('rev-list', '--parents', '-n', '1', commit).split(' ').slice(1);
      // A normal PR merge carries the source addition into its tree; only the source commit
      // establishes the immutable addendum, while the final tree/blob checks cover integration.
      if (parents.length > 1) continue;
      const output = git('diff-tree', '--root', '--first-parent', '--no-commit-id', '--name-status', '-r', commit);
      for (const line of output.split('\n').filter(Boolean)) {
        const [status, changed] = line.split('\t');
        if (changed === amendmentPath) history.push({commit, status});
      }
    }
    if (history.length !== 1 || history[0].status !== 'A') {
      throw new AuthorizationError('Scope amendment must be added once and remain immutable');
    }
    const amendmentBytes = bytes('show', `${candidateSha}:${amendmentPath}`);
    const value = JSON.parse(amendmentBytes.toString('utf8'));
    if (value.schema_version !== 1 || value.project !== record.project || value.authorization_id !== authorizationId
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/.test(value.amendment_id ?? '')
        || amendmentPath !== `${dir}/${value.amendment_id}.json`
        || !/^OWNER_SCOPE_ADDENDUM_[A-Z0-9_]{8,96}$/.test(value.source_instruction_identifier ?? '')
        || typeof value.owner_decision !== 'string' || value.owner_decision.trim().length < 20
        || value.original_authorization_record_blob_sha !== originalRecordBlob
        || value.amendment_commitment_sha256 !== scopeAmendmentCommitmentSha256(value)
        || !Array.isArray(value.scope_additions) || value.scope_additions.length !== 1) {
      throw new AuthorizationError('Scope amendment identity or commitment is invalid');
    }
    if (authorizationId !== GPOINT_RELEASE_SCOPE_ADDENDUM.authorizationId
        || value.amendment_id !== GPOINT_RELEASE_SCOPE_ADDENDUM.amendmentId
        || value.source_instruction_identifier !== GPOINT_RELEASE_SCOPE_ADDENDUM.sourceInstructionIdentifier) {
      throw new AuthorizationError('Scope amendment identity is not an approved addendum');
    }
    const addition = value.scope_additions[0];
    const scopedPath = addition.path;
    if (scopedPath !== GPOINT_RELEASE_SCOPE_ADDENDUM.path) {
      throw new AuthorizationError('Scope amendment does not match the approved scope addition');
    }
    if (typeof scopedPath !== 'string' || !scopedPath || scopedPath.startsWith('/')
        || scopedPath.split('/').includes('..') || inScope(scopedPath, record.allowed_paths)
        || ownerOnly(scopedPath) || !SHA.test(addition.source_commit_sha ?? '')
        || !/^[a-f0-9]{64}$/.test(addition.patch_sha256 ?? '')
        || !/^[a-f0-9]{64}$/.test(addition.result_file_sha256 ?? '')
        || !/^[a-f0-9]{40}$/.test(addition.result_blob_sha ?? '')) {
      throw new AuthorizationError('Scope amendment path or source proof is invalid');
    }
    execFileSync('git', ['merge-base', '--is-ancestor', addition.source_commit_sha, candidateSha], {cwd: repoRoot, stdio: 'ignore', timeout: 20_000});
    const sourceNames = git('diff-tree', '--root', '--no-commit-id', '--name-status', '-r', '--no-renames', addition.source_commit_sha)
      .split('\n').filter(Boolean).map(line => line.split('\t'));
    if (!sourceNames.some(([status, name]) => status === 'A' && name === scopedPath)) {
      throw new AuthorizationError('Scope amendment source commit did not add the exact path');
    }
    const sourcePatch = bytes('show', '--format=', '--no-ext-diff', addition.source_commit_sha, '--', scopedPath);
    const candidateFile = bytes('show', `${candidateSha}:${scopedPath}`);
    if (sha256(sourcePatch) !== addition.patch_sha256
        || sha256(candidateFile) !== addition.result_file_sha256
        || git('rev-parse', `${addition.source_commit_sha}:${scopedPath}`) !== addition.result_blob_sha
        || git('rev-parse', `${candidateSha}:${scopedPath}`) !== addition.result_blob_sha) {
      throw new AuthorizationError('Scope amendment diff, blob, or candidate file does not match');
    }
    // Keep metadata out of the application diff; the exact amended source path remains in it.
    return verifiedAmendedPaths([scopedPath]);
  } catch (error) {
    if (error instanceof AuthorizationError) throw error;
    throw new AuthorizationError('Scope amendment could not be verified');
  }
}

export function candidateScopeDiff(repoRoot, authorizationId, baselineSha, candidateSha) {
  const path = authorizationRecordPath(authorizationId);
  const git = (...args) => execFileSync('git', args, {cwd: repoRoot, encoding: 'utf8', timeout: 20_000}).trim();
  try {
    execFileSync('git', ['merge-base', '--is-ancestor', baselineSha, candidateSha], {cwd: repoRoot, stdio: 'ignore', timeout: 20_000});
    const commits = git('rev-list', '--topo-order', '--reverse', '--ancestry-path', `${baselineSha}..${candidateSha}`).split('\n').filter(Boolean);
    const changes = [];
    for (const commit of commits) {
      const parents = git('rev-list', '--parents', '-n', '1', commit).split(' ').slice(1);
      const output = git('diff-tree', '--root', '--first-parent', '--no-commit-id', '--name-status', '-r', commit);
      for (const line of output.split('\n').filter(Boolean)) {
        const [status, changed] = line.split('\t');
        if (status && changed) changes.push({commit, merge: parents.length > 1, status, path: changed});
      }
    }
    const recordAdds = changes.filter(change => change.path === path && !change.merge && change.status === 'A');
    if (recordAdds.length !== 1 || changes.some(change => change.path === path && change.status !== 'A' && !change.merge)) {
      throw new AuthorizationError('Durable authorization record must be added once and remain unchanged');
    }
    const authorizationCommit = recordAdds[0].commit;
    const recordObject = git('rev-parse', `${authorizationCommit}:${path}`);
    if (git('rev-parse', `${candidateSha}:${path}`) !== recordObject) {
      throw new AuthorizationError('Durable authorization record changed after it was added');
    }
    const amendedPaths = candidateScopeAmendments(repoRoot, authorizationId, baselineSha, candidateSha);
    const amendmentPaths = git('diff', '--name-only', baselineSha, candidateSha, '--', amendmentDirectory(authorizationId))
      .split('\n').filter(Boolean);
    const scopeChanges = changes.filter(change => change.path !== path);
    const authorizationIndex = commits.indexOf(authorizationCommit);
    const preparedBeforeAuthorization = scopeChanges.filter(change => commits.indexOf(change.commit) < authorizationIndex);
    let record;
    try { record = JSON.parse(git('show', `${candidateSha}:${path}`)); }
    catch { throw new AuthorizationError('Durable authorization record is unreadable'); }
    const actualPreparedCommits = [...new Set(preparedBeforeAuthorization.map(change => change.commit))].sort();
    const actualPreparedPaths = [...new Set(preparedBeforeAuthorization.map(change => change.path))].sort();
    const declaredPreparedCommits = Array.isArray(record.preauthorized_commits) ? [...record.preauthorized_commits].sort() : [];
    const declaredPreparedPaths = Array.isArray(record.preauthorized_paths) ? [...record.preauthorized_paths].sort() : [];
    if (preparedBeforeAuthorization.some(change => change.merge)
        || JSON.stringify(actualPreparedCommits) !== JSON.stringify(declaredPreparedCommits)
        || JSON.stringify(actualPreparedPaths) !== JSON.stringify(declaredPreparedPaths)
        || actualPreparedPaths.some(name => !Array.isArray(record.allowed_paths)
          || !inScope(name, record.allowed_paths))) {
      throw new AuthorizationError('Pre-authorization candidate history is not exactly listed in the release task record');
    }
    const authorizationPrecedes = commit => {
      try { execFileSync('git', ['merge-base', '--is-ancestor', authorizationCommit, commit], {cwd: repoRoot, stdio: 'ignore', timeout: 20_000}); return true; }
      catch { return false; }
    };
    if (!scopeChanges.length || scopeChanges.some(change => change.commit === authorizationCommit)
        || scopeChanges.filter(change => commits.indexOf(change.commit) > authorizationIndex)
          .some(change => !authorizationPrecedes(change.commit))) {
      throw new AuthorizationError('Durable authorization record must precede every implementation change');
    }
    const diffPaths = git('diff', '--name-only', baselineSha, candidateSha).split('\n').filter(Boolean)
      .filter(name => name !== path && !amendmentPaths.includes(name));
    if (!diffPaths.length) throw new AuthorizationError('Candidate has no implementation changes beyond the authorization record');
    if (amendedPaths.some(name => !diffPaths.includes(name))) throw new AuthorizationError('Scope amendment does not reference a candidate diff path');
    const result = Object.freeze([...new Set(diffPaths)].sort());
    VERIFIED_CANDIDATE_AMENDMENTS.set(result, {authorizationId, baselineSha, candidateSha,
      amendedPaths, changedPathsJson: JSON.stringify(result)});
    return result;
  } catch (error) {
    if (error instanceof AuthorizationError) throw error;
    throw new AuthorizationError('Candidate diff from production baseline could not be proven');
  }
}

const inScope = (path, allowed) => allowed.some(scope => path === scope.replace(/\/$/, '') || (scope.endsWith('/') && path.startsWith(scope)));
const ownerOnly = path => OWNER_ONLY_PREFIXES.some(prefix => path.toLowerCase() === prefix.replace(/\/$/, '').toLowerCase() || path.toLowerCase().startsWith(prefix.toLowerCase()));
const migrationPath = value => /(?:^|\/)(?:migrations|prisma\/migrations)\//i.test(value);
const releaseInfrastructurePath = value => {
  const normalized = value.toLowerCase();
  if (normalized.startsWith('.github/') || normalized === 'docs/release/authorization.md'
      || normalized === 'docs/release_authorization.md' || normalized.startsWith('docs/release/')) return true;
  if (!normalized.startsWith('scripts/')) return false;
  return /(?:release|prepare|ghcr|oci|artifact|coalesc|worker.*guard)/.test(normalized.slice('scripts/'.length));
};

function validateReleaseInfrastructureRiskContract(record, changedPath) {
  const contract = record.risk_contracts?.release_infrastructure;
  const requiredOperations = ['integrate', 'coalesce', 'prepare', 'preflight', 'deploy', 'verify', 'rollback', 'publish_evidence'];
  if (!contract || !Array.isArray(contract.allowed_paths) || !contract.allowed_paths.includes(changedPath)
      || !Array.isArray(record.allowed_paths) || !record.allowed_paths.some(scope => inScope(changedPath, [scope]))
      || contract.baseline_sha !== record.production_baseline_sha
      || typeof contract.risk_description !== 'string' || contract.risk_description.trim().length < 20
      || !Array.isArray(contract.allowed_operations) || requiredOperations.some(operation => !contract.allowed_operations.includes(operation))
      || !Array.isArray(contract.required_gates) || RELEASE_INFRASTRUCTURE_GATES.some(gate => !contract.required_gates.includes(gate))
      || typeof contract.backup_plan !== 'string' || contract.backup_plan.trim().length < 20
      || typeof contract.rollback_plan !== 'string' || contract.rollback_plan.trim().length < 20
      || !Array.isArray(contract.stop_conditions) || contract.stop_conditions.length < 2
      || contract.stop_conditions.some(value => typeof value !== 'string' || value.trim().length < 8)) {
    throw new AuthorizationError('Candidate touches release infrastructure without a complete pre-approved release-infrastructure risk contract');
  }
}

function validateMigrationRiskContract(record, changedPath) {
  const contract = record.risk_contracts?.database_migration;
  const requiredGates = ['migration_preflight', 'backup_before_migration', 'rollback_readiness', 'tenant_isolation', 'postdeploy_health'];
  if (!migrationPath(changedPath) || !contract || !Array.isArray(contract.allowed_paths)
      || !contract.allowed_paths.includes(changedPath) || !Array.isArray(record.allowed_paths)
      || !record.allowed_paths.some(scope => inScope(changedPath, [scope]))
      || !MIGRATION_TARGET.test(contract.prisma_target ?? '') || !MIGRATION_TARGET.test(contract.agata_target ?? '')
      || !/^[a-f0-9]{64}$/.test(contract.gsales_migration_sha256 ?? '')
      || !MIGRATION_TARGET.test(contract.production_prisma_head ?? '')
      || !MIGRATION_TARGET.test(contract.production_agata_head ?? '')
      || !MIGRATION_TARGET.test(contract.production_gsales_head ?? '')
      || !/^[a-f0-9]{64}$/.test(contract.production_gsales_migration_sha256 ?? '')
      || typeof contract.risk_description !== 'string' || contract.risk_description.trim().length < 20
      || typeof contract.backup_plan !== 'string' || contract.backup_plan.trim().length < 20
      || typeof contract.rollback_plan !== 'string' || contract.rollback_plan.trim().length < 20
      || !Array.isArray(contract.stop_conditions) || contract.stop_conditions.length < 2
      || contract.stop_conditions.some(value => typeof value !== 'string' || value.trim().length < 8)
      || !Array.isArray(contract.required_gates) || requiredGates.some(gate => !contract.required_gates.includes(gate))) {
    throw new AuthorizationError('Candidate touches owner-only migration path without a complete pre-approved database risk contract');
  }
}

export function assertNoTaskScopedMigrations({manifest, prismaHead, agataHead,
  gsalesMigrationSha256, record, changedPaths = []}) {
  const changedMigrationPaths = changedPaths.filter(migrationPath);
  const currentHeadsMatch = manifest && manifest.dbHeadAfter === null
    && manifest.dbHeadBefore === prismaHead && manifest.agataDbHeadBefore === agataHead
    && manifest.agataDbHeadBefore === manifest.liveFacts?.LIVE_AGATA_HEAD
    && manifest.gsalesDbHeadBefore === manifest.liveFacts?.LIVE_GSALES_HEAD
    && manifest.gsalesMigrationSha256Before === manifest.liveFacts?.LIVE_GSALES_MIGRATION_SHA256
    && manifest.liveFacts?.LIVE_GSALES_MIGRATION_SHA256 === gsalesMigrationSha256;
  if (!changedMigrationPaths.length && currentHeadsMatch) return {state: 'NO_MIGRATION_CHANGE'};
  const contract = record?.risk_contracts?.database_migration;
  if (!changedMigrationPaths.length || changedMigrationPaths.some(path => !contract?.allowed_paths?.includes(path))) {
    throw new AuthorizationError('TASK_SCOPED_AUTO cannot apply database migrations without the exact pre-approved risk contract');
  }
  const candidateGsales = record?.risk_contracts?.database_migration?.gsales_migration_sha256;
  const baselineGsales = manifest?.gsalesMigrationSha256Before ?? manifest?.liveFacts?.LIVE_GSALES_MIGRATION_SHA256;
  if (!manifest || manifest.status !== 'PLANNED' || manifest.rollbackSha !== manifest.previousProductionSha
      || manifest.liveFacts?.LIVE_ROLLBACK_IMAGE_AVAILABLE !== 'YES'
      || contract.prisma_target !== prismaHead || contract.agata_target !== agataHead
      || candidateGsales !== gsalesMigrationSha256
      || contract.production_prisma_head !== manifest.dbHeadBefore
      || contract.production_agata_head !== manifest.agataDbHeadBefore
      || contract.production_gsales_head !== manifest.gsalesDbHeadBefore
      || contract.production_gsales_migration_sha256 !== baselineGsales
      || (contract.agata_target !== contract.production_agata_head
        && manifest.agataMigrationReadiness?.readiness !== 'PASS')) {
    throw new AuthorizationError('Preflight migration targets, production baseline, rollback or readiness differ from the pre-approved risk contract');
  }
  return {state: 'PREAPPROVED_MIGRATION', target: contract};
}

export function validateTaskAuthorization({record, project, authorizationId, candidateSha,
  currentMainSha, productionSha, changedPaths}) {
  if (!Array.isArray(changedPaths)) throw new AuthorizationError('Candidate changed paths are required');
  const scopeProof = VERIFIED_CANDIDATE_AMENDMENTS.get(changedPaths);
  if (scopeProof && JSON.stringify([...new Set(changedPaths)].sort()) !== scopeProof.changedPathsJson) {
    throw new AuthorizationError('Candidate changed paths differ from the verified scope proof');
  }
  const effectiveAmendedPaths = scopeProof && scopeProof.authorizationId === authorizationId
      && scopeProof.candidateSha === candidateSha && scopeProof.baselineSha === record?.production_baseline_sha
      && productionSha === scopeProof.baselineSha ? scopeProof.amendedPaths : [];
  if (!AUTH_ID.test(authorizationId ?? '')) throw new AuthorizationError('Invalid task authorization ID');
  if (!record || record.schema_version !== 3) throw new AuthorizationError('Unsupported task authorization record');
  if (record.owner_instruction_scope !== 'FULL_PRODUCTION_CYCLE') throw new AuthorizationError('Original owner instruction must explicitly authorize the full production cycle');
  if (record.authorization_id !== authorizationId) throw new AuthorizationError('authorization ID does not match durable record');
  if (record.project !== project || record.authorization_mode !== 'TASK_SCOPED_AUTO') throw new AuthorizationError('Authorization project or mode does not match');
  if (record.environment !== 'production') throw new AuthorizationError('Task authorization is not bound to the production environment');
  if (record.state !== 'ACTIVE' || record.revoked !== false || record.consumed !== false) throw new AuthorizationError('Task authorization is revoked, consumed, or inactive');
  const sourceInstruction = verifyOwnerTaskRecord(record);
  if (record.selected_candidate_sha !== undefined) throw new AuthorizationError('Durable authorization must not pin a candidate SHA before integration');
  if (Object.values(record.risk_contracts ?? {}).some(contract => contract && typeof contract === 'object' && 'selected_candidate_sha' in contract)) throw new AuthorizationError('Durable authorization risk contracts must not pin a stale candidate SHA');
  if (record.candidate_selection_rule !== 'EXACT_CURRENT_MAIN_WITHIN_AUTHORIZED_SCOPE') throw new AuthorizationError('Unsupported candidate selection rule');
  if (typeof record.owner_task_reference !== 'string' || record.owner_task_reference.trim().length < 20) throw new AuthorizationError('Original OWNER release task reference is required');
  if (typeof record.task !== 'string' || !record.task.trim()) throw new AuthorizationError('Task description is required');
  if (!Array.isArray(record.acceptance_criteria) || !record.acceptance_criteria.length || record.acceptance_criteria.some(value => typeof value !== 'string' || !value.trim())) throw new AuthorizationError('Acceptance criteria are required');
  if (!Array.isArray(record.allowed_operations) || !['integrate', 'coalesce', 'prepare', 'preflight', 'deploy', 'verify', 'rollback', 'publish_evidence'].every(value => record.allowed_operations.includes(value))) throw new AuthorizationError('Full release operations are not authorized by the OWNER task');
  if (!Array.isArray(record.exclusions) || [...REQUIRED_EXCLUSIONS].some(value => !record.exclusions.includes(value))) throw new AuthorizationError('Required owner-only exclusions are missing');
  if (record.risk_contracts?.release_infrastructure && record.exclusions.includes('release_infrastructure')) throw new AuthorizationError('Release infrastructure scope conflicts with its exclusion');
  if (!record.risk_contracts?.release_infrastructure && !record.exclusions.includes('release_infrastructure')) throw new AuthorizationError('Release infrastructure must be excluded without its risk contract');
  if (!Array.isArray(record.allowed_paths) || !record.allowed_paths.length || record.allowed_paths.some(value => typeof value !== 'string' || !value.trim() || value.startsWith('/') || value.split('/').includes('..'))) throw new AuthorizationError('A safe non-empty allowed-path scope is required');
  if (!SHA.test(record.production_baseline_sha ?? '')) throw new AuthorizationError('Exact production baseline SHA is required');
  if (!SHA.test(candidateSha ?? '') || candidateSha !== currentMainSha) throw new AuthorizationError('Candidate must be the exact current main SHA');
  if (record.production_baseline_sha !== productionSha) throw new AuthorizationError('Live production baseline drifted from the task authorization');
  if (!Array.isArray(changedPaths) || !changedPaths.length) throw new AuthorizationError('Candidate diff from production baseline is empty or unavailable');
  for (const path of changedPaths) {
    if (typeof path !== 'string' || !path || path.startsWith('/') || path.split('/').includes('..')) throw new AuthorizationError('Candidate diff contains an invalid path');
    if (migrationPath(path)) {
      validateMigrationRiskContract(record, path);
      continue;
    }
    if (releaseInfrastructurePath(path)) {
      validateReleaseInfrastructureRiskContract(record, path);
      continue;
    }
    if (ownerOnly(path)) {
      throw new AuthorizationError('Candidate path is owner-only and cannot use TASK_SCOPED_AUTO');
    }
    if (!inScope(path, record.allowed_paths) && !effectiveAmendedPaths.includes(path)) {
      throw new AuthorizationError(`Candidate path is outside authorized scope: ${path}`);
    }
  }
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(record).sort(([a], [b]) => a.localeCompare(b))));
  return {authorizationId, candidateSha, productionBaselineSha: record.production_baseline_sha,
    sourceInstructionIdentifier: sourceInstruction.sourceInstructionIdentifier,
    ownerTaskCommitmentSha256: sourceInstruction.commitmentSha256,
    recordSha256: crypto.createHash('sha256').update(canonical).digest('hex'), changedPaths: [...new Set(changedPaths)].sort().join(',')};
}

export async function checkTaskAuthorizationState({project, authorizationId, candidateSha, token, fetchImpl = fetch}) {
  if (!token) throw new AuthorizationError('GitHub token is required to check task authorization state');
  if (candidateSha && !SHA.test(candidateSha)) throw new AuthorizationError('Invalid exact candidate SHA');
  const states = {};
  for (const state of ['revoked', 'consumed', 'claimed']) {
    const route = authorizationStateRef(project, authorizationId, state).replace(/^refs\/tags\//, 'git/ref/tags/');
    const response = await fetchImpl(`https://api.github.com/repos/akhripunov/${project}/${route}`, {
      headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
    });
    if (response.status === 404) { states[state] = false; continue; }
    if (!response.ok) throw new AuthorizationError(`GitHub authorization state read failed (HTTP ${response.status})`);
    const data = await response.json();
    states[state] = true;
    if (state === 'claimed') {
      if (data.object?.type !== 'commit' || !SHA.test(data.object?.sha ?? '')) {
        throw new AuthorizationError('Task authorization claim is not a valid commit reference');
      }
      states.claimedSha = data.object.sha;
    }
  }
  if (states.revoked || states.consumed) throw new AuthorizationError('Task authorization is revoked or already consumed');
  if (candidateSha && !states.claimed) {
    throw new AuthorizationError('Task authorization has no active release claim');
  }
  return states;
}

export async function consumeTaskAuthorization({project, authorizationId, candidateSha, token, fetchImpl = fetch}) {
  if (!token || !SHA.test(candidateSha ?? '')) throw new AuthorizationError('Exact candidate and GitHub token are required to consume authorization');
  const markerUrl = `https://api.github.com/repos/akhripunov/${project}/git/ref/tags/${project}-task-auth/${authorizationId}/consumed`;
  const readMarker = async () => {
    const checked = await fetchImpl(markerUrl, {headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}});
    const data = checked.ok ? await checked.json() : {};
    return data.object?.sha === candidateSha;
  };
  let response;
  try {
    response = await fetchImpl(`https://api.github.com/repos/akhripunov/${project}/git/refs`, {
      method: 'POST', headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'},
      body: JSON.stringify({ref: authorizationStateRef(project, authorizationId, 'consumed'), sha: candidateSha}),
    });
  } catch {
    if (await readMarker()) return;
    throw new AuthorizationError('Task authorization consumption response was lost and exact marker is absent');
  }
  if (response.status === 422) {
    if (await readMarker()) return;
  }
  if (!response.ok) throw new AuthorizationError(`Task authorization consumption failed (HTTP ${response.status})`);
}
