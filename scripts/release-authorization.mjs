/** Validate durable, one-shot TASK_SCOPED_AUTO authorization records. */
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';

const SHA = /^[a-f0-9]{40}$/;
const MIGRATION_TARGET = /^[A-Za-z0-9_.-]{1,128}$/;
const AUTH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/;
const REQUIRED_EXCLUSIONS = new Set(['database', 'migrations', 'security', 'secrets', 'payments', 'ads', 'paid_traffic', 'release_infrastructure']);
const OWNER_ONLY_PREFIXES = [
  '.github/', 'packages/db/', 'migrations/', 'db/', 'deploy/', 'docker/',
  'docs/release/', 'scripts/release', 'scripts/prepare-release', 'scripts/select-release-run',
  'scripts/publish-release', 'services/agata/', 'services/core/', 'services/core-contracts/',
  'services/gpoint-ads/', 'services/ads/', 'services/traffic/', 'services/payments/', 'services/gsales/migrations/',
  'payments/', 'billing/', '.env', 'secrets/',
];

export class AuthorizationError extends Error {}

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
    const scopeChanges = changes.filter(change => change.path !== path);
    const authorizationPrecedes = commit => {
      try { execFileSync('git', ['merge-base', '--is-ancestor', authorizationCommit, commit], {cwd: repoRoot, stdio: 'ignore', timeout: 20_000}); return true; }
      catch { return false; }
    };
    if (!scopeChanges.length || scopeChanges.some(change => change.commit === authorizationCommit
      || !authorizationPrecedes(change.commit))) {
      throw new AuthorizationError('Durable authorization record must precede every implementation change');
    }
    const diffPaths = git('diff', '--name-only', baselineSha, candidateSha).split('\n').filter(Boolean).filter(name => name !== path);
    if (!diffPaths.length) throw new AuthorizationError('Candidate has no implementation changes beyond the authorization record');
    return [...new Set(diffPaths)].sort();
  } catch (error) {
    if (error instanceof AuthorizationError) throw error;
    throw new AuthorizationError('Candidate diff from production baseline could not be proven');
  }
}

const inScope = (path, allowed) => allowed.some(scope => path === scope.replace(/\/$/, '') || (scope.endsWith('/') && path.startsWith(scope)));
const ownerOnly = path => OWNER_ONLY_PREFIXES.some(prefix => path.toLowerCase() === prefix.replace(/\/$/, '').toLowerCase() || path.toLowerCase().startsWith(prefix.toLowerCase()));
const migrationPath = value => /(?:^|\/)(?:migrations|prisma\/migrations)\//i.test(value);

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
  if (!AUTH_ID.test(authorizationId ?? '')) throw new AuthorizationError('Invalid task authorization ID');
  if (!record || record.schema_version !== 3) throw new AuthorizationError('Unsupported task authorization record');
  if (record.owner_instruction_scope !== 'FULL_PRODUCTION_CYCLE') throw new AuthorizationError('Original owner instruction must explicitly authorize the full production cycle');
  if (record.authorization_id !== authorizationId) throw new AuthorizationError('authorization ID does not match durable record');
  if (record.project !== project || record.authorization_mode !== 'TASK_SCOPED_AUTO') throw new AuthorizationError('Authorization project or mode does not match');
  if (record.state !== 'ACTIVE' || record.revoked !== false || record.consumed !== false) throw new AuthorizationError('Task authorization is revoked, consumed, or inactive');
  if (typeof record.source_reference !== 'string' || !/^(linear:|https:\/\/linear\.app\/)/i.test(record.source_reference.trim())) throw new AuthorizationError('A verifiable Linear owner-instruction reference is required');
  if (typeof record.task !== 'string' || !record.task.trim()) throw new AuthorizationError('Task description is required');
  if (!Array.isArray(record.acceptance_criteria) || !record.acceptance_criteria.length || record.acceptance_criteria.some(value => typeof value !== 'string' || !value.trim())) throw new AuthorizationError('Acceptance criteria are required');
  if (!Array.isArray(record.exclusions) || [...REQUIRED_EXCLUSIONS].some(value => !record.exclusions.includes(value))) throw new AuthorizationError('Required owner-only exclusions are missing');
  if (!Array.isArray(record.allowed_paths) || !record.allowed_paths.length || record.allowed_paths.some(value => typeof value !== 'string' || !value.trim() || value.startsWith('/') || value.split('/').includes('..'))) throw new AuthorizationError('A safe non-empty allowed-path scope is required');
  if (!SHA.test(record.production_baseline_sha ?? '')) throw new AuthorizationError('Exact production baseline SHA is required');
  if (!SHA.test(candidateSha ?? '') || candidateSha !== currentMainSha) throw new AuthorizationError('Candidate must be the exact current main SHA');
  if (record.production_baseline_sha !== productionSha) throw new AuthorizationError('Live production baseline drifted from the task authorization');
  if (!Array.isArray(changedPaths) || !changedPaths.length) throw new AuthorizationError('Candidate diff from production baseline is empty or unavailable');
  for (const path of changedPaths) {
    if (typeof path !== 'string' || !path || path.startsWith('/') || path.split('/').includes('..')) throw new AuthorizationError('Candidate diff contains an invalid path');
    if (ownerOnly(path)) {
      validateMigrationRiskContract(record, path);
      continue;
    }
    if (!inScope(path, record.allowed_paths)) throw new AuthorizationError(`Candidate path is outside authorized scope: ${path}`);
  }
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(record).sort(([a], [b]) => a.localeCompare(b))));
  return {authorizationId, candidateSha, productionBaselineSha: record.production_baseline_sha,
    sourceReference: record.source_reference,
    recordSha256: crypto.createHash('sha256').update(canonical).digest('hex'), changedPaths: [...new Set(changedPaths)].sort().join(',')};
}

export async function checkTaskAuthorizationState({project, authorizationId, candidateSha, token, fetchImpl = fetch}) {
  if (!token) throw new AuthorizationError('GitHub token is required to check one-shot authorization state');
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
    if (state === 'claimed') states.claimedSha = data.object?.sha ?? '';
  }
  if (states.revoked || states.consumed) throw new AuthorizationError('Task authorization is revoked or already consumed');
  if (candidateSha && (!states.claimed || states.claimedSha !== candidateSha)) {
    throw new AuthorizationError('Task authorization claim is absent or belongs to another candidate');
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
