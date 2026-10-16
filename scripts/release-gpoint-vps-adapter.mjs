import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { verifyReleaseBundle, importBundleCommand, reuseBundleCommand, preparedImageCaptureCommand, preparedImageParityCommand, selectImageTransport } from './release-bundle.mjs';
import { createOciPullScript, createOciVerifyScript } from './release-oci.mjs';
import { migrationHead } from './release-migrations.mjs';
import { gsalesMigrationIdentity, gsalesReadinessCommand, parseGsalesReadiness, gsalesMigrationStateCommand, gsalesMigrationCommand, combinedComposeCommand, gsalesStartCommand, gsalesRuntimeCommand, parseGsalesRuntime, gsalesProductionInspectionCommand } from './release-gsales.mjs';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const REPO = 'git@github.com:akhripunov/gpoint.git';
const REMOTE_RETRY_BUDGET_MS = 30 * 60 * 1000;
const REMOTE_RETRY_BACKOFF_CAP_MS = 60_000;
const PRE_EXEC_STATUS_MARKER = 'PRE_EXEC_TRANSPORT_FAILURE:255';
export const GPOINT_DATABASE_NETWORK = 'shared_internal';
export const SOURCE_ARCHIVE_EXCLUDES = [
  '--exclude=._*', '--exclude=*/._*',
  '--exclude=.DS_Store', '--exclude=*/.DS_Store',
  '--exclude=__pycache__', '--exclude=*/__pycache__',
  '--exclude=*.pyc', '--exclude=*/*.pyc',
];
export function databaseNetworkArgs(alias) {
  return `--network ${GPOINT_DATABASE_NETWORK}${alias ? ` --network-alias ${alias}` : ''}`;
}

export function createSourceArchive(checkout, archive, { exec = execFileSync, env = process.env } = {}) {
  exec('tar', ['-czf', archive, ...SOURCE_ARCHIVE_EXCLUDES, '-C', path.dirname(checkout), path.basename(checkout)], {
    env: { ...env, COPYFILE_DISABLE: '1' },
  });
}

export function resolveRunnerPath(name, {
  env = process.env,
  configFile = path.join(os.homedir(), '.config/gpoint/release-tools.env'),
  home = os.homedir(),
} = {}) {
  const explicit = env[name]?.trim();
  if (explicit) return explicit;

  let config;
  try {
    config = fs.readFileSync(configFile, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }

  const prefix = `${name}=`;
  const line = config.split(/\r?\n/).find(entry => entry.startsWith(prefix));
  if (!line) return null;
  let value = line.slice(prefix.length).trim();
  if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
    value = value.slice(1, -1);
  }
  if (value === '$HOME' || value.startsWith('$HOME/')) value = path.join(home, value.slice(6));
  else if (value === '~' || value.startsWith('~/')) value = path.join(home, value.slice(2));
  return path.isAbsolute(value) ? path.resolve(value) : null;
}

export function validateRuntimeIdentity(candidate, runtime) {
  const failures = [];
  if (runtime?.buildSha !== candidate) failures.push('runtime build SHA mismatch');
  for (const role of ['apiImage', 'workerImage', 'botImage']) {
    if (runtime?.[role] !== runtime?.imageId) failures.push(`${role} does not use candidate image`);
  }
  return failures;
}

export function validateAgataRuntimeIdentity(candidate, runtime) {
  const failures = [];
  if (runtime?.buildSha !== candidate) failures.push('AGATA build SHA mismatch');
  if (!runtime?.imageId || runtime?.apiImage !== runtime.imageId) failures.push('AGATA API does not use candidate image');
  if (runtime?.workerImage !== runtime.imageId) failures.push('AGATA worker does not use candidate image');
  if (runtime?.apiRunning !== 'true' || runtime?.apiHealth !== 'healthy') failures.push('AGATA API is not healthy');
  if (runtime?.workerRunning !== 'true' || runtime?.workerHealth !== 'healthy') failures.push('AGATA worker is not healthy');
  return failures;
}

export function parseAgataRuntimeIdentityLine(line) {
  const keys = ['IMAGE_ID', 'BUILD_SHA', 'API_IMAGE', 'API_RUNNING', 'API_HEALTH', 'WORKER_IMAGE', 'WORKER_RUNNING', 'WORKER_HEALTH', 'HEAD'];
  return Object.fromEntries(keys.map(key => {
    const camelKey = key.toLowerCase().replace(/_([a-z])/g, (_, character) => character.toUpperCase());
    return [camelKey, line.match(new RegExp(`${key}=([^\\s]+)`))?.[1]];
  }));
}

export function validateAgataProviderConfig(config) {
  const failures = [];
  if (config?.enabled !== 'true') failures.push('AGATA is not enabled');
  failures.push(...validateAgataProviderMode(config));
  if (config?.databaseUrlPresent !== true) failures.push('AGATA database URL is missing');
  for (const name of ['tenant', 'internalAuth', 'messageKey', 'requestHmac', 'retrievalHmac', 'responseHmac', 'shadowHmac']) {
    if (config?.[name] !== true) failures.push(`AGATA internal config is missing: ${name}`);
  }
  return failures;
}

export function validateAgataProviderCompatibility(config) {
  const failures = [];
  const provider = config?.provider;
  if (!['disabled', 'mac_worker'].includes(provider)) return [`unsupported AGATA provider: ${provider ?? 'unset'}`];
  if (provider === 'disabled') {
    if (config?.localEnabled !== undefined && config.localEnabled !== false && config.localEnabled !== 'false') failures.push('AGATA Codex local provider must be disabled');
    if (config?.apiKeyPresent === true) failures.push('AGATA LLM API key must remain absent while provider is disabled');
    return failures;
  }

  if (config?.localEnabled !== true && config?.localEnabled !== 'true') failures.push('AGATA Codex local provider is not enabled');
  if (config?.workerTokenPresent !== true) failures.push('AGATA worker token/config is missing');
  if (config?.agataApiHealthy !== true) failures.push('AGATA API is not healthy');
  return failures;
}

export function validateAgataProviderMode(config, { nowMs = Date.now(), maxWorkerAgeMs = 90_000, futureClockSkewMs = 5_000 } = {}) {
  const failures = validateAgataProviderCompatibility(config);
  if (config?.provider !== 'mac_worker') return failures;
  const status = config?.workerStatus;
  if (!status || status.enabled !== true || status.queue_connectivity !== 'reachable') {
    failures.push('AGATA Mac worker authentication/status is not healthy');
    return failures;
  }
  for (const [label, field] of [['heartbeat', 'last_heartbeat_at'], ['poll', 'last_poll_at']]) {
    const timestamp = Date.parse(status[field] ?? '');
    const age = nowMs - timestamp;
    if (!Number.isFinite(timestamp) || age < -futureClockSkewMs || age > maxWorkerAgeMs) failures.push(`AGATA Mac worker ${label} is missing or stale`);
  }
  return failures;
}

export function agataInternalConfigProvisionCommand() {
  const script = `import base64, os, pathlib, secrets, tempfile, uuid\nfrom urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode\np=pathlib.Path('/opt/gpoint/env/production.env')\nlines=p.read_text().splitlines()\nvalues={}\nfor line in lines:\n    if line and not line.lstrip().startswith('#') and '=' in line:\n        k,v=line.split('=',1); values[k]=v\nif not values.get('DATABASE_URL'):\n    raise SystemExit('DATABASE_URL is required; no AGATA config written')\nvalues['AGATA_ENABLED']='true'\nprovider=values.get('AGATA_LLM_PROVIDER') or 'disabled'\nif provider not in ('disabled','mac_worker'):\n    raise SystemExit('unsupported AGATA provider; no config written')\nif provider == 'mac_worker' and (values.get('AGATA_CODEX_LOCAL_ENABLED','').lower() != 'true' or not values.get('GPOINT_AGATA_WORKER_TOKEN')):\n    raise SystemExit('mac_worker config is incomplete; no config written')\nvalues['AGATA_LLM_PROVIDER']=provider\nif not values.get('AGATA_DATABASE_URL'):\n    source=urlsplit(values['DATABASE_URL'])\n    scheme='postgresql+asyncpg' if source.scheme in ('postgres','postgresql') else source.scheme\n    query=urlencode([(k,v) for k,v in parse_qsl(source.query, keep_blank_values=True) if k!='schema'])\n    values['AGATA_DATABASE_URL']=urlunsplit((scheme,source.netloc,source.path,query,source.fragment))\nif not values.get('AGATA_TENANT_ID'): values['AGATA_TENANT_ID']=str(uuid.uuid4())\nif not values.get('AGATA_INTERNAL_API_TOKEN'): values['AGATA_INTERNAL_API_TOKEN']=secrets.token_urlsafe(48)\nfor key in ('AGATA_MESSAGE_AEAD_KEY','AGATA_REQUEST_FINGERPRINT_HMAC_KEY','AGATA_RETRIEVAL_QUERY_HMAC_KEY','AGATA_RESPONSE_FINGERPRINT_HMAC_KEY','AGATA_SHADOW_CANDIDATE_HMAC_KEY'):\n    if not values.get(key): values[key]=base64.b64encode(secrets.token_bytes(32)).decode()\nmanaged={'AGATA_ENABLED','AGATA_LLM_PROVIDER','AGATA_DATABASE_URL','AGATA_TENANT_ID','AGATA_INTERNAL_API_TOKEN','AGATA_MESSAGE_AEAD_KEY','AGATA_REQUEST_FINGERPRINT_HMAC_KEY','AGATA_RETRIEVAL_QUERY_HMAC_KEY','AGATA_RESPONSE_FINGERPRINT_HMAC_KEY','AGATA_SHADOW_CANDIDATE_HMAC_KEY'}\nout=[]; emitted=set()\nfor line in lines:\n    key=line.split('=',1)[0] if '=' in line else None\n    if key in managed:\n        if key not in emitted: out.append(key+'='+values[key]); emitted.add(key)\n    else: out.append(line)\nfor key in sorted(managed-emitted): out.append(key+'='+values[key])\nfd,tmp=tempfile.mkstemp(prefix='.production.env.',dir=str(p.parent))\ntry:\n    os.fchmod(fd, 0o600)\n    with os.fdopen(fd,'w') as f: f.write('\\n'.join(out)+'\\n'); f.flush(); os.fsync(f.fileno())\n    os.replace(tmp,p)\n    os.chmod(p, 0o600)\nfinally:\n    try: os.unlink(tmp)\n    except FileNotFoundError: pass\nprint('AGATA_INTERNAL_CONFIG_PROVISIONED=YES')\nprint('AGATA_LLM_PROVIDER='+provider)\nprint('AGATA_LLM_API_KEY_PROVISIONED=NO')`;
  return `set -e; printf '%s' '${Buffer.from(script).toString('base64')}' | base64 -d | python3 -`;
}

export function provisionAgataInternalConfig() {
  return run(agataInternalConfigProvisionCommand());
}

export function provisionAgataInternalConfigForBaseline(expectedSha) {
  if (!/^[0-9a-f]{40}$/.test(expectedSha ?? '')) throw new Error('exact current production SHA is required');
  const proof = run(`set -e; test -r /opt/gpoint/env/production.env; actual=$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1); test "$actual" = '${expectedSha}'; ${agataInternalConfigProvisionCommand()}; test "$(stat -c '%a' /opt/gpoint/env/production.env)" = 600; echo PRODUCTION_BASELINE_VERIFIED=${expectedSha}`);
  if (!proof.includes(`PRODUCTION_BASELINE_VERIFIED=${expectedSha}`)) throw new Error('production baseline did not match recorded current SHA');
  return proof;
}

export class RemoteCommandError extends Error {
  constructor(command, error, classification = 'REMOTE_COMMAND_FAILURE') {
    const commandHash = crypto.createHash('sha256').update(command).digest('hex');
    const stdout = String(error.stdout ?? '');
    const stderr = String(error.stderr ?? '');
    super(classification === 'PRE_EXEC_TRANSPORT_FAILURE'
      ? `remote SSH failed before command execution (exit=${error.status ?? 'unknown'}, command_sha256=${commandHash})`
      : `remote command failed (exit=${error.status ?? 'unknown'}, command_sha256=${commandHash})`);
    this.name = 'RemoteCommandError';
    this.code = classification;
    this.classification = classification;
    this.command = `sha256:${commandHash}`;
    this.commandHash = commandHash;
    this.exitCode = Number.isInteger(error.status) ? error.status : null;
    this.stdout = stdout;
    // Some approved wrappers multiplex the remote stderr stream into stdout.
    // Preserve that output as diagnostic stderr when the local stderr pipe is empty.
    this.stderr = stderr || stdout;
    this.cause = error;
  }
}

export function classifyPostSwitchState({ candidate, runtime, composeExitCode, health = true }) {
  const failures = validateRuntimeIdentity(candidate, runtime);
  if (failures.length || !health) return { state: 'RECOVERY_REQUIRED', switched: true, failures: [...failures, ...(!health ? ['health check failed'] : [])] };
  if (composeExitCode !== 0) return { state: 'SWITCHED_UNVERIFIED', switched: true, failures: [`compose returned exit ${composeExitCode} after switch`] };
  return { state: 'VERIFIED', switched: true, failures: [] };
}

export function shouldReuseCandidateImage({ candidate, availableTags = [], exactRuntimeImage = false }) {
  return exactRuntimeImage || availableTags.includes(`gpoint-runtime:${candidate}`);
}

const TERMINAL_RELEASE_STATES = new Set(['COMPLETE', 'FAILED', 'FAILED_PRE_SWITCH', 'FAILED_MIGRATION']);

/** Plan SHA-scoped cleanup. Backups and volumes are intentionally never candidates for deletion. */
export function planReleaseRetention({ currentSha, rollbackSha, candidates = [], imageTags = [], cacheRecords = [], containers = [], tempFiles = [] }) {
  const protectedShas = new Set([currentSha, rollbackSha].filter(Boolean));
  for (const candidate of candidates) {
    if (!TERMINAL_RELEASE_STATES.has(candidate.status) || candidate.recoveryRequired === true) protectedShas.add(candidate.sha);
  }
  const terminalShas = new Set(candidates
    .filter(candidate => TERMINAL_RELEASE_STATES.has(candidate.status) && candidate.recoveryRequired !== true)
    .map(candidate => candidate.sha));
  const removableShas = [...new Set(imageTags
    .map(tag => tag.match(/^gpoint-(?:agata-)?runtime:([0-9a-f]{40})$/)?.[1])
    .filter(sha => sha && terminalShas.has(sha) && !protectedShas.has(sha)))].sort();
  const candidateShaForResource = resource => resource.candidateSha;
  const isRemovable = resource => {
    const sha = candidateShaForResource(resource);
    return sha && terminalShas.has(sha) && !protectedShas.has(sha);
  };
  return {
    protectedShas: [...protectedShas].sort(),
    removableShas,
    removableCacheIds: [...new Set(cacheRecords.filter(record => record.reclaimable && record.shared !== true && record.id && isRemovable(record)).map(record => record.id))].sort(),
    removableContainers: [...new Set(containers.filter(container => ['created', 'exited'].includes(container.state) && isRemovable(container)).map(container => container.id))].sort(),
    removableTempFiles: [...new Set(tempFiles.filter(isRemovable).map(file => file.path))].sort(),
    volumesToRemove: [],
  };
}

export function candidateBuildCacheCleanupCommand(candidateSha) {
  if (!/^[0-9a-f]{40}$/.test(candidateSha ?? '')) throw new Error('exact candidate SHA is required for cache cleanup');
  const script = `import datetime, json, re, subprocess, sys
sha=${JSON.stringify(candidateSha)}
def run(args): return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout
def parse_iso(value): return datetime.datetime.fromisoformat(re.sub(r'(\\.\\d{6})\\d+(?=Z|[+-])',r'\\1',value).replace('Z','+00:00'))
records=[]
for line in run(['docker','buildx','history','ls','--no-trunc','--format','json']).splitlines():
    try: row=json.loads(line)
    except json.JSONDecodeError: continue
    if row.get('created_at') and row.get('completed_at'):
        row['_start']=parse_iso(row['created_at'])
        row['_end']=parse_iso(row['completed_at'])
        records.append(row)
history=[]
for row in records:
    if sha in row.get('name','') and ('gpoint-dockerfile-' + sha in row.get('name','') or 'gpoint-agata-dockerfile-' + sha in row.get('name','')):
        if row.get('status') not in ('Completed','Error'):
            print('CACHE_CLEANUP_SKIPPED=build_not_terminal'); sys.exit(0)
        history.append((row['name'],row['_start'],row['_end']))
if not history:
    print('CACHE_CLEANUP_SKIPPED=no_candidate_build_history'); sys.exit(0)
for row in records:
    if sha not in row.get('name','') and any(row['_start'] < end and row['_end'] > start for _,start,end in history):
        print('CACHE_CLEANUP_SKIPPED=overlapping_unrelated_build'); sys.exit(0)
fmt='{{.ID}}|{{.CreatedAt}}|{{.Reclaimable}}|{{.Shared}}|{{.UsageCount}}'
rows=run(['docker','buildx','du','--format',fmt]).splitlines()
targets=[]
for line in rows:
    parts=line.split('|',4)
    if len(parts)!=5: continue
    ident,created,reclaimable,shared,usage=parts
    if not re.fullmatch(r'[A-Za-z0-9_-]+',ident): continue
    try:
        stamp=created.replace(' UTC','')
        match=re.fullmatch(r'(\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d)\\.(\\d+) ([+-]\\d{4})',stamp)
        if not match: continue
        timestamp=datetime.datetime.strptime(match.group(1)+'.'+match.group(2)[:6].ljust(6,'0')+' '+match.group(3),'%Y-%m-%d %H:%M:%S.%f %z')
    except ValueError: continue
    if reclaimable!='true' or shared!='false' or usage!='1': continue
    if any(start <= timestamp <= end for _,start,end in history): targets.append(ident)
pending=set(targets); removed=set()
for _ in range(4):
    current={line.split('|',1)[0] for line in run(['docker','buildx','du','--format',fmt]).splitlines() if '|' in line}
    pending.intersection_update(current)
    if not pending: break
    pattern='^('+'|'.join(sorted(pending))+')$'
    run(['docker','buildx','prune','--filter','id~='+pattern,'--force'])
    after={line.split('|',1)[0] for line in run(['docker','buildx','du','--format',fmt]).splitlines() if '|' in line}
    removed.update(pending-after)
    if pending.issubset(after): break
    pending.intersection_update(after)
print('CACHE_RECORDS_REMOVED='+str(len(removed)))
print('CACHE_RECORDS_RETAINED='+str(len(pending)))
`;
  return `printf '%s' '${Buffer.from(script).toString('base64')}' | base64 -d | python3 -`;
}

export function migrationNeedsDeploy(statusOutput) {
  return !/Database schema is up to date!|No pending migrations to apply\./i.test(statusOutput ?? '');
}

export function detectorSourceHash(checkout) {
  const root = path.join(checkout, 'infra/production/sensitive-detector');
  const files = fs.readdirSync(root).filter(name => fs.statSync(path.join(root, name)).isFile()).sort();
  const hash = crypto.createHash('sha256');
  for (const file of files) { hash.update(file); hash.update(fs.readFileSync(path.join(root, file))); }
  return hash.digest('hex');
}

export function detectorNeedsRebuild(currentSourceHash, candidateSourceHash) {
  return currentSourceHash !== candidateSourceHash;
}

export function cleanupTemporaryWorktree(worktree, {
  removeWorktree = target => execFileSync('git', ['worktree', 'remove', '--force', target], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] }),
  removeDirectory = target => fs.rmSync(target, { recursive: true, force: true }),
  pruneWorktreeMetadata = () => execFileSync('git', ['worktree', 'prune', '--expire', 'now'], { cwd: ROOT, stdio: 'ignore' }),
} = {}) {
  const target = path.resolve(worktree);
  const tempRoot = path.resolve(os.tmpdir());
  if (path.dirname(target) !== tempRoot || !/^gpoint-(?:release|apply)-/.test(path.basename(target))) {
    throw new Error('refusing to clean a path outside a recognized GPoint temporary worktree');
  }
  try {
    removeWorktree(target);
    return 'GIT_WORKTREE_REMOVE';
  } catch {
    removeDirectory(target);
    try { pruneWorktreeMetadata(); } catch {}
    return 'FILESYSTEM_FALLBACK';
  }
}

function stage(timings, name, action) {
  const started = Date.now();
  console.error(`${name} START`);
  try { return action(); }
  catch (error) {
    if (error && typeof error === 'object' && !error.releaseStage) error.releaseStage = name;
    throw error;
  }
  finally { timings[name] = Number(((Date.now() - started) / 1000).toFixed(1)); console.error(`${name} = ${timings[name]}s`); }
}

function applyRemoteTimingMarkers(timings, output) {
  for (const match of String(output ?? '').matchAll(/GPOINT_STAGE_TIMING=([A-Z0-9_]+)_DURATION_MS=(\d+)/g)) {
    timings[match[1]] = Number((Number(match[2]) / 1000).toFixed(1));
  }
}

function run(command, env = {}, retryOptions = {}, input = undefined) {
  const runner = resolveRunnerPath('GPOINT_REMOTE_RUNNER');
  if (!runner) throw new Error('GPOINT_REMOTE_RUNNER is required for apply mode; no implicit SSH credentials are used');
  const captureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-remote-run-'));
  const stdoutFile = path.join(captureDir, 'stdout');
  const stderrFile = path.join(captureDir, 'stderr');
  const statusFile = path.join(captureDir, 'attempt-status');
  console.error(`REMOTE_COMMAND_START elapsed=0s command=${command.split(/\s+/).slice(0, 3).join(' ')}`);
  try {
    return runWithPreExecRetry(attempt => {
      fs.writeFileSync(stdoutFile, '');
      fs.writeFileSync(stderrFile, '');
      fs.writeFileSync(statusFile, '');
      const stdoutFd = fs.openSync(stdoutFile, 'w');
      const stderrFd = fs.openSync(stderrFile, 'w');
      const started = Date.now();
      let result;
      try {
        result = spawnSync(runner, [], {
          env: remoteCommandEnvironment({ ...env, REMOTE_CMD: command, GPOINT_REMOTE_ATTEMPT_STATUS_FILE: statusFile }),
          ...(input === undefined ? { stdio: ['ignore', stdoutFd, stderrFd] } : {
            input, encoding: 'utf8', stdio: ['pipe', stdoutFd, stderrFd],
          }),
        });
      } finally {
        fs.closeSync(stdoutFd);
        fs.closeSync(stderrFd);
      }
      const stdout = fs.readFileSync(stdoutFile, 'utf8');
      const stderr = fs.readFileSync(stderrFile, 'utf8');
      const elapsed = ((Date.now() - started) / 1000).toFixed(1);
      if (result.status === 0) {
        console.error(`REMOTE_COMMAND_DONE attempt=${attempt} elapsed=${elapsed}s exit=0`);
        return stdout;
      }
      const statusMarker = fs.readFileSync(statusFile, 'utf8').trim();
      const classification = result.status === 75 && statusMarker === PRE_EXEC_STATUS_MARKER
        ? 'PRE_EXEC_TRANSPORT_FAILURE'
        : 'REMOTE_COMMAND_FAILURE';
      const error = new RemoteCommandError(command, {
        status: classification === 'PRE_EXEC_TRANSPORT_FAILURE' ? 255 : result.status,
        stdout: result.error ? `${stdout}${result.error.message}` : stdout,
        stderr: result.error ? `${stderr}${result.error.message}` : stderr,
      }, classification);
      console.error(`REMOTE_COMMAND_DONE attempt=${attempt} elapsed=${elapsed}s exit=${result.status ?? 'unknown'} class=${classification}`);
      throw error;
    }, retryOptions);
  } finally {
    fs.rmSync(captureDir, { recursive: true, force: true });
  }
}

export function remoteCommandEnvironment(overrides = {}) {
  const environment = { ...process.env, ...overrides };
  // Registry credentials travel only through runRemoteCommandWithInput stdin.
  delete environment.GHCR_READ_TOKEN;
  return environment;
}

export function runRemoteCommandWithInput(command, input, env = {}) {
  if (typeof input !== 'string' || !input || input.includes('\0'))
    throw new Error('remote stdin payload is required');
  return run(command, env, {}, input);
}

export function runWithPreExecRetry(operation, {
  sleep = milliseconds => spawnSync('sleep', [String(milliseconds / 1_000)]),
  now = () => performance.now(), jitter = (low, high) => low + Math.random() * (high - low),
  budgetMs = REMOTE_RETRY_BUDGET_MS, emit = message => console.error(message),
} = {}) {
  const started = now();
  const statePath = process.env.NETWORK_RETRY_BUDGET_STATE;
  let spentMs = 0;
  if (statePath && fs.existsSync(statePath)) {
    const raw = fs.readFileSync(statePath, 'utf8').trim();
    if (!/^\d+(?:\.\d+)?$/.test(raw)) throw new Error('Invalid shared network retry budget state');
    spentMs = Number(raw) * 1000;
  }
  const remainingBudgetMs = Math.max(0, budgetMs - spentMs);
  const deadline = started + remainingBudgetMs;
  let retries = 0;
  const unavailable = () => {
    emit(`NETWORK_UNAVAILABLE stage=ssh_pre_exec elapsed_s=${((now() - started) / 1000).toFixed(1)} budget_s=${budgetMs / 1000}`);
    const error = new Error('NETWORK_UNAVAILABLE stage=ssh_pre_exec');
    error.code = 'NETWORK_UNAVAILABLE';
    return error;
  };
  try { while (true) {
    if (now() >= deadline) throw unavailable();
    try {
      const result = operation(retries + 1);
      if (now() >= deadline) throw unavailable();
      if (retries) emit(`NETWORK_RESTORED stage=ssh_pre_exec elapsed_s=${((now() - started) / 1000).toFixed(1)}`);
      emit('RELEASE_CONTINUING stage=ssh_pre_exec');
      return result;
    } catch (error) {
      if (error?.classification !== 'PRE_EXEC_TRANSPORT_FAILURE') throw error;
      const remaining = deadline - now();
      if (remaining <= 0) throw unavailable();
      const cap = Math.min(REMOTE_RETRY_BACKOFF_CAP_MS, 1_000 * 2 ** Math.min(retries, 6));
      const delay = Math.min(Math.max(0, jitter(0, cap)), remaining);
      emit(`NETWORK_RETRYING stage=ssh_pre_exec retry_in_s=${(delay / 1000).toFixed(1)} elapsed_s=${((now() - started) / 1000).toFixed(1)}`);
      retries += 1;
      sleep(delay);
    }
  } } finally {
    if (statePath && retries) {
      fs.mkdirSync(path.dirname(statePath), { recursive: true });
      const temporary = `${statePath}.${process.pid}.tmp`;
      fs.writeFileSync(temporary, `${((spentMs + Math.max(0, now() - started)) / 1000).toFixed(6)}\n`, { mode: 0o600 });
      fs.renameSync(temporary, statePath);
    }
  }
}

export function upload(localFile, remoteFile) {
  const runner = resolveRunnerPath('GPOINT_SCP_RUNNER');
  if (!runner) throw new Error('GPOINT_SCP_RUNNER is required for apply mode; no implicit SSH credentials are used');
  const maxAttempts = 2; // one controlled retry after a transient transport failure
  // Budget a transfer at 1 MiB/s plus connection/setup time. Large immutable
  // image bundles must not inherit the small source/config upload deadline.
  const bytes = fs.statSync(localFile).size;
  const timeoutSeconds = Math.min(600, Math.max(60, 30 + Math.ceil(bytes / (1024 * 1024))));
  console.error(`SOURCE_UPLOAD_START file=${path.basename(localFile)} bytes=${bytes} timeout_s=${timeoutSeconds}`);
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return execFileSync(runner, [], { encoding: 'utf8', env: { ...process.env, LOCAL_FILE: localFile, REMOTE_FILE: remoteFile, GPOINT_SCP_COMMAND_TIMEOUT_SECONDS: String(timeoutSeconds) }, stdio: ['ignore', 'pipe', 'pipe'], timeout: (timeoutSeconds + 5) * 1000 });
    } catch (error) {
      const detail = String(error.stderr || error.stdout || error.message).slice(-2000);
      console.error(`SOURCE_UPLOAD_FAIL file=${path.basename(localFile)} attempt=${attempt} exit=${error.status ?? 'unknown'} detail=${detail}`);
      if (attempt === maxAttempts) {
        const failure = new Error(`source upload failed after ${maxAttempts} attempts: ${path.basename(localFile)}`);
        failure.command = `scp upload ${path.basename(localFile)}`;
        failure.exitCode = Number.isInteger(error.status) ? error.status : null;
        failure.stderr = detail;
        throw failure;
      }
      spawnSync('sleep', ['2']);
    }
  }
}

export { run as runRemoteCommand };

export function runCleanupCommand(command, { remoteRun = run, remoteUpload = upload } = {}) {
  if (Buffer.byteLength(command, 'utf8') <= 32_768) return remoteRun(command);
  const digest = crypto.createHash('sha256').update(command).digest('hex');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-cleanup-'));
  const local = path.join(directory, 'cleanup.sh');
  const remote = `/tmp/gpoint-cleanup-${digest}.sh`;
  try {
    fs.writeFileSync(local, command, { mode: 0o600 });
    remoteUpload(local, remote);
    return remoteRun(`set -e; chmod 600 '${remote}'; test "$(sha256sum '${remote}' | cut -d ' ' -f1)" = '${digest}'; sh '${remote}'; rm -f '${remote}'`);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

export function inspectProductionReadOnly() {
  const prerequisites = run("set -e; dockerVersion=$(docker version --format '{{.Server.Version}}'); composeVersion=$(docker compose version --short); buildxVersion=$(docker buildx version | awk 'NR==1 {print $2}'); docker buildx inspect --bootstrap >/dev/null; freeKb=$(df -Pk / | awk 'NR==2 {print $4}'); test -n \"$freeKb\"; test \"$freeKb\" -ge 5242880; releaseSha=$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1); docker image inspect gpoint-runtime:$releaseSha >/dev/null; command -v caddy >/dev/null; printf 'LIVE_DOCKER_VERSION=%s\\nLIVE_COMPOSE_VERSION=%s\\nLIVE_BUILDX_VERSION=%s\\nLIVE_BUILDKIT=ready\\nLIVE_DISK_FREE_KB=%s\\nLIVE_ROLLBACK_IMAGE_AVAILABLE=YES\\n' \"$dockerVersion\" \"$composeVersion\" \"$buildxVersion\" \"$freeKb\"");
  const dbScript = `const {PrismaClient}=require('@prisma/client'); const db=new PrismaClient(); (async()=>{try{const p=await db.$queryRawUnsafe('SELECT migration_name FROM "_prisma_migrations" WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY finished_at DESC LIMIT 1'); const a=await db.$queryRawUnsafe("SELECT to_regclass('agata.alembic_version')::text AS relation"); let head='ABSENT'; if(a[0].relation){const rows=await db.$queryRawUnsafe('SELECT version_num FROM agata.alembic_version'); if(rows.length!==1) throw Error('agata head is not singular'); head=rows[0].version_num;} console.log('LIVE_PRISMA_HEAD='+(p[0]?.migration_name||'NONE')); console.log('LIVE_AGATA_HEAD='+head);}catch(e){console.error('READ_ONLY_DB_INSPECTION_FAILED='+e.name); process.exitCode=1;}finally{await db.$disconnect();}})();`;
  const encodedDbScript = Buffer.from(dbScript).toString('base64');
  const command = `set -e; envFile=/opt/gpoint/env/production.env; test -r "$envFile"; releaseSha=$(sed -n 's/^RELEASE_SHA=//p' "$envFile" | head -1); printf 'LIVE_RELEASE_ENV_SHA=%s\\n' "$releaseSha"; for name in api worker bot; do container=compose-$name-1; image=$(docker inspect "$container" --format '{{.Image}}'); running=$(docker inspect "$container" --format '{{.State.Running}}'); health=$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'); restart=$(docker inspect "$container" --format '{{.RestartCount}}'); build=$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'); upper=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]'); printf 'LIVE_%s_IMAGE=%s LIVE_%s_SHA=%s LIVE_%s_RUNNING=%s LIVE_%s_HEALTH=%s LIVE_%s_RESTARTS=%s\\n' "$upper" "$image" "$upper" "$build" "$upper" "$running" "$upper" "$health" "$upper" "$restart"; done; mini=$(grep -oE 'gpoint-build-sha" content="[0-9a-f]{40}' /opt/gpoint/app/miniapp/index.html | head -1 | cut -d'"' -f3); printf 'LIVE_MINIAPP_SHA=%s\\n' "$mini"; printf '%s' '${encodedDbScript}' | base64 -d | docker exec -i -w /app/apps/api compose-api-1 node; for name in AGATA_ENABLED AGATA_LLM_PROVIDER AGATA_DATABASE_URL AGATA_TENANT_ID AGATA_INTERNAL_API_TOKEN AGATA_MESSAGE_AEAD_KEY AGATA_REQUEST_FINGERPRINT_HMAC_KEY AGATA_RETRIEVAL_QUERY_HMAC_KEY AGATA_RESPONSE_FINGERPRINT_HMAC_KEY AGATA_SHADOW_CANDIDATE_HMAC_KEY; do if grep -Eq "^\${name}=.+$" "$envFile"; then present=YES; else present=NO; fi; printf 'LIVE_CONFIG_%s=%s\\n' "$name" "$present"; done; value=$(sed -n 's/^AGATA_ENABLED=//p' "$envFile" | head -1); printf 'LIVE_AGATA_ENABLED=%s\\n' "\${value:-unset}"; value=$(sed -n 's/^AGATA_LLM_PROVIDER=//p' "$envFile" | head -1); printf 'LIVE_AGATA_PROVIDER=%s\\n' "\${value:-unset}"`;
  const agataRuntime = run("set -e; envFile=/opt/gpoint/env/production.env; for role in API WORKER; do if [ \"$role\" = API ]; then container=compose-agata-api-1; else container=compose-agata-memory-worker-1; fi; if docker inspect \"$container\" >/dev/null 2>&1; then imageId=$(docker inspect \"$container\" --format '{{.Image}}'); image=$(docker inspect \"$container\" --format '{{.Config.Image}}'); build=$(docker image inspect \"$imageId\" --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}'); running=$(docker inspect \"$container\" --format '{{.State.Running}}'); health=$(docker inspect \"$container\" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'); printf 'LIVE_AGATA_%s_IMAGE=%s LIVE_AGATA_%s_IMAGE_ID=%s LIVE_AGATA_%s_SHA=%s LIVE_AGATA_%s_RUNNING=%s LIVE_AGATA_%s_HEALTH=%s\\n' \"$role\" \"$image\" \"$role\" \"$imageId\" \"$role\" \"$build\" \"$role\" \"$running\" \"$role\" \"$health\"; fi; done; if grep -Eq '^AGATA_LLM_API_KEY=.+$' \"$envFile\"; then echo LIVE_CONFIG_AGATA_LLM_API_KEY=YES; else echo LIVE_CONFIG_AGATA_LLM_API_KEY=NO; fi");
  const providerConfig = run("set -e; envFile=/opt/gpoint/env/production.env; for name in AGATA_CODEX_LOCAL_ENABLED GPOINT_AGATA_WORKER_TOKEN; do if grep -Eq \"^${name}=.+$\" \"$envFile\"; then present=YES; else present=NO; fi; printf 'LIVE_CONFIG_%s=%s\\n' \"$name\" \"$present\"; done; value=$(sed -n 's/^AGATA_CODEX_LOCAL_ENABLED=//p' \"$envFile\" | head -1); printf 'LIVE_AGATA_CODEX_LOCAL_ENABLED=%s\\n' \"${value:-unset}\"");
  const tenant = run("set -e; sed -n 's/^AGATA_TENANT_ID=/LIVE_AGATA_TENANT_ID=/p' /opt/gpoint/env/production.env");
  return `${tenant}\n${prerequisites}\n${run(command)}\n${agataRuntime}\n${providerConfig}\n${run(gsalesProductionInspectionCommand())}`;
}

export function inspectGsalesReadiness(candidate, baselineSha, execute = run) {
  return parseGsalesReadiness(execute(gsalesReadinessCommand(candidate, baselineSha)), candidate, baselineSha);
}

export function exactCheckout(sha) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-apply-'));
  try {
    // runApply has already verified this SHA against origin/main. Reuse the
    // verified local object instead of opening another fragile SSH session.
    execFileSync('git', ['worktree', 'add', '--detach', dir, sha], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
    const actual = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir, encoding: 'utf8' }).trim();
    if (actual !== sha) throw new Error('apply checkout SHA mismatch');
    if (execFileSync('git', ['status', '--porcelain'], { cwd: dir, encoding: 'utf8' }).trim()) throw new Error('apply checkout is dirty');
    return dir;
  } catch (error) {
    cleanupTemporaryWorktree(dir);
    throw error;
  }
}

export function agataProductionMigrationCommand(sha, mode, checkout = ROOT) {
  if (!/^[0-9a-f]{40}$/.test(sha ?? '') || !['preflight', 'backup', 'migrate'].includes(mode)) throw new Error('exact SHA and supported migration mode required');
  const source = fs.readFileSync(path.join(checkout, 'scripts/agata-production-migration.py'), 'utf8');
  const encoded = Buffer.from(source).toString('base64');
  return `python3 -c "import base64,sys;sys.argv=['agata-production-migration.py','${mode}','--sha','${sha}'];exec(base64.b64decode('${encoded}'))"`;
}

export const agata0010Command = agataProductionMigrationCommand;

export function inspectAgataMigrationReadiness(sha, beforeHead, candidateHead, execute = run) {
  if (beforeHead === candidateHead) return null;
  const expectedBaseline = { agata0010: 'agata0009', agata0011: 'agata0010' }[candidateHead];
  if (!expectedBaseline) return null;
  if (beforeHead !== expectedBaseline) throw new Error(`${candidateHead} production guard requires ${expectedBaseline} baseline`);
  const output = execute(agataProductionMigrationCommand(sha, 'preflight'));
  const line = output.split(/\r?\n/).find(value => value.startsWith('AGATA_MIGRATION_READINESS='));
  let facts;
  try { facts = JSON.parse(line?.slice('AGATA_MIGRATION_READINESS='.length)); } catch { throw new Error('AGATA migration readiness evidence absent'); }
  if (facts.readiness !== 'PASS' || facts.database !== 'gpoint' || facts.head !== beforeHead || !Number.isInteger(facts.tenant_count) || facts.tenant_count < 1 || !/^[0-9a-f]{64}$/.test(facts.inventory_sha256 ?? '')) throw new Error('AGATA migration readiness failed');
  return facts;
}

export function parseGuardedMigrationEvidence(output, sha) {
  const read = marker => {
    const line = output.split(/\r?\n/).find(value => value.startsWith(marker));
    return line ? JSON.parse(line.slice(marker.length)) : null;
  };
  const backup = read('GPOINT_COMPLETE_BACKUP=');
  const baseline = read('AGATA_MIGRATION_VERIFIED=');
  if (!backup && !baseline) return null;
  const backupSchemas = backup?.schemas?.join(',');
  const extraSchemas = backup?.schemas?.slice(2) ?? [];
  const schemaListValid = backupSchemas === 'public,agata' ||
    (backup?.schemas?.[0] === 'public' && backup?.schemas?.[1] === 'agata' &&
      extraSchemas.length > 0 && extraSchemas.join(',') === ['gsales', 'gsales_meta', 'agata_identity'].filter(schema => extraSchemas.includes(schema)).join(','));
  const backupExtensions = backup?.extensions ?? [];
  if (!Array.isArray(backupExtensions) || backupExtensions.some(name => typeof name !== 'string' || !/^[a-z][a-z0-9_-]*$/.test(name)) || new Set(backupExtensions).size !== backupExtensions.length) throw new Error('complete GPoint backup extension evidence invalid');
  if (!backup || backup.status !== 'COMPLETE_PROTECTED' || backup.database !== 'gpoint' || !schemaListValid || !/^[0-9a-f]{64}$/.test(backup.sha256 ?? '') || !backup.path?.startsWith('/opt/gpoint/backups/')) throw new Error('complete GPoint backup proof missing or invalid');
  if (backup.candidate_sha !== sha || (baseline && baseline.candidate_sha !== sha)) throw new Error('guarded migration evidence exact SHA mismatch');
  if (baseline && (baseline.readiness !== 'PASS' || !['agata0010', 'agata0011'].includes(baseline.head_after) || !Number.isInteger(baseline.tenant_count) || baseline.tenant_count < 1 || !/^[0-9a-f]{64}$/.test(baseline.inventory_sha256 ?? ''))) throw new Error('guarded baseline proof invalid');
  return { backup, baseline };
}

export function productionPreflightCommand() {
  return `set -e; command -v docker >/dev/null; docker version --format 'DOCKER_VERSION={{.Server.Version}}'; docker compose version; docker buildx version; docker buildx inspect --bootstrap >/dev/null; freeKb=$(df -Pk / | awk 'NR==2 {print $4}'); test -n "$freeKb"; test "$freeKb" -ge 5242880; test -r /opt/gpoint/env/production.env; currentSha=$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1); printf '%s\\n' "$currentSha" | grep -Eq '^[0-9a-f]{40}$'; currentImage=$(docker inspect compose-api-1 --format '{{.Config.Image}}'); dbStatus=$(docker run --rm ${databaseNetworkArgs('gpoint-preflight')} --env-file /opt/gpoint/env/production.env "$currentImage" pnpm --filter @gpoint/db exec prisma migrate status 2>&1); test -n "$dbStatus"; printf '%s\\n' "$dbStatus" | tail -5; for name in AGATA_ENABLED AGATA_LLM_PROVIDER AGATA_DATABASE_URL AGATA_TENANT_ID AGATA_INTERNAL_API_TOKEN AGATA_MESSAGE_AEAD_KEY AGATA_REQUEST_FINGERPRINT_HMAC_KEY AGATA_RETRIEVAL_QUERY_HMAC_KEY AGATA_RESPONSE_FINGERPRINT_HMAC_KEY AGATA_SHADOW_CANDIDATE_HMAC_KEY; do grep -Eq "^$name=.+\\$" /opt/gpoint/env/production.env || { echo AGATA_CONFIG_MISSING=$name; exit 32; }; done; grep -qx 'AGATA_ENABLED=true' /opt/gpoint/env/production.env; provider=$(sed -n 's/^AGATA_LLM_PROVIDER=//p' /opt/gpoint/env/production.env | head -1); case "$provider" in disabled) grep -qx 'AGATA_CODEX_LOCAL_ENABLED=false' /opt/gpoint/env/production.env && ! grep -Eq '^AGATA_LLM_API_KEY=.+$' /opt/gpoint/env/production.env ;; mac_worker) grep -qx 'AGATA_CODEX_LOCAL_ENABLED=true' /opt/gpoint/env/production.env && grep -Eq '^GPOINT_AGATA_WORKER_TOKEN=.+$' /opt/gpoint/env/production.env ;; *) echo AGATA_PROVIDER_UNSUPPORTED; exit 32 ;; esac; printf 'GPOINT_PREFLIGHT_OK\\n'`;
}

export function agataPreSwitchReadinessCommand(sha, provider) {
  if (!/^[0-9a-f]{40}$/.test(sha ?? '') || !['disabled', 'mac_worker'].includes(provider)) throw new Error('exact SHA and supported AGATA provider are required');
  const macWorkerConfig = provider === 'mac_worker'
    ? "grep -qx 'AGATA_CODEX_LOCAL_ENABLED=true' /opt/gpoint/env/production.env; grep -Eq '^GPOINT_AGATA_WORKER_TOKEN=.+$' /opt/gpoint/env/production.env;"
    : "grep -qx 'AGATA_CODEX_LOCAL_ENABLED=false' /opt/gpoint/env/production.env; ! grep -Eq '^AGATA_LLM_API_KEY=.+$' /opt/gpoint/env/production.env;";
  return `set -e; ${macWorkerConfig} RELEASE_SHA=${sha} docker compose -p compose --profile agata --env-file /opt/gpoint/env/production.env -f /tmp/gpoint-compose-${sha}.yml up -d --no-build agata-api agata-memory-worker; ready=0; for attempt in $(seq 1 45); do apiRunning=$(docker inspect --format '{{.State.Running}}' compose-agata-api-1 2>/dev/null || echo false); apiHealth=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-agata-api-1 2>/dev/null || echo missing); workerRunning=$(docker inspect --format '{{.State.Running}}' compose-agata-memory-worker-1 2>/dev/null || echo false); workerHealth=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-agata-memory-worker-1 2>/dev/null || echo missing); echo AGATA_READINESS_ATTEMPT=$attempt API_RUNNING=$apiRunning API_HEALTH=$apiHealth WORKER_RUNNING=$workerRunning WORKER_HEALTH=$workerHealth; if [ "$apiRunning" = true ] && [ "$apiHealth" = healthy ] && [ "$workerRunning" = true ] && [ "$workerHealth" = healthy ]; then ready=1; break; fi; sleep 2; done; test "$ready" = 1; docker exec compose-agata-api-1 python -c "from app.config import get_settings; s=get_settings(); assert s.llm_provider == '${provider}'; assert ${provider === 'mac_worker' ? 's.codex_local_enabled' : 'True'}; print('AGATA_PROVIDER_VERIFIED=${provider}')"; docker run --rm --network gpoint_app --env-file /opt/gpoint/env/production.env --entrypoint node gpoint-runtime:${sha} -e 'fetch("http://agata-api:8000/internal/v1/users/gpoint-release-readiness/age-assurance", {headers:{authorization:"Bearer "+process.env.AGATA_INTERNAL_API_TOKEN}}).then(async r=>{if(!r.ok) throw Error("internal auth/readiness HTTP "+r.status); await r.arrayBuffer(); console.log("GPOINT_AGATA_INTERNAL_AUTH_OK")}).catch(e=>{console.error(e.message);process.exit(1)})'; echo AGATA_PRE_SWITCH_READY`;
}

export function agataProviderRuntimeCommand(provider) {
  if (!['disabled', 'mac_worker'].includes(provider)) throw new Error('supported AGATA provider is required');
  return `set -e; actualProvider=$(docker exec compose-agata-api-1 python -c "from app.config import get_settings; print(get_settings().llm_provider)"); localEnabled=$(docker exec compose-agata-api-1 python -c "from app.config import get_settings; print(str(get_settings().codex_local_enabled).lower())"); tokenPresent=$(docker exec compose-agata-api-1 sh -c 'test -n "$GPOINT_AGATA_WORKER_TOKEN" && printf YES || printf NO'); printf 'AGATA_PROVIDER=%s AGATA_CODEX_LOCAL_ENABLED=%s AGATA_WORKER_TOKEN_PRESENT=%s\\n' "$actualProvider" "$localEnabled" "$tokenPresent"`;
}

export function applyCandidate(sha, { retentionPlan = { removableShas: [], removableCacheIds: [] }, provider = 'disabled', imageTransport: requestedImageTransport = process.env.IMAGE_TRANSPORT || 'prepared', ghcrReadToken = '', onBeforeMutation = () => {}, onMigrationStart = () => {}, onAgataStart = () => {}, onSwitchStart = () => {} } = {}) {
  if (!['disabled', 'mac_worker'].includes(provider)) throw new Error(`unsupported AGATA provider: ${provider}`);
  const bundle = process.env.GPOINT_RELEASE_BUNDLE ? verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, sha) : null;
  if (!bundle) throw new Error('verified immutable CI release bundle is required; application images are never rebuilt during apply');
  const imageTransport = selectImageTransport(bundle, requestedImageTransport);
  const checkout = exactCheckout(sha);
  let agataMigrations;
  let gsalesMigrations;
  const timings = {};
  const detectorHash = detectorSourceHash(checkout);
  const detectorTag = `gpoint-sensitive-detector:${detectorHash}`;
  const releaseDir = `/opt/gpoint/releases/${sha}`;
  const imageProof = `/tmp/gpoint-image-id-${sha}`;
  const agataImageProof = `/tmp/gpoint-agata-image-id-${sha}`;
  const archive = `${checkout}.tar.gz`;
  const agataStatusCode = `import os, psycopg\nfrom sqlalchemy.engine import make_url\nurl = make_url(os.environ.get('AGATA_MIGRATOR_DATABASE_URL') or os.environ['AGATA_DATABASE_URL'])\nquery = {key: value for key, value in url.query.items() if key in {'sslmode','sslrootcert','sslcert','sslkey','connect_timeout','application_name'}}\nurl = url.set(drivername='postgresql', query=query)\nwith psycopg.connect(url.render_as_string(hide_password=False)) as conn:\n    row = conn.execute(\"SELECT to_regclass('agata.alembic_version')\").fetchone()\n    if row[0] is None:\n        print('AGATA_SCHEMA_ABSENT')\n    else:\n        heads = conn.execute('SELECT version_num FROM agata.alembic_version').fetchall()\n        if len(heads) != 1:\n            raise RuntimeError('AGATA migration head is not singular')\n        print('AGATA_HEAD=' + heads[0][0])\n`;
  const agataStatusB64 = Buffer.from(agataStatusCode).toString('base64');
  try {
    const migrationDirectory = path.join(checkout, 'services/agata/migrations/versions');
    agataMigrations = migrationHead(fs.readdirSync(migrationDirectory).filter(name => name.endsWith('.py')).map(name => fs.readFileSync(path.join(migrationDirectory, name), 'utf8')));
    if (bundle && bundle.agataHead !== agataMigrations.head) throw new Error('CI migration head does not match candidate source');
    gsalesMigrations = gsalesMigrationIdentity(checkout, bundle);
    if (agataMigrations.head !== 'agata0011') throw new Error('GSales integration requires AGATA agata0011');
    stage(timings, 'PRODUCTION_PREFLIGHT', () => run(productionPreflightCommand()));
    if (imageTransport === 'ghcr-oci') {
      const registryUser = process.env.GHCR_READ_USERNAME || 'akhripunov';
      if (!ghcrReadToken) throw new Error('read-only GHCR credential is unavailable');
      const output = stage(timings, 'GHCR_IMAGE_PULL_AND_VERIFY', () =>
        runRemoteCommandWithInput(createOciPullScript(bundle, sha, { username: registryUser }), ghcrReadToken));
      if (!output.includes('GPOINT_GHCR_IMAGES_VERIFIED=YES'))
        throw new Error('GHCR image pull/identity gate failed before production apply');
      bundle.ghcrVerified = true;
    } else if (bundle) {
      if (!bundle.archive) throw new Error('verified archive fallback is unavailable before Apply');
      stage(timings, 'CI_ARTIFACT_IMPORT', () => {
      const reuse = stage(timings, 'CI_ARTIFACT_REUSE_CHECK', () => run(reuseBundleCommand(bundle, sha)));
      if (reuse.includes('GPOINT_CI_IMAGES_REUSABLE')) {
        console.error('CI_ARTIFACT_REUSED=YES');
        return;
      }
      stage(timings, 'CI_ARTIFACT_UPLOAD', () => upload(bundle.archive, `/tmp/gpoint-images-${sha}.tar.gz`));
      const output = stage(timings, 'CI_ARTIFACT_LOAD', () => run(importBundleCommand(bundle, sha)));
      if (!output.includes('GPOINT_CI_IMAGES_VERIFIED')) throw new Error('CI image import failed');
      });
    }
    createSourceArchive(checkout, archive);
    stage(timings, 'SOURCE_PREPARE', () => {
      upload(archive, `/tmp/gpoint-${sha}.tar.gz`);
    });
    const diskGuard = run("set -e; freeKb=$(df -Pk / | awk 'NR==2 {print $4}'); test -n \"$freeKb\"; if [ \"$freeKb\" -lt 8388608 ]; then echo GPOINT_DISK_GUARD_WARN; fi; test \"$freeKb\" -ge 5242880; echo GPOINT_DISK_GUARD_OK");
    if (!diskGuard.includes('GPOINT_DISK_GUARD_OK')) throw new Error('remote disk guard failed: minimum 5 GiB free space required');
    const build = [
      'set -e',
      `rm -rf ${releaseDir}`,
      `mkdir -p ${releaseDir}`,
      `tar -xzf /tmp/gpoint-${sha}.tar.gz -C ${releaseDir}`,
      `mv ${releaseDir}/${path.basename(checkout)} ${releaseDir}/source`,
      combinedComposeCommand(sha),
      `cp ${releaseDir}/source/infra/production/Gpoint.Dockerfile /tmp/gpoint-dockerfile-${sha}`,
      `cp ${releaseDir}/source/infra/production/sensitive-detector/config.mjs /tmp/gpoint-safety-config-${sha}.mjs`,
      `cp ${releaseDir}/source/infra/production/sensitive-detector/Dockerfile /tmp/gpoint-safety-dockerfile-${sha}`,
      `cp ${releaseDir}/source/services/agata/Dockerfile /tmp/gpoint-agata-dockerfile-${sha}`,
      `cp ${releaseDir}/source/infra/production/Caddyfile /tmp/gpoint-caddy-${sha}`,
      `caddy validate --config /tmp/gpoint-caddy-${sha} --adapter caddyfile`,
      `RELEASE_SHA=${sha} docker compose -p compose --env-file /opt/gpoint/env/production.env -f /tmp/gpoint-compose-${sha}.yml config --quiet`,
      preparedImageCaptureCommand(bundle, sha),
      `mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-build-${sha}.log /opt/gpoint/release-logs/${sha}/runtime-build.log 2>/dev/null || true && cp /tmp/gpoint-agata-build-${sha}.log /opt/gpoint/release-logs/${sha}/agata-build.log 2>/dev/null || true`,
      'echo GPOINT_BUILD_OK AGATA_BUILD_OK GSALES_BUILD_OK'
    ].join('; ');
    const buildOutput = stage(timings, 'RUNTIME_BUILD', () => run(build)); if (!buildOutput.includes('GPOINT_BUILD_OK') || !buildOutput.includes('AGATA_BUILD_OK') || !buildOutput.includes('GSALES_BUILD_OK')) throw new Error('immutable app/AGATA/G Sales image import failed');
    let effectiveConfigSha256 = buildOutput.match(/GPOINT_EFFECTIVE_CONFIG_SHA256=([0-9a-f]{64})/)?.[1];
    if (!effectiveConfigSha256) throw new Error('effective production config evidence absent');
    stage(timings, 'CI_ARTIFACT_PARITY', () => run(preparedImageParityCommand(bundle, sha)));
    const migration = stage(timings, 'MIGRATION_APPLY', () => {
      const steps = [
        'set -e',
        `status=$(docker run --rm ${databaseNetworkArgs('gpoint-migration')} --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate status 2>&1 || true)`,
        `printf '%s\\n' "$status"`,
        `prismaPending=0; if printf '%s' "$status" | grep -Eiq 'Database schema is up to date!|No pending migrations to apply\\.'; then echo PRISMA_NO_PENDING_MIGRATIONS; elif printf '%s' "$status" | grep -Eiq 'Following migrations have not yet been applied|The following migration(s) have not yet been applied'; then prismaPending=1; echo PRISMA_PENDING_MIGRATIONS; else echo PRISMA_STATUS_UNRECOGNIZED; exit 33; fi`,
        `agataBefore=$(printf '%s' '${agataStatusB64}' | base64 -d | docker run --rm -i ${databaseNetworkArgs('gpoint-agata-preflight')} --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/migration.env gpoint-agata-runtime:${sha} python -)`,
        `printf '%s\\n' "$agataBefore"`,
        `agataPending=0; if printf '%s' "$agataBefore" | grep -qx 'AGATA_SCHEMA_ABSENT'; then agataPending=1; elif printf '%s' "$agataBefore" | grep -Eq '^AGATA_HEAD=(${agataMigrations.revisions.join('|')})$'; then if ! printf '%s' "$agataBefore" | grep -qx 'AGATA_HEAD=${agataMigrations.head}'; then agataPending=1; fi; else echo AGATA_HEAD_UNRECOGNIZED; exit 34; fi`,
        `gsalesState=$( ${gsalesMigrationStateCommand(gsalesMigrations)} ); printf '%s\\n' "$gsalesState"; gsalesPending=$(printf '%s' "$gsalesState" | sed -n 's/^GSALES_PENDING=//p'); test "$gsalesPending" = 0 || test "$gsalesPending" = 1`,
        `if [ "$prismaPending" = 1 ] || [ "$agataPending" = 1 ] || [ "$gsalesPending" = 1 ]; then ${['agata0010', 'agata0011'].includes(agataMigrations.head) ? agataProductionMigrationCommand(sha, 'backup', checkout) : `backupName=gpoint-pre-release-${sha}-$(date -u +%Y%m%dT%H%M%SZ).dump; mkdir -p /opt/gpoint/backups; chmod 700 /opt/gpoint/backups; docker image inspect postgres:16 >/dev/null 2>&1 || docker pull postgres:16; docker run --rm ${databaseNetworkArgs()} --env-file /opt/gpoint/env/production.env -e GPOINT_BACKUP_NAME="$backupName" -v /opt/gpoint/backups:/backups postgres:16 sh -ec 'dburl=$(printf "%s" "$DATABASE_URL" | sed -E "s/([?&])(schema|connection_limit|pool_timeout|pgbouncer|sslaccept)=[^&]*//g; s/[?&]$//; s/\\?&/?/g"); schemas="--schema=public"; if psql "$dburl" -Atc "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name='\''agata'\'')" | grep -qx t; then schemas="$schemas --schema=agata"; fi; pg_dump --format=custom --no-owner $schemas --dbname "$dburl" --file "/backups/$GPOINT_BACKUP_NAME"'; chmod 600 "/opt/gpoint/backups/$backupName"; test -s "/opt/gpoint/backups/$backupName"; docker run --rm -v /opt/gpoint/backups:/backups postgres:16 pg_restore --list "/backups/$backupName" >/dev/null; echo GPOINT_BACKUP_PATH=/opt/gpoint/backups/$backupName; echo GPOINT_BACKUP_STATUS=NONEMPTY_PROTECTED`}; fi`,
        `if [ "$prismaPending" = 1 ]; then docker run --rm ${databaseNetworkArgs('gpoint-migration')} --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate deploy; fi`,
        `if [ "$agataPending" = 1 ]; then ${['agata0010', 'agata0011'].includes(agataMigrations.head) ? agataProductionMigrationCommand(sha, 'migrate', checkout) : `docker run --rm ${databaseNetworkArgs('gpoint-agata-migration')} --env-file /opt/gpoint/env/production.env gpoint-agata-runtime:${sha} alembic upgrade head`}; else echo AGATA_NO_PENDING_MIGRATIONS; fi`,
        `agataAfter=$(printf '%s' '${agataStatusB64}' | base64 -d | docker run --rm -i ${databaseNetworkArgs('gpoint-agata-verify')} --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/migration.env gpoint-agata-runtime:${sha} python -)`,
        `printf '%s\\n' "$agataAfter"; printf '%s' "$agataAfter" | grep -qx 'AGATA_HEAD=${agataMigrations.head}'`,
        gsalesMigrationCommand(sha, gsalesMigrations),
        'echo GPOINT_MIGRATION_OK',
      ];
      onBeforeMutation('database-migration');
      onMigrationStart();
      return run(steps.join('; '));
    });
    if (!migration.includes('GPOINT_MIGRATION_OK')) throw new Error('Prisma/AGATA migration gate failed');
    const migrationProof = parseGuardedMigrationEvidence(migration, sha);
    const preparation = [
      'set -e; stageStartMs=$(date +%s%3N)',
      `sed -i 's#image: gpoint-sensitive-detector:.*#image: ${detectorTag}#' /tmp/gpoint-compose-${sha}.yml`,
      `cp /tmp/gpoint-compose-${sha}.yml /tmp/gpoint-compose-build-${sha}.yml`,
      `sed -i 's#context: /opt/gpoint/compose/sensitive-detector#context: ${releaseDir}/source/infra/production/sensitive-detector#' /tmp/gpoint-compose-build-${sha}.yml`,
      `caddy validate --config /tmp/gpoint-caddy-${sha} --adapter caddyfile`,
      `RELEASE_SHA=${sha} docker compose -p compose --env-file /opt/gpoint/env/production.env -f /tmp/gpoint-compose-${sha}.yml config --quiet`,
      `umask 077; RELEASE_SHA=${sha} docker compose -p compose --profile agata --profile gsales --env-file /opt/gpoint/env/production.env -f /tmp/gpoint-compose-${sha}.yml config --format json > /opt/gpoint/release-logs/${sha}/effective-compose.json; chmod 600 /opt/gpoint/release-logs/${sha}/effective-compose.json; sha256sum /opt/gpoint/release-logs/${sha}/effective-compose.json | awk '{print "GPOINT_EFFECTIVE_CONFIG_SHA256=" $1}'`,
      `mkdir -p ${releaseDir}/miniapp`,
      `docker rm -f gpoint-static-${sha.slice(0, 8)} >/dev/null 2>&1 || true`,
      `docker create --name gpoint-static-${sha.slice(0, 8)} gpoint-runtime:${sha} >/dev/null`,
      `docker cp gpoint-static-${sha.slice(0, 8)}:/app/apps/miniapp/dist/. ${releaseDir}/miniapp/`,
      `docker rm gpoint-static-${sha.slice(0, 8)} >/dev/null`,
      `chmod -R a+rX ${releaseDir}/miniapp`,
      `echo GPOINT_STAGE_TIMING=COMPOSE_PREPARE_DURATION_MS=$(( $(date +%s%3N) - stageStartMs )); auxStartMs=$(date +%s%3N); auxNeedsRecreate=0; if docker image inspect ${detectorTag} >/dev/null 2>&1; then echo GPOINT_AUX_REUSED; else auxNeedsRecreate=1; RELEASE_SHA=${sha} DOCKER_BUILDKIT=1 docker compose -p compose --env-file /opt/gpoint/env/production.env -f /tmp/gpoint-compose-build-${sha}.yml build --progress=plain gpoint-sensitive-detector > /tmp/gpoint-detector-build-${detectorHash}.log 2>&1; fi; printf 'GPOINT_AUX_NEEDS_RECREATE=%s\\n' \"$auxNeedsRecreate\" > /tmp/gpoint-aux-${sha}; echo GPOINT_STAGE_TIMING=AUX_BUILD_DURATION_MS=$(( $(date +%s%3N) - auxStartMs ))`,
      `mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-detector-build-${detectorHash}.log /opt/gpoint/release-logs/${sha}/detector-build.log 2>/dev/null || true`,
      `docker inspect compose-api-1 --format '{{.Config.Image}}' > /tmp/gpoint-previous-runtime-${sha} 2>/dev/null || true`,
      'echo GPOINT_PRE_SWITCH_ARTIFACTS_READY'
    ];
    const deploy = [
      `switchStartMs=$(date +%s%3N); set -e; mkdir -p /opt/gpoint/backups; chmod 700 /opt/gpoint/backups; backupStamp=$(date -u +%Y%m%dT%H%M%SZ); cp /opt/gpoint/compose/docker-compose.yml /opt/gpoint/backups/compose-before-${sha}-$backupStamp.yml; chmod 600 /opt/gpoint/backups/compose-before-${sha}-$backupStamp.yml; cp /etc/caddy/Caddyfile /opt/gpoint/backups/caddy-before-${sha}-$backupStamp; chmod 600 /opt/gpoint/backups/caddy-before-${sha}-$backupStamp; cp /opt/gpoint/compose/sensitive-detector/config.mjs /opt/gpoint/backups/detector-config-before-${sha}-$backupStamp.mjs; cp /opt/gpoint/compose/sensitive-detector/Dockerfile /opt/gpoint/backups/detector-dockerfile-before-${sha}-$backupStamp; chmod 600 /opt/gpoint/backups/detector-config-before-${sha}-$backupStamp.mjs /opt/gpoint/backups/detector-dockerfile-before-${sha}-$backupStamp; envStage=/opt/gpoint/env/.production-${sha}.staged; cp /opt/gpoint/env/production.env \"$envStage\"; chmod 600 \"$envStage\"; sed -i 's/^RELEASE_SHA=.*/RELEASE_SHA=${sha}/' \"$envStage\"; grep -q '^GPOINT_SAFETY_PROVIDER=' \"$envStage\" && sed -i 's#^GPOINT_SAFETY_PROVIDER=.*#GPOINT_SAFETY_PROVIDER=misskey-sensitive-detector#' \"$envStage\" || printf '%s\\n' 'GPOINT_SAFETY_PROVIDER=misskey-sensitive-detector' >> \"$envStage\"; grep -q '^GPOINT_SAFETY_URL=' \"$envStage\" && sed -i 's#^GPOINT_SAFETY_URL=.*#GPOINT_SAFETY_URL=http://gpoint-sensitive-detector:3009#' \"$envStage\" || printf '%s\\n' 'GPOINT_SAFETY_URL=http://gpoint-sensitive-detector:3009' >> \"$envStage\"; grep -q '^GPOINT_SAFETY_TIMEOUT_MS=' \"$envStage\" && sed -i 's#^GPOINT_SAFETY_TIMEOUT_MS=.*#GPOINT_SAFETY_TIMEOUT_MS=60000#' \"$envStage\" || printf '%s\\n' 'GPOINT_SAFETY_TIMEOUT_MS=60000' >> \"$envStage\"; if ! grep -q '^GPOINT_SAFETY_API_KEY=.' \"$envStage\"; then printf 'GPOINT_SAFETY_API_KEY=%s\\n' \"$(openssl rand -hex 32)\" >> \"$envStage\"; fi; chmod 600 \"$envStage\"; cp /opt/gpoint/env/production.env /opt/gpoint/backups/production-env-before-${sha}-$backupStamp.env; chmod 600 /opt/gpoint/backups/production-env-before-${sha}-$backupStamp.env; mv \"$envStage\" /opt/gpoint/env/production.env; cp /tmp/gpoint-compose-${sha}.yml /opt/gpoint/compose/docker-compose.yml; caddy validate --config /tmp/gpoint-caddy-${sha} --adapter caddyfile; cp /tmp/gpoint-caddy-${sha} /etc/caddy/Caddyfile; systemctl reload caddy; mkdir -p /opt/gpoint/compose/sensitive-detector && cp /tmp/gpoint-safety-config-${sha}.mjs /opt/gpoint/compose/sensitive-detector/config.mjs && cp /tmp/gpoint-safety-dockerfile-${sha} /opt/gpoint/compose/sensitive-detector/Dockerfile; if [ -d /opt/gpoint/app/miniapp ]; then mv /opt/gpoint/app/miniapp /opt/gpoint/app/miniapp.rollback-${sha}; fi; mv ${releaseDir}/miniapp /opt/gpoint/app/miniapp; chmod -R a+rX /opt/gpoint/app/miniapp; auxStartMs=$(date +%s%3N); auxNeedsRecreate=0; if docker image inspect ${detectorTag} >/dev/null 2>&1; then echo GPOINT_AUX_REUSED; else auxNeedsRecreate=1; RELEASE_SHA=${sha} DOCKER_BUILDKIT=1 docker compose -p compose --env-file /opt/gpoint/env/production.env -f /opt/gpoint/compose/docker-compose.yml build --progress=plain gpoint-sensitive-detector > /tmp/gpoint-detector-build-${detectorHash}.log 2>&1; fi; printf 'GPOINT_AUX_NEEDS_RECREATE=%s\\n' \"$auxNeedsRecreate\" > /tmp/gpoint-aux-${sha}; echo GPOINT_STAGE_TIMING=AUX_BUILD_DURATION_MS=$(( $(date +%s%3N) - auxStartMs )); docker inspect compose-api-1 --format '{{.Config.Image}}' > /tmp/gpoint-previous-runtime-${sha} 2>/dev/null || true; set +e; services='api worker bot'; if grep -q 'GPOINT_AUX_NEEDS_RECREATE=1' /tmp/gpoint-aux-${sha}; then services=\"$services gpoint-sensitive-detector\"; fi; docker compose --env-file /opt/gpoint/env/production.env -f /opt/gpoint/compose/docker-compose.yml up -d --force-recreate $services > /tmp/gpoint-compose-switch-${sha}.log 2>&1; composeExit=$?; set -e; echo GPOINT_STAGE_TIMING=RUNTIME_SWITCH_DURATION_MS=$(( $(date +%s%3N) - switchStartMs )); printf 'GPOINT_COMPOSE_EXIT=%s\\n' \"$composeExit\"; mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-compose-switch-${sha}.log /opt/gpoint/release-logs/${sha}/compose-switch.log; tail -80 /tmp/gpoint-compose-switch-${sha}.log`,
      `healthStartMs=$(date +%s%3N); consecutive=0; for attempt in $(seq 1 30); do running=$(docker inspect --format '{{.State.Running}}' compose-api-1 2>/dev/null || echo false); containerHealth=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-api-1 2>/dev/null || echo missing); set +e; curl --connect-timeout 1 --max-time 3 -fsS -o /tmp/gpoint-health-${sha} http://127.0.0.1:3000/health/live; curlExit=$?; set -e; echo GPOINT_HEALTH_ATTEMPT=$attempt RUNNING=$running CONTAINER_HEALTH=$containerHealth CURL_EXIT=$curlExit; if [ "$curlExit" -eq 0 ] && grep -Fq '"status":"ok"' /tmp/gpoint-health-${sha}; then consecutive=$((consecutive + 1)); else consecutive=0; fi; if [ "$consecutive" -ge 3 ]; then echo GPOINT_DEPLOY_OK; break; fi; sleep 2; done; echo GPOINT_STAGE_TIMING=CONTAINER_HEALTH_WAIT_DURATION_MS=$(( $(date +%s%3N) - healthStartMs )); rm -f /tmp/gpoint-health-${sha}; test "$consecutive" -ge 3`,
      'echo GPOINT_DEPLOY_OK'
    ];
    const preparationOutput = stage(timings, 'PRE_SWITCH_PREPARATION', () => {
      const output = run(preparation.join('; '));
      if (!output.includes('GPOINT_PRE_SWITCH_ARTIFACTS_READY')) throw new Error('pre-switch artifact preparation did not complete');
      return output;
    });
    effectiveConfigSha256 = preparationOutput.match(/GPOINT_EFFECTIVE_CONFIG_SHA256=([0-9a-f]{64})/)?.[1];
    if (!effectiveConfigSha256) throw new Error('final effective production config evidence absent');
    onBeforeMutation('agata-start');
    onAgataStart();
    const agataReady = stage(timings, 'AGATA_PRE_SWITCH_READINESS', () => run(agataPreSwitchReadinessCommand(sha, provider)));
    if (!agataReady.includes('AGATA_PRE_SWITCH_READY') || !agataReady.includes(`AGATA_PROVIDER_VERIFIED=${provider}`) || !agataReady.includes('GPOINT_AGATA_INTERNAL_AUTH_OK')) throw new Error('AGATA pre-switch readiness/auth gate failed');
    onBeforeMutation('gsales-start');
    const gsalesStarted = stage(timings, 'GSALES_PRE_SWITCH_START', () => run(gsalesStartCommand(sha)));
    if (!gsalesStarted.includes('GSALES_STARTED')) throw new Error('GSales lane start failed');
    onBeforeMutation('runtime-switch');
    onSwitchStart();
    const deployOutput = run(deploy.join('; ')); applyRemoteTimingMarkers(timings, deployOutput); if (!deployOutput.includes('GPOINT_DEPLOY_OK')) throw new Error('remote runtime deploy failed');
    const composeExitCode = Number(deployOutput.match(/GPOINT_COMPOSE_EXIT=(\d+)/)?.[1] ?? NaN);
    const identityOutput = stage(timings, 'POST_SWITCH_RECONCILIATION', () => run(`set -e; imageId=$(cat ${imageProof}); apiImage=$(docker inspect compose-api-1 --format '{{.Image}}'); workerImage=$(docker inspect compose-worker-1 --format '{{.Image}}'); botImage=$(docker inspect compose-bot-1 --format '{{.Image}}'); detectorImage=$(docker inspect compose-gpoint-sensitive-detector-1 --format '{{.Config.Image}}'); indexSha256=$(sha256sum /opt/gpoint/app/miniapp/index.html | awk '{print $1}'); grep -F -q 'gpoint-build-sha" content="${sha}' /opt/gpoint/app/miniapp/index.html; test "$apiImage" = "$imageId"; test "$workerImage" = "$imageId"; test "$botImage" = "$imageId"; test "$detectorImage" = '${detectorTag}'; docker rm gpoint-static-${sha.slice(0, 8)} >/dev/null 2>&1 || true; printf 'IMAGE_ID=%s INDEX_SHA256=%s BUILD_SHA=%s API_IMAGE=%s WORKER_IMAGE=%s BOT_IMAGE=%s DETECTOR_IMAGE=%s\\n' "$imageId" "$indexSha256" '${sha}' "$apiImage" "$workerImage" "$botImage" "$detectorImage"`)).trim();
    const identityLine = identityOutput.split(/\\r?\\n/).find(line => line.includes('IMAGE_ID=')) ?? '';
    const imageId = identityLine.match(/IMAGE_ID=(sha256:[a-f0-9]+)/)?.[1];
    const indexSha256 = identityLine.match(/INDEX_SHA256=([a-f0-9]{64})/)?.[1];
    const buildSha = identityLine.match(/BUILD_SHA=([0-9a-f]{40})/)?.[1];
    const runtimeFailures = validateRuntimeIdentity(sha, { imageId, buildSha, apiImage: identityOutput.match(/API_IMAGE=(sha256:[a-f0-9]+)/)?.[1], workerImage: identityOutput.match(/WORKER_IMAGE=(sha256:[a-f0-9]+)/)?.[1], botImage: identityOutput.match(/BOT_IMAGE=(sha256:[a-f0-9]+)/)?.[1] });
    const postSwitch = classifyPostSwitchState({ candidate: sha, runtime: { imageId, buildSha, apiImage: identityOutput.match(/API_IMAGE=(sha256:[a-f0-9]+)/)?.[1], workerImage: identityOutput.match(/WORKER_IMAGE=(sha256:[a-f0-9]+)/)?.[1], botImage: identityOutput.match(/BOT_IMAGE=(sha256:[a-f0-9]+)/)?.[1] }, composeExitCode: Number.isNaN(composeExitCode) ? 1 : composeExitCode, health: true });
    if (!imageId || !indexSha256 || runtimeFailures.length) throw new Error(`remote runtime identity verification failed: ${runtimeFailures.join('; ') || 'missing identity fields'}: ${identityOutput}`);
    const agataIdentityOutput = stage(timings, 'AGATA_POST_SWITCH_RECONCILIATION', () => run(`set -e; imageId=$(cat ${agataImageProof}); apiImage=$(docker inspect compose-agata-api-1 --format '{{.Image}}'); workerImage=$(docker inspect compose-agata-memory-worker-1 --format '{{.Image}}'); apiRunning=$(docker inspect compose-agata-api-1 --format '{{.State.Running}}'); apiHealth=$(docker inspect compose-agata-api-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'); workerRunning=$(docker inspect compose-agata-memory-worker-1 --format '{{.State.Running}}'); workerHealth=$(docker inspect compose-agata-memory-worker-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'); buildSha=$(docker image inspect gpoint-agata-runtime:${sha} --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'); head=$(printf '%s' '${agataStatusB64}' | base64 -d | docker run --rm -i ${databaseNetworkArgs('gpoint-agata-verify')} --env-file /opt/gpoint/env/production.env --env-file /opt/gpoint/env/migration.env gpoint-agata-runtime:${sha} python - | sed -n 's/^AGATA_HEAD=//p'); printf 'AGATA_IMAGE_ID=%s AGATA_BUILD_SHA=%s AGATA_API_IMAGE=%s AGATA_API_RUNNING=%s AGATA_API_HEALTH=%s AGATA_WORKER_IMAGE=%s AGATA_WORKER_RUNNING=%s AGATA_WORKER_HEALTH=%s AGATA_HEAD=%s\\n' "$imageId" "$buildSha" "$apiImage" "$apiRunning" "$apiHealth" "$workerImage" "$workerRunning" "$workerHealth" "$head"`)).trim();
    const agataLine = agataIdentityOutput.split(/\r?\n/).find(line => line.includes('AGATA_IMAGE_ID=')) ?? '';
    const agataRuntime = parseAgataRuntimeIdentityLine(agataLine);
    const agataFailures = validateAgataRuntimeIdentity(sha, { imageId: agataRuntime.imageId, buildSha: agataRuntime.buildSha, apiImage: agataRuntime.apiImage, apiRunning: agataRuntime.apiRunning, apiHealth: agataRuntime.apiHealth, workerImage: agataRuntime.workerImage, workerRunning: agataRuntime.workerRunning, workerHealth: agataRuntime.workerHealth });
    if (agataFailures.length || agataRuntime.head !== agataMigrations.head) throw new Error(`AGATA runtime identity verification failed: ${agataFailures.join('; ') || 'Alembic head mismatch'}: ${agataIdentityOutput}`);
    const providerOutput = stage(timings, 'AGATA_PROVIDER_POST_SWITCH', () => run(agataProviderRuntimeCommand(provider)));
    const providerLine = providerOutput.split(/\r?\n/).find(line => line.includes('AGATA_PROVIDER=')) ?? '';
    const providerRuntime = Object.fromEntries([...providerLine.matchAll(/AGATA_([A-Z_]+)=([^\s]+)/g)].map(match => [match[1], match[2]]));
    if (providerRuntime.PROVIDER !== provider) throw new Error('post-switch AGATA provider mismatch');
    if (provider === 'mac_worker' && (providerRuntime.CODEX_LOCAL_ENABLED !== 'true' || providerRuntime.WORKER_TOKEN_PRESENT !== 'YES')) throw new Error('post-switch mac_worker config is incomplete');
    if (provider === 'disabled' && providerRuntime.CODEX_LOCAL_ENABLED !== 'false') throw new Error('post-switch disabled-provider config is incomplete');
    agataRuntime.provider = providerRuntime.PROVIDER;
    agataRuntime.codexLocalEnabled = providerRuntime.CODEX_LOCAL_ENABLED;
    agataRuntime.workerTokenPresent = providerRuntime.WORKER_TOKEN_PRESENT === 'YES';
    const gsalesIdentity = stage(timings, 'GSALES_POST_SWITCH_RECONCILIATION', () => parseGsalesRuntime(run(gsalesRuntimeCommand(sha, gsalesMigrations)), sha, gsalesMigrations));
    if (imageTransport === 'ghcr-oci') {
      const parity = stage(timings, 'GHCR_POST_SWITCH_PARITY', () => run(createOciVerifyScript(bundle, sha)));
      if (!parity.includes('GPOINT_GHCR_POST_SWITCH_PARITY=YES'))
        throw new Error('post-switch GHCR digest parity gate failed');
      bundle.ghcrPostSwitchVerified = true;
    }
    const removableShas = [...new Set(retentionPlan.removableShas ?? [])].filter(candidate => /^[0-9a-f]{40}$/.test(candidate) && candidate !== sha).join(' ');
    const terminalCleanup = removableShas ? removableShas.split(' ').map(candidate =>
      `for ref in gpoint-runtime:${candidate} gpoint-agata-runtime:${candidate} gpoint-g-sales-runtime:${candidate}; do for status in created exited; do docker ps -aq --filter ancestor="$ref" --filter status="$status" | xargs -r docker rm >/dev/null; done; done; if [ -z "$(docker ps -aq --filter ancestor=gpoint-runtime:${candidate})" ] && [ -z "$(docker ps -aq --filter ancestor=gpoint-agata-runtime:${candidate})" ] && [ -z "$(docker ps -aq --filter ancestor=gpoint-g-sales-runtime:${candidate})" ]; then docker image rm gpoint-runtime:${candidate} gpoint-agata-runtime:${candidate} gpoint-g-sales-runtime:${candidate} >/dev/null 2>&1 || true; rm -f /tmp/gpoint-${candidate}.tar.gz /tmp/gpoint-compose-${candidate}.yml /tmp/gpoint-caddy-${candidate} /tmp/gpoint-dockerfile-${candidate} /tmp/gpoint-agata-dockerfile-${candidate} /tmp/gpoint-safety-dockerfile-${candidate} /tmp/gpoint-safety-config-${candidate}.mjs /tmp/gpoint-image-id-${candidate} /tmp/gpoint-agata-image-id-${candidate} /tmp/gpoint-g-sales-image-id-${candidate} /tmp/gpoint-previous-runtime-${candidate} /tmp/gpoint-aux-${candidate}; fi`
    ).join('; ') : 'true';
    const cacheCleanup = removableShas
      ? removableShas.split(' ').map(candidateBuildCacheCleanupCommand).join('; ')
      : "echo GPOINT_BUILD_CACHE_PRESERVED reason=no_terminal_candidate_cache";
    const cleanup = stage(timings, 'POST_RELEASE_CLEANUP', () => runCleanupCommand(`set -e; test "$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1)" = '${sha}'; ${terminalCleanup}; ${cacheCleanup}; rm -f /tmp/gpoint-previous-runtime-${sha} /tmp/gpoint-aux-${sha}; echo GPOINT_POST_RELEASE_CLEANUP_OK`));
    if (!cleanup.includes('GPOINT_POST_RELEASE_CLEANUP_OK')) throw new Error('post-release cleanup failed');
    const migrationOutput = [
      migration.includes('Database schema is up to date!') ? 'DATABASE_SCHEMA_UP_TO_DATE' : null,
      migration.includes('No pending migrations to apply.') ? 'NO_PENDING_MIGRATIONS' : null,
      migration.includes('Applying migration') ? 'PENDING_MIGRATION_APPLIED' : null,
      migration.includes('AGATA_SCHEMA_ABSENT') ? 'AGATA_SCHEMA_CREATED' : null,
      migration.match(/AGATA_HEAD=([^\s]+)/)?.[1] ? `AGATA_HEAD=${migration.match(/AGATA_HEAD=([^\s]+)/)[1]}` : null,
      migration.includes('Running upgrade') ? 'AGATA_MIGRATION_APPLIED' : null,
      migration.match(/GPOINT_BACKUP_PATH=([^\s]+)/)?.[1] ? `BACKUP=${migration.match(/GPOINT_BACKUP_PATH=([^\s]+)/)[1]}` : null,
    ].filter(Boolean).join(',');
    if (!migrationOutput) throw new Error('migration gate output was not recognized');
    return { imageIdentity: { imageId, indexSha256, buildSha, detectorHash }, agataIdentity: agataRuntime, gsalesIdentity, effectiveConfigSha256, migrationOutput, migrationProof, composeExitCode, postSwitch, detectorHash, timings, ciReceipt: bundle ? { ...bundle, archive: undefined, selectedTransport: imageTransport } : null };
  } finally {
    try { cleanupTemporaryWorktree(checkout); }
    finally { fs.rmSync(archive, { force: true }); }
  }
}
