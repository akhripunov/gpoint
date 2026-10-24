import crypto from 'node:crypto';

export const PILOT_PROJECT = 'gpoint-eros-pilot';
export const PILOT_ENV_FILE = '/opt/gpoint/env/eros-pilot.env';
export const PILOT_BACKUP_DIR = '/opt/gpoint/backups/eros-pilot';
export const PILOT_SERVICES = Object.freeze([
  'eros-pilot-api', 'postgres', 'eros-migrate', 'eros-pilot-embedding-relay',
  'ollama-model-verify', 'eros',
]);
export const REQUIRED_PILOT_ENV = Object.freeze([
  'EROS_PILOT_GPOINT_API_KEY', 'EROS_PILOT_BRIDGE_SIGNING_SECRET',
  'EROS_PILOT_TENANT_ID', 'EROS_PILOT_TEST_USER_ID',
  'EROS_PILOT_TEST_CREATOR_ID', 'EROS_PILOT_POSTGRES_ADMIN_PASSWORD',
  'EROS_PILOT_MIGRATOR_PASSWORD', 'EROS_PILOT_RUNTIME_PASSWORD',
  'EROS_PILOT_SUPABASE_JWT_SECRET',
]);

export function parsePilotEnvironment(encoded) {
  if (typeof encoded !== 'string' || !encoded) throw new Error('Protected Eros Pilot environment secret is missing');
  const canonical = encoded.replace(/\s/g, '');
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(canonical)) throw new Error('Protected Eros Pilot environment secret is invalid');
  let text;
  try { text = Buffer.from(canonical, 'base64').toString('utf8'); }
  catch { throw new Error('Protected Eros Pilot environment secret is invalid'); }
  if (!text || text.includes('\0') || Buffer.from(text).toString('base64') !== canonical)
    throw new Error('Protected Eros Pilot environment secret is not canonical base64');
  const values = new Map();
  for (const line of text.split(/\r?\n/)) {
    if (!line || line.startsWith('#')) continue;
    const match = line.match(/^([A-Z][A-Z0-9_]*)=(.*)$/);
    if (!match || values.has(match[1])) throw new Error('Protected Eros Pilot environment has invalid or duplicate entries');
    values.set(match[1], match[2]);
  }
  for (const key of REQUIRED_PILOT_ENV) if (!values.get(key)) throw new Error(`Protected Eros Pilot environment is missing ${key}`);
  for (const [key, value] of values) {
    if (!key.startsWith('EROS_PILOT_')) throw new Error('Protected Eros Pilot environment contains an out-of-scope variable');
    if (!/^[A-Za-z0-9_./:@+=-]{16,512}$/.test(value))
      throw new Error('Protected Eros Pilot environment contains a value unsafe for a Compose dotenv file');
  }
  for (const key of ['EROS_PILOT_TENANT_ID', 'EROS_PILOT_TEST_USER_ID', 'EROS_PILOT_TEST_CREATOR_ID']) {
    if (!/^[a-f0-9-]{16,64}$/i.test(values.get(key))) throw new Error(`Protected Eros Pilot ${key} must be a synthetic identifier`);
  }
  return Object.fromEntries(values);
}

export function pilotEnvironmentText(environment, {sourceSha, erosImage, runtimeImageSha}) {
  if (!/^[a-f0-9]{40}$/.test(sourceSha ?? '') || !/^ghcr\.io\/akhripunov\/gpoint-eros-pilot@sha256:[a-f0-9]{64}$/.test(erosImage ?? '')
      || !/^[a-f0-9]{40}$/.test(runtimeImageSha ?? '')) throw new Error('Exact pilot SHA and immutable image identities are required');
  const entries = {...environment, EROS_PILOT_SOURCE_SHA: sourceSha, EROS_PILOT_IMAGE: erosImage,
    GPOINT_APP_IMAGE_SHA: runtimeImageSha};
  return `${Object.keys(entries).sort().map(key => `${key}=${entries[key]}`).join('\n')}\n`;
}

export function assertPilotCapacity({availableMemoryKiB, freeDiskKiB, minimumAvailableMemoryKiB = 1408 * 1024,
  minimumFreeDiskKiB = 5120 * 1024}) {
  if (!Number.isSafeInteger(availableMemoryKiB) || availableMemoryKiB < minimumAvailableMemoryKiB)
    throw new Error('Fresh VPS memory headroom is below the Eros Pilot minimum');
  if (!Number.isSafeInteger(freeDiskKiB) || freeDiskKiB < minimumFreeDiskKiB)
    throw new Error('Fresh VPS free disk is below the Eros Pilot minimum');
  return {availableMemoryKiB, freeDiskKiB, status: 'PASS'};
}

export function pilotComposeArguments(sourceSha, envPath = PILOT_ENV_FILE) {
  if (!/^[a-f0-9]{40}$/.test(sourceSha ?? '') || envPath !== PILOT_ENV_FILE)
    throw new Error('Pilot Compose must use the exact source SHA and protected pilot environment path');
  return `docker compose -p ${PILOT_PROJECT} --env-file /opt/gpoint/env/production.env --env-file ${PILOT_ENV_FILE} -f /opt/gpoint/compose/docker-compose.yml -f /opt/gpoint/releases/${sourceSha}/source/infra/production/docker-compose.eros-pilot.yml --profile eros-pilot`;
}

export function pilotStartCommand(sourceSha) {
  const compose = pilotComposeArguments(sourceSha);
  return `set -euo pipefail; ${compose} config --quiet; ${compose} up -d ${PILOT_SERVICES.join(' ')}`;
}

export function executePilotApply({run, sourceSha, beforeSnapshot, healthCommand, preApplySnapshotCommand = pilotStateFingerprintCommand(),
  postSnapshotCommand}) {
  if (typeof run !== 'function' || !/^[a-f0-9]{64}$/.test(beforeSnapshot ?? '') ||
      typeof healthCommand !== 'string' || !healthCommand || typeof postSnapshotCommand !== 'string' || !postSnapshotCommand)
    throw new Error('Exact pilot deployment preflight and health evidence are required');
  validatePilotServiceSelection(PILOT_SERVICES);
  const compose = pilotComposeArguments(sourceSha);
  let applyStarted = false;
  try {
    run(`${compose} config --quiet`);
    const preApplyOutput = String(run(preApplySnapshotCommand));
    const preApplySnapshot = preApplyOutput.match(/^EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=([a-f0-9]{64})$/m)?.[1];
    if (!preApplySnapshot || preApplySnapshot !== beforeSnapshot)
      throw new Error('Existing production Compose container fingerprint changed before pilot Apply');
    const preApplyFacts = Object.fromEntries([...preApplyOutput.matchAll(/^(EROS_PILOT_[A-Z0-9_]+)=([^\s]+)$/gm)]
      .map(([, key, value]) => [key, value]));
    assertPilotCapacity({availableMemoryKiB: Number(preApplyFacts.EROS_PILOT_AVAILABLE_MEMORY_KIB),
      freeDiskKiB: Number(preApplyFacts.EROS_PILOT_FREE_DISK_KIB)});
    applyStarted = true;
    run(pilotStartCommand(sourceSha));
    const health = run(healthCommand);
    if (!String(health).includes('EROS_PILOT_HEALTH=PASS')) throw new Error('Eros Pilot health/readback gate failed');
    const postOutput = String(run(postSnapshotCommand));
    const postSnapshot = postOutput.match(/^EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=([a-f0-9]{64})$/m)?.[1];
    if (!postSnapshot || postSnapshot !== beforeSnapshot)
      throw new Error('Existing production Compose container fingerprint changed during pilot Apply');
    const postFacts = Object.fromEntries([...postOutput.matchAll(/^(EROS_PILOT_[A-Z0-9_]+)=([^\s]+)$/gm)]
      .map(([, key, value]) => [key, value]));
    assertPilotCapacity({availableMemoryKiB: Number(postFacts.EROS_PILOT_AVAILABLE_MEMORY_KIB),
      freeDiskKiB: Number(postFacts.EROS_PILOT_FREE_DISK_KIB),
      minimumAvailableMemoryKiB: 256 * 1024});
    const memoryUsage = String(health).split(/\r?\n/).filter(line => /^gpoint-eros-pilot-/.test(line));
    if (memoryUsage.length !== 4) throw new Error('Actual Eros/API/PostgreSQL/relay memory readback is incomplete');
    return {memoryUsage, productionSnapshotSha256: postSnapshot,
      postMemoryAvailableKiB: Number(postFacts.EROS_PILOT_AVAILABLE_MEMORY_KIB),
      postDiskFreeKiB: Number(postFacts.EROS_PILOT_FREE_DISK_KIB)};
  } catch (error) {
    error.pilotApplyStarted = applyStarted;
    if (applyStarted) {
      try {
        run(`${compose} stop ${PILOT_SERVICES.join(' ')}`);
        error.rollbackStatus = 'PASS_STOPPED_ONLY_PILOT_SERVICES';
      } catch (rollbackError) {
        error.rollbackStatus = 'FAILED_RECONCILIATION_REQUIRED';
        error.rollbackFailure = String(rollbackError?.message ?? 'pilot rollback failed').slice(0, 250);
      }
    }
    throw error;
  }
}

export function pilotBackupCommand(sourceSha) {
  const compose = pilotComposeArguments(sourceSha);
  return `set -euo pipefail; umask 077; install -d -m 700 ${PILOT_BACKUP_DIR}; stamp=$(date -u +%Y%m%dT%H%M%SZ); backup=${PILOT_BACKUP_DIR}/eros-pilot-$stamp.dump; env_snapshot=${PILOT_BACKUP_DIR}/eros-pilot-env-$stamp.env; install -m 600 ${PILOT_ENV_FILE} "$env_snapshot"; ${compose} exec -T postgres pg_dump -U eros_pilot_admin -d eros_pilot -Fc > "$backup"; ${compose} exec -T postgres pg_restore --list - < "$backup" >/dev/null; sha256sum "$backup" > "$backup.sha256"; chmod 600 "$backup" "$backup.sha256"; printf 'EROS_PILOT_BACKUP=%s\\n' "$backup"`;
}

export function pilotStateFingerprintCommand() {
  return `set -euo pipefail; containers=$(docker ps -aq --filter label=com.docker.compose.project=compose | sort); test -n "$containers"; for name in compose-api-1 compose-worker-1 compose-bot-1 compose-agata-api-1 compose-agata-memory-worker-1 compose-gsales-api-1 compose-gsales-agata-api-1 compose-gsales-agata-memory-worker-1; do state=$(docker inspect "$name" --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null) || { echo GPOINT_PRODUCTION_SERVICE_MISSING=$name; exit 42; }; test "$state" = 'true healthy' || { echo GPOINT_PRODUCTION_SERVICE_UNHEALTHY=$name; exit 42; }; done; records=$(docker inspect $containers --format '{{.Id}} {{.Image}} {{.State.Running}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' | sort); printf 'EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=%s\\n' "$(printf '%s\\n' "$records" | sha256sum | awk '{print $1}')"; printf 'EROS_PILOT_AVAILABLE_MEMORY_KIB=%s\\n' "$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"; printf 'EROS_PILOT_FREE_DISK_KIB=%s\\n' "$(df -Pk /opt/gpoint | awk 'NR==2 {print $4}')"`;
}

export function validatePilotServiceSelection(services) {
  if (!Array.isArray(services) || services.length !== PILOT_SERVICES.length
      || new Set(services).size !== PILOT_SERVICES.length
      || services.some(service => !PILOT_SERVICES.includes(service)))
    throw new Error('Pilot Apply service selection escaped the Eros Pilot allowlist');
  return true;
}

export function deploymentEvidenceSha256(payload) {
  return crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
}
