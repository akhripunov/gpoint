import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** Keep the full GitHub run inventory out of process environments. */
export function createRecoveryActiveDeploysFile(inventory, tempRoot = os.tmpdir()) {
  const directory = fs.mkdtempSync(path.join(tempRoot, 'gpoint-recovery-active-deploys-'));
  const filePath = path.join(directory, 'inventory.json');
  const serialized = JSON.stringify(inventory);
  try {
    fs.chmodSync(directory, 0o700);
    fs.writeFileSync(filePath, serialized, {flag: 'wx', mode: 0o600});
    fs.chmodSync(filePath, 0o600);
  } catch (error) {
    fs.rmSync(directory, {recursive: true, force: true});
    throw error;
  }
  return {
    filePath,
    byteLength: Buffer.byteLength(serialized),
    cleanup() { fs.rmSync(directory, {recursive: true, force: true}); },
  };
}

export function readRecoveryActiveDeploysFile(filePath) {
  const directory = path.dirname(filePath);
  const directoryStat = fs.lstatSync(directory);
  const fileStat = fs.lstatSync(filePath);
  if (!path.basename(directory).startsWith('gpoint-recovery-active-deploys-')
      || path.basename(filePath) !== 'inventory.json'
      || directoryStat.isSymbolicLink() || !directoryStat.isDirectory() || (directoryStat.mode & 0o077) !== 0
      || fileStat.isSymbolicLink() || !fileStat.isFile() || (fileStat.mode & 0o077) !== 0
      || (typeof process.getuid === 'function' && (directoryStat.uid !== process.getuid() || fileStat.uid !== process.getuid()))
      || fileStat.size > 64 * 1024 * 1024) {
    throw new Error('Recovery active Deploy inventory file is invalid');
  }
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}
