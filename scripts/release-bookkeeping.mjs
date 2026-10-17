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
      fs.writeFileSync(path.join(manifestDir, name), content + '\n');
    }
  }
  return { stateFile, manifestDir, ledgerSha, destination };
}

export function persistReleaseBookkeeping({ mode, records, evidence, git, stateFile, manifestDir }) {
  if (!['production', 'task-scoped-auto'].includes(mode) || !records.length) return 'NOT_REQUIRED';
  if (['BLOCKED', 'FAILED_PRE_SWITCH'].includes(evidence.status)) return 'ARTIFACT_AND_LINEAR_ONLY';
  if (!stateFile || !manifestDir) throw new Error('External release evidence paths are required');
  if (!SHA.test(evidence.candidateSha ?? '') || records.some(r => !MANIFEST.test(r.name))) throw new Error('Invalid release ledger evidence');
  const parent = ledgerHead(git);
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
    entries.set(name, git(['hash-object', '-w', '--stdin'], fs.readFileSync(path.join(manifestDir, name), 'utf8')));
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
  return 'PERSISTED_TO_LEDGER';
}
