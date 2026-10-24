#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {verifyReleaseBundle, selectImageTransport} from './release-bundle.mjs';
import {verifyOciReadAccess, createErosPilotOciPullScript} from './release-oci.mjs';
import {runRemoteCommand, runRemoteCommandWithInput, upload} from './release-gpoint-vps-adapter.mjs';
import {acquireReleaseLock} from './release-lock.mjs';
import {assertPilotCapacity, parsePilotEnvironment, pilotEnvironmentText,
  pilotStateFingerprintCommand, validatePilotServiceSelection, executePilotApply, PILOT_SERVICES} from './eros-pilot-deployment.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const sourceFiles = [
  'infra/production/docker-compose.eros-pilot.yml',
  'infra/production/eros-pilot-init.sh',
  'services/eros-engine/eros-pilot-embedding-relay.mjs',
  'services/eros-engine/verify-eros-pilot-model.mjs',
  'services/eros-engine/eros-pilot-model-config.toml',
];
const sha = process.env.CANDIDATE_SHA;
const releaseMode = process.env.RELEASE_MODE;

function requireOwnerAuthorization() {
  if (releaseMode !== 'eros-pilot' || process.env.APPROVE_EROS_PILOT !== 'true' ||
      process.env.APPROVE_PRODUCTION === 'true' || process.env.TASK_AUTHORIZATION_ID ||
      process.env.OWNER_REPOSITORY !== 'akhripunov' ||
      process.env.OWNER_APPROVAL_ACTOR !== process.env.OWNER_REPOSITORY ||
      process.env.OWNER_TRIGGERING_ACTOR !== process.env.OWNER_REPOSITORY)
    throw new Error('Eros Pilot requires its own exact-SHA repository-owner approval');
  if (!/^[a-f0-9]{40}$/.test(sha ?? '') || !/^[0-9]+$/.test(process.env.PREPARE_RUN_ID ?? '') ||
      !/^[0-9]+$/.test(process.env.PREPARE_RUN_ATTEMPT ?? '')) throw new Error('Exact candidate and Prepare run identity are required');
  if (!process.env.GITHUB_RUN_ID || !process.env.RELEASE_RESULT || !process.env.GPOINT_RELEASE_BUNDLE)
    throw new Error('V5 run evidence paths are required');
}

function parseRemoteFacts(output) {
  const facts = {};
  for (const line of String(output).split(/\r?\n/)) {
    const match = line.match(/^(EROS_PILOT_[A-Z0-9_]+)=([^\s]+)$/);
    if (match) facts[match[1]] = match[2];
  }
  return facts;
}

function inspectCommand(sourceSha, {allowPilotEnvironment = false} = {}) {
  if (!/^[a-f0-9]{40}$/.test(sourceSha)) throw new Error('Exact pilot SHA required');
  const baseline = pilotStateFingerprintCommand();
  const pilotState = allowPilotEnvironment
    ? 'test -r /opt/gpoint/env/eros-pilot.env;'
    : 'pilotContainers=$(docker ps -aq --filter label=com.docker.compose.project=gpoint-eros-pilot); pilotVolume=$(docker volume ls -q --filter name=^gpoint_eros_pilot_pgdata$); if [ -n "$pilotContainers" ] || [ -n "$pilotVolume" ] || [ -e /opt/gpoint/env/eros-pilot.env ]; then echo EROS_PILOT_EXISTING_STATE=YES; exit 41; fi;';
  return `set -euo pipefail; test -r /opt/gpoint/env/production.env; docker compose version >/dev/null; command -v curl >/dev/null; \
    ${pilotState} \
    ${baseline}; \
    avail=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo); disk=$(df -Pk /opt/gpoint | awk 'NR==2 {print $4}'); \
    printf 'EROS_PILOT_AVAILABLE_MEMORY_KIB=%s\\nEROS_PILOT_FREE_DISK_KIB=%s\\n' "$avail" "$disk"; \
    tunnel=$(curl --silent --show-error --fail --connect-timeout 3 --max-time 8 http://127.0.0.1:11435/api/tags); \
    printf '%s' "$tunnel" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert any("qwen3-embedding:0.6b" in x.get("name", "") for x in d.get("models", []))'; \
    echo EROS_PILOT_MAC_OLLAMA_TUNNEL=PASS; echo EROS_PILOT_PREDEPLOY=PASS`;
}

function projectContainerSnapshot(output) {
  const match = String(output).match(/^EROS_PILOT_PRODUCTION_CONTAINER_SNAPSHOT_SHA256=([a-f0-9]{64})$/m);
  if (!match) throw new Error('Existing GPoint container snapshot is missing');
  return match[1];
}

function sourceArchive(directory) {
  const archive = execFileSync('git', ['archive', '--format=tar', sha, '--', ...sourceFiles], {
    cwd: directory, encoding: 'buffer', maxBuffer: 64 * 1024 * 1024,
  });
  return zlib.gzipSync(archive);
}

function writeEvidence(record) {
  const temp = path.resolve(process.env.RUNNER_TEMP ?? os.tmpdir());
  const requested = path.resolve(process.env.RELEASE_RESULT);
  if (!requested.startsWith(`${temp}${path.sep}`) || path.dirname(requested) !== temp || !fs.existsSync(temp))
    throw new Error('Unsafe Eros Pilot evidence destination');
  const realTemp = fs.realpathSync(temp);
  const file = path.join(realTemp, path.basename(requested));
  if (fs.existsSync(file) || fs.lstatSync(realTemp).isSymbolicLink()) throw new Error('Unsafe Eros Pilot evidence destination');
  fs.writeFileSync(file, `${JSON.stringify(record, null, 2)}\n`, {flag: 'wx', mode: 0o600});
}

function writeStepOutputs(record) {
  if (!process.env.GITHUB_OUTPUT) return;
  fs.appendFileSync(process.env.GITHUB_OUTPUT,
    `status=${record.status}\npreflight=${record.vpsPreflight ? 'PASS' : 'BLOCKED'}\n`, {mode: 0o600});
}

export async function runErosPilotDeploy({remote = runRemoteCommand, remoteWithInput = runRemoteCommandWithInput,
  uploadFile = upload, env = process.env, checkout = root, now = () => new Date().toISOString()} = {}) {
  requireOwnerAuthorization();
  const secretEnvironment = parsePilotEnvironment(env.EROS_PILOT_ENV_B64);
  const proof = verifyReleaseBundle(env.GPOINT_RELEASE_BUNDLE, sha, env.GHCR_RECEIPT_DIGEST);
  selectImageTransport(proof, 'ghcr-oci');
  if (proof.staticOnly === true || proof.candidateSha !== sha || !proof.pilotImages?.eros ||
      proof.images?.app?.sourceSha !== sha || proof.images?.app?.repository !== 'ghcr.io/akhripunov/gpoint-runtime' ||
      proof.gates?.erosPilotImage !== 'PASS' || proof.gates?.erosPilotGhcrReadback !== 'PASS')
    throw new Error('Prepare receipt has no exact stock Eros Pilot and GPoint API image proof');
  const environmentText = pilotEnvironmentText(secretEnvironment, {
    sourceSha: sha, runtimeImageSha: sha, erosImage: proof.pilotImages.eros.reference,
  });
  await verifyOciReadAccess(proof, env.GHCR_READ_USERNAME, env.GHCR_READ_TOKEN);
  const git = args => execFileSync('git', args, {cwd: checkout, encoding: 'utf8'}).trim();
  if (git(['rev-parse', 'HEAD']) !== sha || git(['status', '--porcelain']))
    throw new Error('Eros Pilot executor checkout must be clean at the exact candidate SHA');
  validatePilotServiceSelection(PILOT_SERVICES);
  const lockFile = path.join(os.homedir(), '.config/gpoint/release-production.lock');
  const legacyLock = path.join(checkout, '.release-production.lock');
  const releaseId = `eros-pilot-${sha}-${env.GITHUB_RUN_ID}-${env.GITHUB_RUN_ATTEMPT}`;
  const release = acquireReleaseLock(lockFile, releaseId, legacyLock);
  const evidence = {schema: 'gpoint-eros-pilot-deployment/v1', status: 'BLOCKED', candidateSha: sha,
    prepareRunId: env.PREPARE_RUN_ID, prepareRunAttempt: env.PREPARE_RUN_ATTEMPT,
    erosImageDigest: proof.pilotImages.eros.digest, gpointApiImageDigest: proof.images.app.digest,
    startedAt: now(), applyStarted: false};
  let productionSnapshotBefore = '';
  let pilotEnvInstallAttempted = false;
  try {
    const before = remote(inspectCommand(sha));
    const facts = parseRemoteFacts(before);
    const capacity = assertPilotCapacity({availableMemoryKiB: Number(facts.EROS_PILOT_AVAILABLE_MEMORY_KIB),
      freeDiskKiB: Number(facts.EROS_PILOT_FREE_DISK_KIB)});
    productionSnapshotBefore = projectContainerSnapshot(before);
    evidence.vpsPreflight = {memoryAvailableKiB: capacity.availableMemoryKiB,
      diskFreeKiB: capacity.freeDiskKiB, macOllamaTunnel: 'PASS', existingPilotState: 'NONE'};
    const pullScript = createErosPilotOciPullScript(proof, sha, {username: env.GHCR_READ_USERNAME || 'akhripunov'});
    const pull = remoteWithInput(pullScript, env.GHCR_READ_TOKEN);
    if (!pull.includes('EROS_PILOT_GHCR_IMAGE_VERIFIED=YES')) throw new Error('Exact stock Eros GHCR readback failed on VPS');
    const artifactPath = path.join(os.tmpdir(), `gpoint-eros-pilot-source-${sha}-${process.pid}.tar.gz`);
    try {
      const archiveBytes = sourceArchive(checkout);
      fs.writeFileSync(artifactPath, archiveBytes, {mode: 0o600, flag: 'wx'});
      const archiveSha256 = crypto.createHash('sha256').update(archiveBytes).digest('hex');
      remote(`set -euo pipefail; install -d -m 755 /opt/gpoint/releases/${sha}/source; install -d -m 700 /opt/gpoint/compose`);
      uploadFile(artifactPath, `/tmp/gpoint-eros-pilot-source-${sha}.tar.gz`);
      remote(`set -euo pipefail; test "$(sha256sum /tmp/gpoint-eros-pilot-source-${sha}.tar.gz | cut -d' ' -f1)" = '${archiveSha256}'; tar -xzf /tmp/gpoint-eros-pilot-source-${sha}.tar.gz --no-same-owner -C /opt/gpoint/releases/${sha}/source; rm -f /tmp/gpoint-eros-pilot-source-${sha}.tar.gz`);
    } finally { fs.rmSync(artifactPath, {force: true}); }
    const deployEnv = pilotEnvironmentText(secretEnvironment, {
      sourceSha: sha, runtimeImageSha: sha, erosImage: proof.pilotImages.eros.reference,
    });
    pilotEnvInstallAttempted = true;
    remoteWithInput(`python3 -c 'import os,sys; p="/opt/gpoint/env/eros-pilot.env"; fd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); f=os.fdopen(fd,"wb"); f.write(sys.stdin.buffer.read()); f.flush(); os.fsync(f.fileno()); f.close()'`, deployEnv);
    const healthCommand = `set -euo pipefail; for i in $(seq 1 120); do ok=1; for s in eros-pilot-api postgres eros-pilot-embedding-relay eros; do name=gpoint-eros-pilot-$s-1; state=$(docker inspect "$name" --format '{{.State.Running}}:{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo missing); [ "$state" = 'true:healthy' ] || ok=0; done; for s in eros-migrate ollama-model-verify; do name=gpoint-eros-pilot-$s-1; code=$(docker inspect "$name" --format '{{.State.ExitCode}}' 2>/dev/null || echo missing); [ "$code" = 0 ] || ok=0; done; [ "$ok" = 1 ] && break; sleep 2; done; test "$ok" = 1; docker exec gpoint-eros-pilot-eros-1 curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null; docker exec gpoint-eros-pilot-eros-pilot-api-1 node -e 'if(process.env.EROS_OLLAMA_EMBEDDING_URL!=="http://ollama:11434/api/embed")process.exit(1)'; echo EROS_PILOT_HEALTH=PASS; docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' gpoint-eros-pilot-eros-1 gpoint-eros-pilot-postgres-1 gpoint-eros-pilot-eros-pilot-api-1 gpoint-eros-pilot-eros-pilot-embedding-relay-1`;
    let applied;
    try { applied = executePilotApply({run: remote, sourceSha: sha, beforeSnapshot: productionSnapshotBefore,
      healthCommand, postSnapshotCommand: pilotStateFingerprintCommand()}); }
    catch (error) { evidence.applyStarted = error.pilotApplyStarted === true; throw error; }
    const memoryUsage = applied.memoryUsage;
    evidence.status = 'COMPLETE';
    evidence.rollback = 'FIRST_INSTALL_STOP_PILOT_SERVICES_RETAIN_PILOT_VOLUME';
    evidence.health = 'PASS';
    evidence.actualContainerMemory = memoryUsage;
    evidence.vpsPostflight = {memoryAvailableKiB: applied.postMemoryAvailableKiB,
      diskFreeKiB: applied.postDiskFreeKiB};
    evidence.productionContainerSnapshotSha256 = applied.productionSnapshotSha256;
    evidence.finishedAt = now();
    writeEvidence(evidence);
    writeStepOutputs(evidence);
    console.log(`EROS_PILOT_DEPLOY=COMPLETE SHA=${sha} IMAGE=${proof.pilotImages.eros.digest}`);
    return evidence;
  } catch (error) {
    evidence.failure = String(error?.message ?? 'Eros Pilot deployment failed').slice(0, 400);
    if (evidence.applyStarted) {
      evidence.rollback = error.rollbackStatus === 'PASS_STOPPED_ONLY_PILOT_SERVICES'
        ? 'PASS_STOPPED_ONLY_ALLOWLISTED_PILOT_SERVICES_AND_RETAINED_VOLUME'
        : 'FAILED_RECONCILIATION_REQUIRED';
      if (error.rollbackFailure) evidence.rollbackFailure = error.rollbackFailure;
      evidence.status = error.rollbackStatus === 'PASS_STOPPED_ONLY_PILOT_SERVICES' ? 'ROLLED_BACK' : 'RECOVERY_REQUIRED';
    } else if (pilotEnvInstallAttempted) {
      try {
        remote(`set -euo pipefail; if [ -e /opt/gpoint/env/eros-pilot.env ]; then test -z "$(docker ps -aq --filter label=com.docker.compose.project=gpoint-eros-pilot)"; test -z "$(docker volume ls -q --filter name=^gpoint_eros_pilot_pgdata$)"; grep -qx 'EROS_PILOT_SOURCE_SHA=${sha}' /opt/gpoint/env/eros-pilot.env; rm -f /opt/gpoint/env/eros-pilot.env; fi; echo EROS_PILOT_ENV_CLEANUP=PASS`);
        evidence.pilotEnvCleanup = 'PASS';
      } catch (cleanupError) {
        evidence.pilotEnvCleanup = 'FAILED_MANUAL_RECONCILIATION_REQUIRED';
        evidence.status = 'RECOVERY_REQUIRED';
        evidence.cleanupFailure = String(cleanupError?.message ?? 'pilot env cleanup failed').slice(0, 250);
      }
    }
    evidence.finishedAt = now();
    try { writeEvidence(evidence); writeStepOutputs(evidence); } catch {}
    throw error;
  } finally { release(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await runErosPilotDeploy(); }
  catch (error) {
    console.error(`EROS_PILOT_DEPLOY_FAILED=${String(error?.message ?? 'unknown failure').replace(/[\r\n]+/g, ' ').slice(0, 400)}`);
    process.exitCode = 1;
  }
}
