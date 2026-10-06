import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export const GIB = 1024 ** 3;
export const MIN_FREE_BYTES = 10 * GIB;
export const EXPECTED_RUNNER_NAME = 'gpoint-owner-mac';

export function checkRunnerReadiness({env = process.env,
  freeBytes = () => { const stats = fs.statfsSync(env.HOME); return stats.bavail * stats.bsize; },
  command = args => spawnSync(args[0], args.slice(1), {encoding: 'utf8', timeout: 15_000})} = {}) {
  if (env.RUNNER_NAME !== EXPECTED_RUNNER_NAME) throw new Error('runner_name_mismatch');
  if (env.RUNNER_OS !== 'macOS') throw new Error('runner_os_mismatch');
  if (env.RUNNER_ARCH !== 'ARM64') throw new Error('runner_arch_mismatch');
  let docker;
  try { docker = command(['docker', 'info', '--format', '{{.ServerVersion}}']); }
  catch { throw new Error('docker_unavailable'); }
  if (docker.status !== 0 || !String(docker.stdout ?? '').trim()) throw new Error('docker_unavailable');

  const home = path.resolve(env.HOME ?? '');
  if (!env.HOME || !path.isAbsolute(env.HOME)) throw new Error('home_unavailable');
  const cache = path.join(home, '.cache', 'gpoint-release', 'artifacts');
  try {
    fs.mkdirSync(cache, {recursive: true, mode: 0o700});
    const resolvedHome = fs.realpathSync(home);
    const resolved = fs.realpathSync(cache);
    if (!resolved.startsWith(`${resolvedHome}${path.sep}`)) throw new Error('cache_path_escapes_home');
    const probe = path.join(resolved, `.readiness-${process.pid}-${Date.now()}`);
    fs.writeFileSync(probe, 'ready', {flag: 'wx', mode: 0o600});
    fs.unlinkSync(probe);
  } catch (error) {
    if (error.message === 'cache_path_escapes_home') throw error;
    throw new Error('cache_unavailable');
  }
  let bytes;
  try { bytes = typeof freeBytes === 'function' ? freeBytes() : freeBytes; }
  catch { throw new Error('disk_unavailable'); }
  if (!Number.isFinite(bytes) || bytes < MIN_FREE_BYTES) throw new Error('disk_below_hard_minimum');
  return {RUNNER_READY: 'YES', DOCKER_READY: 'YES', DISK_READY: 'YES',
    RUNNER_NAME: env.RUNNER_NAME, RUNNER_ARCH: env.RUNNER_ARCH,
    DOCKER_VERSION: String(docker.stdout).trim(), FREE_BYTES: String(bytes), CACHE_PATH: cache};
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  try {
    const result = checkRunnerReadiness();
    console.log(Object.entries(result).map(([key, value]) => `${key}=${value}`).join(' '));
  } catch (error) {
    console.error(`RUNNER_READY=NO REASON=${error.message}`);
    process.exitCode = 1;
  }
}
