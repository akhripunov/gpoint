/** Validate durable, one-shot TASK_SCOPED_AUTO authorization records. */
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';

const SHA = /^[a-f0-9]{40}$/;
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

export function assertNoTaskScopedMigrations({manifest, prismaHead, agataHead, gsalesMigrationSha256}) {
  if (!manifest || manifest.dbHeadBefore !== prismaHead || manifest.agataDbHeadBefore !== agataHead
    || manifest.gsalesDbHeadBefore !== '001_gsales'
    || manifest.liveFacts?.LIVE_GSALES_MIGRATION_SHA256 !== gsalesMigrationSha256) {
    throw new AuthorizationError('TASK_SCOPED_AUTO cannot apply or reconcile database migrations');
  }
}

export function authorizationStateRef(project, authorizationId, state) {
  if (!['maxibot', 'gpoint'].includes(String(project).toLowerCase()) || !AUTH_ID.test(authorizationId)) {
    throw new AuthorizationError('Invalid project-scoped authorization ID');
  }
  if (!['revoked', 'consumed'].includes(state)) throw new AuthorizationError('Invalid authorization state marker');
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
    const commits = git('rev-list', '--first-parent', '--reverse', '--ancestry-path', `${baselineSha}..${candidateSha}`).split('\n').filter(Boolean);
    const changes = [];
    commits.forEach((commit, index) => {
      const output = git('diff-tree', '--root', '--first-parent', '--no-commit-id', '--name-status', '-r', commit);
      for (const line of output.split('\n').filter(Boolean)) {
        const [status, changed] = line.split('\t');
        if (status && changed) changes.push({index, status, path: changed});
      }
    });
    const recordChanges = changes.filter(change => change.path === path);
    const scopeChanges = changes.filter(change => change.path !== path);
    if (recordChanges.length !== 1 || recordChanges[0].status !== 'A' || !scopeChanges.length
      || recordChanges[0].index >= Math.min(...scopeChanges.map(change => change.index))) {
      throw new AuthorizationError('Durable authorization record must be added before implementation and remain unchanged');
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

export function validateTaskAuthorization({record, project, authorizationId, candidateSha,
  currentMainSha, productionSha, changedPaths}) {
  if (!AUTH_ID.test(authorizationId ?? '')) throw new AuthorizationError('Invalid task authorization ID');
  if (!record || record.schema_version !== 1) throw new AuthorizationError('Unsupported task authorization record');
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
    if (ownerOnly(path)) throw new AuthorizationError(`Candidate touches owner-only path: ${path}`);
    if (!inScope(path, record.allowed_paths)) throw new AuthorizationError(`Candidate path is outside authorized scope: ${path}`);
  }
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(record).sort(([a], [b]) => a.localeCompare(b))));
  return {authorizationId, candidateSha, productionBaselineSha: record.production_baseline_sha,
    sourceReference: record.source_reference,
    recordSha256: crypto.createHash('sha256').update(canonical).digest('hex'), changedPaths: [...new Set(changedPaths)].sort().join(',')};
}

export async function checkTaskAuthorizationState({project, authorizationId, token, fetchImpl = fetch}) {
  if (!token) throw new AuthorizationError('GitHub token is required to check one-shot authorization state');
  const states = {};
  for (const state of ['revoked', 'consumed']) {
    const route = authorizationStateRef(project, authorizationId, state).replace(/^refs\/tags\//, 'git/ref/tags/');
    const response = await fetchImpl(`https://api.github.com/repos/akhripunov/${project}/${route}`, {
      headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
    });
    if (response.status === 404) { states[state] = false; continue; }
    if (!response.ok) throw new AuthorizationError(`GitHub authorization state read failed (HTTP ${response.status})`);
    states[state] = true;
  }
  if (states.revoked || states.consumed) throw new AuthorizationError('Task authorization is revoked or already consumed');
  return states;
}

export async function consumeTaskAuthorization({project, authorizationId, candidateSha, token, fetchImpl = fetch}) {
  if (!token || !SHA.test(candidateSha ?? '')) throw new AuthorizationError('Exact candidate and GitHub token are required to consume authorization');
  const response = await fetchImpl(`https://api.github.com/repos/akhripunov/${project}/git/refs`, {
    method: 'POST', headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'},
    body: JSON.stringify({ref: authorizationStateRef(project, authorizationId, 'consumed'), sha: candidateSha}),
  });
  if (response.status === 422) {
    const checked = await fetchImpl(`https://api.github.com/repos/akhripunov/${project}/git/ref/tags/${project}-task-auth/${authorizationId}/consumed`, {
      headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
    });
    const data = checked.ok ? await checked.json() : {};
    if (data.object?.sha === candidateSha) return;
  }
  if (!response.ok) throw new AuthorizationError(`Task authorization consumption failed (HTTP ${response.status})`);
}
