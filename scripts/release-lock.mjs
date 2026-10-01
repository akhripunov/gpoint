import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export function acquireReleaseLock(file, releaseId, legacyFile) {
  if (legacyFile && legacyFile !== file && fs.existsSync(legacyFile)) {
    throw new Error(`BLOCKED: legacy release lock requires reconciliation: ${legacyFile}`);
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const owner = crypto.randomUUID();
  const data = { releaseId, owner, pid: process.pid, startedAt: new Date().toISOString() };
  try { fs.writeFileSync(file, JSON.stringify(data) + '\n', { flag: 'wx', mode: 0o600 }); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`BLOCKED: release lock exists; reconcile before removal: ${file}`);
    throw error;
  }
  return () => {
    const current = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (current.owner !== owner) throw new Error('BLOCKED: release lock owner changed; refusing to remove it');
    fs.unlinkSync(file);
  };
}
