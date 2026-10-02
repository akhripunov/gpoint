// A safe pre-switch failure must not invalidate the prepared application SHA.
// Unknown/partially mutated outcomes still persist recovery state and fail closed.
export function shouldPersistReleaseToMain(status) {
  return !['BLOCKED', 'FAILED_PRE_SWITCH'].includes(status);
}

export function persistReleaseBookkeeping({ mode, records, evidence, git }) {
  if (mode !== 'production' || !records.length) return 'NOT_REQUIRED';
  if (!shouldPersistReleaseToMain(evidence.status)) return 'ARTIFACT_AND_LINEAR_ONLY';
  git(['config', 'user.name', 'GPoint Release']);
  git(['config', 'user.email', 'gpoint-release@users.noreply.github.com']);
  git(['add', 'docs/release/state/production.json', ...records.map(r => `docs/release/manifests/${r.name}`)]);
  git(['commit', '-m', `Record GPoint release ${evidence.candidateSha} [skip ci]`]);
  git(['push', 'origin', 'HEAD:main']);
  return 'PERSISTED_TO_MAIN';
}
