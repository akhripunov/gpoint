// Read literal Alembic revision metadata without executing application code.
// Complex/branched graphs fail closed and require an explicit tooling change.
export function migrationHead(sources) {
  const revisions = new Map();
  for (const source of sources) {
    const revision = source.match(/^revision(?:\s*:\s*[^=\n]+)?\s*=\s*["']([A-Za-z0-9_]{1,32})["']\s*$/m)?.[1];
    const parentMatch = source.match(/^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(None|["'][A-Za-z0-9_]{1,32}["'])\s*$/m);
    if (!revision || !parentMatch || revisions.has(revision)) throw new Error('unsupported Alembic revision metadata');
    revisions.set(revision, parentMatch[1] === 'None' ? null : parentMatch[1].slice(1, -1));
  }
  const parents = new Set([...revisions.values()].filter(Boolean));
  if ([...parents].some(parent => !revisions.has(parent))) throw new Error('missing Alembic ancestor');
  const heads = [...revisions.keys()].filter(revision => !parents.has(revision));
  if (heads.length !== 1) throw new Error('Alembic must have one candidate head');
  const seen = new Set();
  let current = heads[0];
  while (current) {
    if (seen.has(current)) throw new Error('Alembic revision cycle');
    seen.add(current); current = revisions.get(current);
  }
  if (seen.size !== revisions.size) throw new Error('disconnected Alembic revisions');
  return { head: heads[0], revisions: [...revisions.keys()] };
}
