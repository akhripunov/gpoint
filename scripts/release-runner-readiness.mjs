/** Fail-fast checks for the assigned GPoint self-hosted release runner. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

export const EXPECTED_RUNNER = 'gpoint-owner-mac';
export const MIN_FREE_BYTES = 5 * 1024 ** 3;
export class RunnerReadinessError extends Error {}

export function validateRuntime({runnerName, runnerOs, runnerArch, freeBytes, dockerAvailable,
  dockerOs, mainSha, candidateSha, activeConflict, pathsReady}) {
  if (runnerName !== EXPECTED_RUNNER || runnerOs !== 'macOS' || runnerArch !== 'ARM64') throw new RunnerReadinessError('Assigned runner identity/OS/architecture does not match GPoint lane');
  if (freeBytes < MIN_FREE_BYTES) throw new RunnerReadinessError('Insufficient free disk for release bundle/runtime work');
  if (!dockerAvailable || dockerOs !== 'linux') throw new RunnerReadinessError('Required Docker runtime is unavailable');
  if (!pathsReady) throw new RunnerReadinessError('GPoint cache/runtime paths are unavailable or unsafe');
  if (!candidateSha || mainSha !== candidateSha) throw new RunnerReadinessError('Candidate is no longer exact current main');
  if (activeConflict) throw new RunnerReadinessError('Conflicting GPoint production mutation is active or queued');
  return {runner: runnerName, runnerOs, runnerArch, freeBytes, docker: 'READY', network: 'READY', projectPaths: 'READY', conflictingProductionMutation: 'NO'};
}

function safeWritable(directory, home) {
  try {
    fs.mkdirSync(directory, {recursive: true});
    const resolvedHome = fs.realpathSync(home);
    const resolved = fs.realpathSync(directory);
    if (resolved !== resolvedHome && !resolved.startsWith(`${resolvedHome}${path.sep}`)) return false;
    const probe = path.join(resolved, `.release-readiness-${process.pid}`);
    fs.writeFileSync(probe, 'ok', {flag: 'wx', mode: 0o600});
    fs.unlinkSync(probe);
    return true;
  } catch { return false; }
}

async function github(pathname, token) {
  const response = await fetch(`https://api.github.com/repos/akhripunov/gpoint/${pathname}`, {
    headers: {authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2022-11-28'},
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new RunnerReadinessError(`GitHub network/API readiness failed (HTTP ${response.status})`);
  return response.json();
}

export async function runReadiness(env = process.env) {
  if (!env.GITHUB_TOKEN) throw new RunnerReadinessError('Existing workflow GITHUB_TOKEN is unavailable');
  const home = os.homedir();
  const runnerTemp = env.RUNNER_TEMP ?? '';
  const cache = path.join(home, '.cache', 'gpoint-release');
  const pathsReady = Boolean(runnerTemp && safeWritable(runnerTemp, home) && safeWritable(cache, home));
  let dockerOs = '';
  try { dockerOs = execFileSync('docker', ['info', '--format', '{{.OSType}}'], {encoding: 'utf8', timeout: 20_000}).trim(); } catch {}
  const [branch, runs] = await Promise.all([
    github('git/ref/heads/main', env.GITHUB_TOKEN),
    github('actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100', env.GITHUB_TOKEN),
  ]);
  const activeConflict = (runs.workflow_runs ?? []).some(run => String(run.id) !== String(env.GITHUB_RUN_ID)
    && ['queued', 'in_progress', 'waiting', 'pending'].includes(run.status));
  const result = validateRuntime({runnerName: env.RUNNER_NAME, runnerOs: env.RUNNER_OS, runnerArch: env.RUNNER_ARCH,
    freeBytes: fs.existsSync(runnerTemp) ? fs.statfsSync(runnerTemp).bavail * fs.statfsSync(runnerTemp).bsize : 0,
    dockerAvailable: Boolean(dockerOs), dockerOs, mainSha: branch.object?.sha, candidateSha: env.CANDIDATE_SHA,
    activeConflict, pathsReady});
  console.log(`RELEASE_RUNNER_READINESS=PASS ${Object.entries(result).map(([key, value]) => `${key.toUpperCase()}=${value}`).join(' ')}`);
  return result;
}
