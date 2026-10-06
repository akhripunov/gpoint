/** Fail-fast checks for the assigned GPoint self-hosted release runner. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync, spawnSync} from 'node:child_process';
import {resolveRunnerPath} from './release-gpoint-vps-adapter.mjs';

export const EXPECTED_RUNNER = 'gpoint-owner-mac';
export const MIN_FREE_BYTES = 5 * 1024 ** 3;
const WORKER_ASSETS = ['gpoint_mac_worker.py', 'agata_worker_pool.py', 'lab_queue.py',
  'lab_fixture_manifest.json', 'requirements.txt', 'run_codex_worker.sh'];
export class RunnerReadinessError extends Error {}

export function validateRuntime({runnerName, runnerOs, runnerArch, freeBytes, dockerAvailable,
  dockerOs, mainSha, candidateSha, activeConflict, pathsReady, workerRuntimeReady, toolchainAvailable,
  networkReady}) {
  if (runnerName !== EXPECTED_RUNNER || runnerOs !== 'macOS' || runnerArch !== 'ARM64') throw new RunnerReadinessError('Assigned runner identity/OS/architecture does not match GPoint lane');
  if (freeBytes < MIN_FREE_BYTES) throw new RunnerReadinessError('Insufficient free disk for release bundle/runtime work');
  if (!dockerAvailable || dockerOs !== 'linux') throw new RunnerReadinessError('Required Docker runtime is unavailable');
  if (!pathsReady) throw new RunnerReadinessError('GPoint cache/runtime paths are unavailable or unsafe');
  if (!workerRuntimeReady) throw new RunnerReadinessError('Installed GPoint worker runtime is missing or unsafe');
  if (!toolchainAvailable) throw new RunnerReadinessError('Required release command-line tools are unavailable');
  if (!networkReady) throw new RunnerReadinessError('Canonical deployment SSH/SCP network is unavailable');
  if (!candidateSha || mainSha !== candidateSha) throw new RunnerReadinessError('Candidate is no longer exact current main');
  if (activeConflict) throw new RunnerReadinessError('Conflicting GPoint production mutation is active or queued');
  return {runner: runnerName, runnerOs, runnerArch, freeBytes, docker: 'READY', network: 'READY', projectPaths: 'READY', conflictingProductionMutation: 'NO'};
}

export function probeDeploymentNetwork({runner, env = process.env, spawn = spawnSync}) {
  if (!runner || !path.isAbsolute(runner)) return false;
  try {
    fs.accessSync(runner, fs.constants.X_OK);
    const result = spawn(runner, [], {encoding: 'utf8', timeout: 40_000, maxBuffer: 2048,
      env: {...env, REMOTE_CMD: 'printf GPOINT_DEPLOYMENT_NETWORK_READY', GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS: '30'}});
    return result.status === 0 && result.stdout.trim() === 'GPOINT_DEPLOYMENT_NETWORK_READY';
  } catch { return false; }
}

export function configuredDeploymentRunner(env = process.env) {
  return resolveRunnerPath('GPOINT_REMOTE_RUNNER', {env});
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

function validWorkerRuntime(directory, home) {
  try {
    const root = fs.realpathSync(directory);
    const resolvedHome = fs.realpathSync(home);
    if (root === resolvedHome || !root.startsWith(`${resolvedHome}${path.sep}`)) return false;
    return WORKER_ASSETS.every(asset => fs.statSync(path.join(root, asset)).isFile() && fs.accessSync(path.join(root, asset), fs.constants.R_OK) === undefined);
  } catch { return false; }
}

function requiredToolsAvailable() {
  return ['pnpm', 'python3', 'git', 'ssh', 'scp', 'curl'].every(binary => Boolean(execFileSync('/usr/bin/which', [binary], {encoding: 'utf8', timeout: 5_000}).trim()));
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
  const workerRoot = path.join(home, 'Library/Application Support/GPoint/agata-codex-worker/runtime/current');
  const workerRuntimeReady = validWorkerRuntime(workerRoot, home);
  const toolchainAvailable = requiredToolsAvailable();
  const remoteRunner = configuredDeploymentRunner(env);
  const networkReady = probeDeploymentNetwork({runner: remoteRunner, env});
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
    activeConflict, pathsReady, workerRuntimeReady, toolchainAvailable, networkReady});
  console.log(`RELEASE_RUNNER_READINESS=PASS ${Object.entries(result).map(([key, value]) => `${key.toUpperCase()}=${value}`).join(' ')}`);
  return result;
}
