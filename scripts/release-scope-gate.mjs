/** Fail-closed TASK_SCOPED_AUTO decision over the full production baseline diff. */
const SHA_RE = /^[0-9a-f]{40}$/;
const OWNER_ONLY_PREFIXES = [
  '.github/', 'scripts/release', 'scripts/automated-release', 'scripts/select-release-run',
  'docs/RELEASE', 'docs/release', 'prisma/', 'migrations/', 'db/', 'security/',
  'auth/', 'payments/', 'billing/', 'deploy/', 'agata/', 'gsales/',
];
const OWNER_ONLY_NAMES = new Set([
  'Dockerfile', 'docker-compose.yml', 'docker-compose.yaml', 'package.json',
  'pnpm-lock.yaml', 'package-lock.json', 'requirements.txt',
]);

export function evaluateScope({baselineSha, candidateSha, mode, authorizationRef,
  taskScope, authorizedPrefixes, changedPaths, riskAttestation}) {
  const paths = [...new Set(changedPaths ?? [])].sort();
  const owner = reason => ({status: 'OWNER_DECISION_REQUIRED', reason, changedPaths: paths});
  if (!SHA_RE.test(baselineSha ?? '') || !SHA_RE.test(candidateSha ?? '') || baselineSha === candidateSha)
    return owner('invalid_or_empty_baseline_diff');
  if (mode !== 'TASK_SCOPED_AUTO') return owner('exact_sha_or_unknown_authorization_mode');
  if (!authorizationRef?.trim() || !taskScope?.trim() || paths.length === 0)
    return owner('authorization_or_scope_record_missing');
  if (riskAttestation !== 'LOW') return owner('risk_not_explicitly_low');
  const prefixes = (authorizedPrefixes ?? []).filter(value => value?.trim())
    .map(value => `${value.trim().replace(/\/+$/, '')}/`);
  if (!prefixes.length) return owner('authorized_paths_missing');
  for (const path of paths) {
    if (OWNER_ONLY_PREFIXES.some(prefix => path.startsWith(prefix)) || OWNER_ONLY_NAMES.has(path.split('/').at(-1)))
      return owner(`owner_only_path:${path}`);
    if (!prefixes.some(prefix => path.startsWith(prefix))) return owner(`scope_expansion:${path}`);
  }
  return {status: 'AUTO_RELEASE_ALLOWED', reason: 'exact_task_scope_low_risk', changedPaths: paths};
}
