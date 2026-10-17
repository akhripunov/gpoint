import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const LEDGER_REF = 'refs/heads/gpoint-release-ledger';
const SHA = /^[0-9a-f]{40}$/;
const MANIFEST = /^gpoint-[a-zA-Z0-9_.-]+\.json$/;

function ledgerHead(git) {
  const ref = git(['ls-remote', 'origin', LEDGER_REF]).trim();
  if (!ref) return null; // Initial adoption uses the last recorded main baseline.
  const [sha, name] = ref.split(/\s+/);
  if (!SHA.test(sha) || name !== LEDGER_REF) throw new Error('Invalid release ledger ref');
  git(['fetch', '--quiet', 'origin', LEDGER_REF]);
  if (git(['rev-parse', 'FETCH_HEAD']) !== sha) throw new Error('Release ledger moved during read; retry required');
  return sha;
}

function stableJson(value) {
  if (Array.isArray(value)) return value.map(stableJson);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, stableJson(value[key])]));
  return value;
}

/** Read back an exact published ledger state and verify every local manifest blob. */
export function verifyReleaseBookkeeping({ git, stateFile, manifestDir, expectedState }) {
  if (!stateFile || !manifestDir) throw new Error('External release evidence paths are required');
  const headSha = ledgerHead(git);
  if (!headSha) throw new Error('Release ledger missing after publication');
  const remoteState = JSON.parse(git(['show', `${headSha}:docs/release/state/production.json`]));
  const localState = expectedState ?? JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  if (JSON.stringify(stableJson(remoteState)) !== JSON.stringify(stableJson(localState)))
    throw new Error('Release ledger state read-back differs from the expected state');
  const remoteFiles = new Map();
  for (const line of git(['ls-tree', '-r', headSha, 'docs/release/manifests']).split('\n').filter(Boolean)) {
    const match = /^100644 blob ([0-9a-f]{40})\t(docs\/release\/manifests\/(.+))$/.exec(line);
    if (!match || !MANIFEST.test(match[3])) throw new Error('Invalid release manifest in ledger read-back');
    remoteFiles.set(match[3], match[1]);
  }
  const localNames = fs.readdirSync(manifestDir).filter(name => MANIFEST.test(name));
  for (const name of localNames) {
    const localBlob = git(['hash-object', '--stdin'], fs.readFileSync(path.join(manifestDir, name), 'utf8'));
    if (remoteFiles.get(name) !== localBlob) throw new Error(`Release manifest read-back mismatch: ${name}`);
  }
  if (remoteFiles.size !== localNames.length) throw new Error('Release ledger read-back omitted or added manifests');
  return {headSha};
}

export function loadReleaseBookkeeping({ root, git, allowMissing = false, destination = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-ledger-')) }) {
  const stateFile = path.join(destination, 'state/production.json');
  const manifestDir = path.join(destination, 'manifests');
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.mkdirSync(manifestDir, { recursive: true });
  fs.copyFileSync(path.join(root, 'docs/release/state/production.json'), stateFile);
  for (const name of fs.readdirSync(path.join(root, 'docs/release/manifests')).filter(n => MANIFEST.test(n))) {
    fs.copyFileSync(path.join(root, 'docs/release/manifests', name), path.join(manifestDir, name));
  }
  const ledgerSha = ledgerHead(git);
  if (!ledgerSha && !allowMissing) throw new Error("Release ledger missing; explicit initial adoption required");
  if (ledgerSha) {
    const state = git(['show', `${ledgerSha}:docs/release/state/production.json`]);
    const parsed = JSON.parse(state);
    if (!SHA.test(parsed.currentProductionSha ?? '')) throw new Error('Invalid ledger production SHA');
    fs.writeFileSync(stateFile, state + '\n');
    for (const file of git(['ls-tree', '-r', '--name-only', ledgerSha, 'docs/release/manifests']).split('\n').filter(Boolean)) {
      const name = file.replace('docs/release/manifests/', '');
      if (!MANIFEST.test(name) || file !== `docs/release/manifests/${name}`) throw new Error('Invalid ledger manifest path');
      const content = git(['show', `${ledgerSha}:${file}`]);
      JSON.parse(content);
      // Preserve the exact historical blob so later ledger commits can prove
      // manifests are append-only instead of silently normalizing their bytes.
      fs.writeFileSync(path.join(manifestDir, name), content);
    }
  }
  return { stateFile, manifestDir, ledgerSha, destination };
}

export function persistReleaseBookkeeping({ mode, records, evidence, git, stateFile, manifestDir, ledgerSha: expectedParentSha }) {
  if (!['production', 'task-scoped-auto'].includes(mode) || !records.length) return 'NOT_REQUIRED';
  if (['BLOCKED', 'FAILED_PRE_SWITCH'].includes(evidence.status)) return 'ARTIFACT_AND_LINEAR_ONLY';
  if (!stateFile || !manifestDir) throw new Error('External release evidence paths are required');
  if (!SHA.test(evidence.candidateSha ?? '') || records.some(r => !MANIFEST.test(r.name))) throw new Error('Invalid release ledger evidence');
  const parent = ledgerHead(git);
  if (parent !== expectedParentSha) throw new Error('Release ledger changed since its verified read; refusing stale-state publication');
  const entries = new Map();
  if (parent) {
    for (const line of git(['ls-tree', `${parent}:docs/release/manifests`]).split('\n').filter(Boolean)) {
      const match = /^100644 blob ([0-9a-f]{40})\t(.+)$/.exec(line);
      if (!match || !MANIFEST.test(match[2])) throw new Error('Invalid existing ledger manifest');
      entries.set(match[2], match[1]);
    }
  }
  // Retain every historical record, including recovery evidence, outside main.
  for (const name of fs.readdirSync(manifestDir).filter(n => MANIFEST.test(n))) {
    const blob = git(['hash-object', '-w', '--stdin'], fs.readFileSync(path.join(manifestDir, name), 'utf8'));
    if (entries.has(name) && entries.get(name) !== blob) throw new Error(`Existing release manifest is immutable: ${name}`);
    entries.set(name, blob);
  }
  const tree = lines => git(['mktree'], lines.join('\n') + '\n');
  const manifests = tree([...entries].sort().map(([name, sha]) => `100644 blob ${sha}\t${name}`));
  const state = git(['hash-object', '-w', '--stdin'], fs.readFileSync(stateFile, 'utf8'));
  const stateTree = tree([`100644 blob ${state}\tproduction.json`]);
  const releaseTree = tree([`040000 tree ${manifests}\tmanifests`, `040000 tree ${stateTree}\tstate`]);
  const docsTree = tree([`040000 tree ${releaseTree}\trelease`]);
  const ledgerTree = tree([`040000 tree ${docsTree}\tdocs`]);
  const commit = git(['-c', 'user.name=GPoint Release', '-c', 'user.email=gpoint-release@users.noreply.github.com',
    'commit-tree', ledgerTree, ...(parent ? ['-p', parent] : []), '-m', `Record GPoint release ${evidence.candidateSha}`]);
  // No force push: a concurrent writer or publication failure stays visible.
  git(['push', 'origin', `${commit}:${LEDGER_REF}`]);
  const published = verifyReleaseBookkeeping({git, stateFile, manifestDir});
  if (published.headSha !== commit) throw new Error('Release ledger moved after publication; outcome requires reconciliation');
  return 'PERSISTED_TO_LEDGER';
}
