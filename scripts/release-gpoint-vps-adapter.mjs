import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const REPO = 'git@github.com:akhripunov/gpoint.git';

export function validateRuntimeIdentity(candidate, runtime) {
  const failures = [];
  if (runtime?.buildSha !== candidate) failures.push('runtime build SHA mismatch');
  for (const role of ['apiImage', 'workerImage', 'botImage']) {
    if (runtime?.[role] !== runtime?.imageId) failures.push(`${role} does not use candidate image`);
  }
  return failures;
}

export class RemoteCommandError extends Error {
  constructor(command, error) {
    super(`remote command failed (exit=${error.status ?? 'unknown'}): ${command}`);
    this.name = 'RemoteCommandError';
    this.code = 'REMOTE_COMMAND_FAILED';
    this.command = command;
    this.exitCode = Number.isInteger(error.status) ? error.status : null;
    this.stdout = String(error.stdout ?? '');
    this.stderr = String(error.stderr ?? '');
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

function stage(timings, name, action) {
  const started = Date.now();
  console.error(`${name} START`);
  try { return action(); }
  finally { timings[name] = Number(((Date.now() - started) / 1000).toFixed(1)); console.error(`${name} = ${timings[name]}s`); }
}

function run(command, env = {}) {
  const runner = process.env.GPOINT_REMOTE_RUNNER;
  if (!runner) throw new Error('GPOINT_REMOTE_RUNNER is required for apply mode; no implicit SSH credentials are used');
  const captureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-remote-run-'));
  const stdoutFile = path.join(captureDir, 'stdout');
  const stderrFile = path.join(captureDir, 'stderr');
  const stdoutFd = fs.openSync(stdoutFile, 'w');
  const stderrFd = fs.openSync(stderrFile, 'w');
  const started = Date.now();
  console.error(`REMOTE_COMMAND_START elapsed=0s command=${command.split(/\s+/).slice(0, 3).join(' ')}`);
  try {
    const result = spawnSync(runner, [], { env: { ...process.env, ...env, REMOTE_CMD: command }, stdio: ['ignore', stdoutFd, stderrFd] });
    const stdout = fs.readFileSync(stdoutFile, 'utf8');
    const stderr = fs.readFileSync(stderrFile, 'utf8');
    console.error(`REMOTE_COMMAND_DONE elapsed=${((Date.now() - started) / 1000).toFixed(1)}s exit=${result.status ?? 'unknown'}`);
    if (result.status !== 0) {
      const error = new Error('remote runner exited non-zero');
      error.status = result.status;
      error.stdout = stdout;
      error.stderr = stderr;
      throw error;
    }
    return stdout;
  } catch (error) {
    throw new RemoteCommandError(command, error);
  } finally {
    fs.closeSync(stdoutFd);
    fs.closeSync(stderrFd);
    fs.rmSync(captureDir, { recursive: true, force: true });
  }
}

function upload(localFile, remoteFile) {
  const runner = process.env.GPOINT_SCP_RUNNER;
  if (!runner) throw new Error('GPOINT_SCP_RUNNER is required for apply mode; no implicit SSH credentials are used');
  return execFileSync(runner, [], { encoding: 'utf8', env: { ...process.env, LOCAL_FILE: localFile, REMOTE_FILE: remoteFile }, stdio: ['ignore', 'pipe', 'pipe'] });
}

export { run as runRemoteCommand };

function exactCheckout(sha) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-apply-'));
  execFileSync('git', ['init', '-q', dir], { stdio: 'ignore' });
  execFileSync('git', ['remote', 'add', 'origin', REPO], { cwd: dir, stdio: 'ignore' });
  execFileSync('git', ['fetch', '--depth', '1', 'origin', sha], { cwd: dir, stdio: 'ignore' });
  execFileSync('git', ['checkout', '--detach', '-q', 'FETCH_HEAD'], { cwd: dir, stdio: 'ignore' });
  const actual = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir, encoding: 'utf8' }).trim();
  if (actual !== sha) throw new Error('apply checkout SHA mismatch');
  return dir;
}

export function applyCandidate(sha) {
  const checkout = exactCheckout(sha);
  const timings = {};
  const detectorHash = detectorSourceHash(checkout);
  const detectorTag = `gpoint-sensitive-detector:${detectorHash}`;
  const releaseDir = `/opt/gpoint/releases/${sha}`;
  const imageProof = `/tmp/gpoint-image-id-${sha}`;
  const archive = `${checkout}.tar.gz`;
  const compose = path.join(checkout, 'infra/production/docker-compose.yml');
  const dockerfile = path.join(checkout, 'infra/production/Gpoint.Dockerfile');
  const safetyConfig = path.join(checkout, 'infra/production/sensitive-detector/config.mjs');
  const safetyDockerfile = path.join(checkout, 'infra/production/sensitive-detector/Dockerfile');
  const caddyfile = path.join(checkout, 'infra/production/Caddyfile');
  execFileSync('tar', ['-czf', archive, '-C', path.dirname(checkout), path.basename(checkout)]);
  try {
    stage(timings, 'PRODUCTION_PREFLIGHT', () => run("set -e; command -v docker >/dev/null; docker version --format 'DOCKER_VERSION={{.Server.Version}}'; docker compose version; docker buildx version; docker buildx inspect --bootstrap >/dev/null; freeKb=$(df -Pk / | awk 'NR==2 {print $4}'); test -n \"$freeKb\"; test \"$freeKb\" -ge 5242880; test -r /opt/gpoint/env/production.env; currentSha=$(awk -F= '$1==\"RELEASE_SHA\" {print $2}' /opt/gpoint/env/production.env); printf '%s\\n' \"$currentSha\" | grep -Eq '^[0-9a-f]{40}$'; currentImage=$(docker inspect compose-api-1 --format '{{.Config.Image}}'); dbStatus=$(docker run --rm --network gpoint_app --network-alias gpoint-preflight --network shared_internal --env-file /opt/gpoint/env/production.env \"$currentImage\" pnpm --filter @gpoint/db exec prisma migrate status 2>&1); test -n \"$dbStatus\"; printf '%s\\n' \"$dbStatus\" | tail -5; printf 'GPOINT_PREFLIGHT_OK\\n'"));
    stage(timings, 'SOURCE_PREPARE', () => {
      upload(archive, `/tmp/gpoint-${sha}.tar.gz`);
      upload(compose, `/tmp/gpoint-compose-${sha}.yml`);
      upload(dockerfile, `/tmp/gpoint-dockerfile-${sha}`);
      upload(safetyConfig, `/tmp/gpoint-safety-config-${sha}.mjs`);
      upload(safetyDockerfile, `/tmp/gpoint-safety-dockerfile-${sha}`);
      upload(caddyfile, `/tmp/gpoint-caddy-${sha}`);
    });
    const diskGuard = run("set -e; freeKb=$(df -Pk / | awk 'NR==2 {print $4}'); test -n \"$freeKb\"; if [ \"$freeKb\" -lt 8388608 ]; then echo GPOINT_DISK_GUARD_WARN; fi; test \"$freeKb\" -ge 5242880; echo GPOINT_DISK_GUARD_OK");
    if (!diskGuard.includes('GPOINT_DISK_GUARD_OK')) throw new Error('remote disk guard failed: minimum 5 GiB free space required');
    const build = [
      'set -e',
      `rm -rf ${releaseDir}`,
      `mkdir -p ${releaseDir}`,
      `tar -xzf /tmp/gpoint-${sha}.tar.gz -C ${releaseDir}`,
      `mv ${releaseDir}/${path.basename(checkout)} ${releaseDir}/source`,
      `if docker image inspect gpoint-runtime:${sha} >/dev/null 2>&1; then test "$(docker image inspect gpoint-runtime:${sha} --format '{{index .Config.Labels \"org.opencontainers.image.revision\"}}')" = '${sha}'; echo GPOINT_BUILD_REUSED; else DOCKER_BUILDKIT=1 docker build --progress=plain --build-arg GPOINT_BUILD_SHA=${sha} --label org.opencontainers.image.revision=${sha} --label org.opencontainers.image.source=${REPO} -f /tmp/gpoint-dockerfile-${sha} -t gpoint-runtime:${sha} ${releaseDir}/source > /tmp/gpoint-build-${sha}.log 2>&1; fi`,
      `docker image inspect gpoint-runtime:${sha} --format '{{.Id}}' > ${imageProof}`,
      `mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-build-${sha}.log /opt/gpoint/release-logs/${sha}/runtime-build.log 2>/dev/null || true`,
      'echo GPOINT_BUILD_OK'
    ].join('; ');
    const buildOutput = stage(timings, 'RUNTIME_BUILD', () => run(build)); if (!buildOutput.includes('GPOINT_BUILD_OK')) throw new Error('remote image build failed');
    const migration = stage(timings, 'MIGRATION_CHECK', () => run(`set -e; status=$(docker run --rm --network gpoint_app --network-alias gpoint-migration --network shared_internal --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate status 2>&1 || true); printf '%s\\n' "$status"; if printf '%s' "$status" | grep -Eiq 'Database schema is up to date!|No pending migrations to apply\\.'; then echo NO_PENDING_MIGRATIONS; else docker run --rm --network gpoint_app --network-alias gpoint-migration --network shared_internal --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate deploy; fi; echo GPOINT_MIGRATION_OK`));
    if (!migration.includes('GPOINT_MIGRATION_OK')) throw new Error('remote migration deploy failed');
    const deploy = [
      'set -e',
      `cp /tmp/gpoint-compose-${sha}.yml /opt/gpoint/compose/docker-compose.yml`,
      `caddy validate --config /tmp/gpoint-caddy-${sha} --adapter caddyfile`,
      `cp /tmp/gpoint-caddy-${sha} /etc/caddy/Caddyfile`,
      'systemctl reload caddy',
      `mkdir -p /opt/gpoint/compose/sensitive-detector && cp /tmp/gpoint-safety-config-${sha}.mjs /opt/gpoint/compose/sensitive-detector/config.mjs && cp /tmp/gpoint-safety-dockerfile-${sha} /opt/gpoint/compose/sensitive-detector/Dockerfile`,
      `sed -i.bak s/^RELEASE_SHA=.*/RELEASE_SHA=${sha}/ /opt/gpoint/env/production.env`,
      'rm -f /opt/gpoint/env/production.env.bak',
      `sed -i 's#image: gpoint-sensitive-detector:.*#image: ${detectorTag}#' /opt/gpoint/compose/docker-compose.yml`,
      `rm -rf /opt/gpoint/app/miniapp/assets`,
      `docker rm -f gpoint-static-${sha.slice(0, 8)} >/dev/null 2>&1 || true`,
      `docker create --name gpoint-static-${sha.slice(0, 8)} gpoint-runtime:${sha} >/dev/null`,
      `mkdir -p /opt/gpoint/app/miniapp`,
      `docker cp gpoint-static-${sha.slice(0, 8)}:/app/apps/miniapp/dist/. /opt/gpoint/app/miniapp/`,
      'chmod -R a+rX /opt/gpoint/app/miniapp',
      "grep -q '^GPOINT_SAFETY_PROVIDER=' /opt/gpoint/env/production.env && sed -i 's#^GPOINT_SAFETY_PROVIDER=.*#GPOINT_SAFETY_PROVIDER=misskey-sensitive-detector#' /opt/gpoint/env/production.env || printf '%s\\n' 'GPOINT_SAFETY_PROVIDER=misskey-sensitive-detector' >> /opt/gpoint/env/production.env",
      "grep -q '^GPOINT_SAFETY_URL=' /opt/gpoint/env/production.env && sed -i 's#^GPOINT_SAFETY_URL=.*#GPOINT_SAFETY_URL=http://gpoint-sensitive-detector:3009#' /opt/gpoint/env/production.env || printf '%s\\n' 'GPOINT_SAFETY_URL=http://gpoint-sensitive-detector:3009' >> /opt/gpoint/env/production.env",
      "grep -q '^GPOINT_SAFETY_TIMEOUT_MS=' /opt/gpoint/env/production.env && sed -i 's#^GPOINT_SAFETY_TIMEOUT_MS=.*#GPOINT_SAFETY_TIMEOUT_MS=60000#' /opt/gpoint/env/production.env || printf '%s\\n' 'GPOINT_SAFETY_TIMEOUT_MS=60000' >> /opt/gpoint/env/production.env",
      "if ! grep -q '^GPOINT_SAFETY_API_KEY=.' /opt/gpoint/env/production.env; then printf 'GPOINT_SAFETY_API_KEY=%s\\n' \"$(openssl rand -hex 32)\" >> /opt/gpoint/env/production.env; fi",
      `auxNeedsRecreate=0; if docker image inspect ${detectorTag} >/dev/null 2>&1; then echo GPOINT_AUX_REUSED; else auxNeedsRecreate=1; DOCKER_BUILDKIT=1 docker compose --env-file /opt/gpoint/env/production.env -f /opt/gpoint/compose/docker-compose.yml build --progress=plain gpoint-sensitive-detector > /tmp/gpoint-detector-build-${detectorHash}.log 2>&1; fi; printf 'GPOINT_AUX_NEEDS_RECREATE=%s\\n' "$auxNeedsRecreate" > /tmp/gpoint-aux-${sha}`,
      `mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-detector-build-${detectorHash}.log /opt/gpoint/release-logs/${sha}/detector-build.log 2>/dev/null || true`,
      `docker inspect compose-api-1 --format '{{.Config.Image}}' > /tmp/gpoint-previous-runtime-${sha} 2>/dev/null || true`,
      `set +e; services='api worker bot'; if grep -q 'GPOINT_AUX_NEEDS_RECREATE=1' /tmp/gpoint-aux-${sha}; then services="$services gpoint-sensitive-detector"; fi; docker compose --env-file /opt/gpoint/env/production.env -f /opt/gpoint/compose/docker-compose.yml up -d --force-recreate $services > /tmp/gpoint-compose-switch-${sha}.log 2>&1; composeExit=$?; set -e; printf 'GPOINT_COMPOSE_EXIT=%s\\n' "$composeExit"; mkdir -p /opt/gpoint/release-logs/${sha} && cp /tmp/gpoint-compose-switch-${sha}.log /opt/gpoint/release-logs/${sha}/compose-switch.log; tail -80 /tmp/gpoint-compose-switch-${sha}.log`,
      `consecutive=0; for attempt in $(seq 1 30); do running=$(docker inspect --format '{{.State.Running}}' compose-api-1 2>/dev/null || echo false); containerHealth=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' compose-api-1 2>/dev/null || echo missing); set +e; curl --connect-timeout 1 --max-time 3 -fsS -o /tmp/gpoint-health-${sha} http://127.0.0.1:3000/health/live; curlExit=$?; set -e; echo GPOINT_HEALTH_ATTEMPT=$attempt RUNNING=$running CONTAINER_HEALTH=$containerHealth CURL_EXIT=$curlExit; if [ "$curlExit" -eq 0 ] && grep -Fq '"status":"ok"' /tmp/gpoint-health-${sha}; then consecutive=$((consecutive + 1)); else consecutive=0; fi; if [ "$consecutive" -ge 3 ]; then echo GPOINT_DEPLOY_OK; break; fi; sleep 2; done; rm -f /tmp/gpoint-health-${sha}; test "$consecutive" -ge 3`,
      'echo GPOINT_DEPLOY_OK'
    ].join('; ');
    const deployOutput = run(deploy); if (!deployOutput.includes('GPOINT_DEPLOY_OK')) throw new Error('remote runtime deploy failed');
    const composeExitCode = Number(deployOutput.match(/GPOINT_COMPOSE_EXIT=(\d+)/)?.[1] ?? NaN);
    const identityOutput = stage(timings, 'POST_SWITCH_RECONCILIATION', () => run(`set -e; imageId=$(cat ${imageProof}); apiImage=$(docker inspect compose-api-1 --format '{{.Image}}'); workerImage=$(docker inspect compose-worker-1 --format '{{.Image}}'); botImage=$(docker inspect compose-bot-1 --format '{{.Image}}'); detectorImage=$(docker inspect compose-gpoint-sensitive-detector-1 --format '{{.Config.Image}}'); indexSha256=$(sha256sum /opt/gpoint/app/miniapp/index.html | awk '{print $1}'); grep -F -q 'gpoint-build-sha" content="${sha}' /opt/gpoint/app/miniapp/index.html; test "$apiImage" = "$imageId"; test "$workerImage" = "$imageId"; test "$botImage" = "$imageId"; test "$detectorImage" = '${detectorTag}'; docker rm gpoint-static-${sha.slice(0, 8)} >/dev/null 2>&1 || true; printf 'IMAGE_ID=%s INDEX_SHA256=%s BUILD_SHA=%s API_IMAGE=%s WORKER_IMAGE=%s BOT_IMAGE=%s DETECTOR_IMAGE=%s\\n' "$imageId" "$indexSha256" '${sha}' "$apiImage" "$workerImage" "$botImage" "$detectorImage"`)).trim();
    const identityLine = identityOutput.split(/\\r?\\n/).find(line => line.includes('IMAGE_ID=')) ?? '';
    const imageId = identityLine.match(/IMAGE_ID=(sha256:[a-f0-9]+)/)?.[1];
    const indexSha256 = identityLine.match(/INDEX_SHA256=([a-f0-9]{64})/)?.[1];
    const buildSha = identityLine.match(/BUILD_SHA=([0-9a-f]{40})/)?.[1];
    const runtimeFailures = validateRuntimeIdentity(sha, { imageId, buildSha, apiImage: identityOutput.match(/API_IMAGE=(sha256:[a-f0-9]+)/)?.[1], workerImage: identityOutput.match(/WORKER_IMAGE=(sha256:[a-f0-9]+)/)?.[1], botImage: identityOutput.match(/BOT_IMAGE=(sha256:[a-f0-9]+)/)?.[1] });
    const postSwitch = classifyPostSwitchState({ candidate: sha, runtime: { imageId, buildSha, apiImage: identityOutput.match(/API_IMAGE=(sha256:[a-f0-9]+)/)?.[1], workerImage: identityOutput.match(/WORKER_IMAGE=(sha256:[a-f0-9]+)/)?.[1], botImage: identityOutput.match(/BOT_IMAGE=(sha256:[a-f0-9]+)/)?.[1] }, composeExitCode: Number.isNaN(composeExitCode) ? 1 : composeExitCode, health: true });
    if (!imageId || !indexSha256 || runtimeFailures.length) throw new Error(`remote runtime identity verification failed: ${runtimeFailures.join('; ') || 'missing identity fields'}: ${identityOutput}`);
    const cleanup = stage(timings, 'POST_RELEASE_CLEANUP', () => run(`set -e; docker ps -aq --filter name=gpoint-static- --filter status=created --filter status=exited | xargs -r docker rm >/dev/null; previousRuntime=$(cat /tmp/gpoint-previous-runtime-${sha} 2>/dev/null || true); if [ -n "$previousRuntime" ] && [ "$previousRuntime" != "gpoint-runtime:${sha}" ]; then docker image rm "$previousRuntime" >/dev/null 2>&1 || true; fi; rm -f /tmp/gpoint-previous-runtime-${sha} /tmp/gpoint-aux-${sha}; docker builder prune --filter until=168h -f >/dev/null; dangling=$(docker image ls --filter dangling=true -q | sort -u); if [ -n "$dangling" ]; then docker image rm $dangling >/dev/null; fi; test -z "$(docker image ls --filter dangling=true -q)"; echo GPOINT_POST_RELEASE_CLEANUP_OK`));
    if (!cleanup.includes('GPOINT_POST_RELEASE_CLEANUP_OK')) throw new Error('post-release cleanup failed');
    const migrationOutput = [
      migration.includes('Database schema is up to date!') ? 'DATABASE_SCHEMA_UP_TO_DATE' : null,
      migration.includes('No pending migrations to apply.') ? 'NO_PENDING_MIGRATIONS' : null,
      migration.includes('Applying migration') ? 'PENDING_MIGRATION_APPLIED' : null,
    ].filter(Boolean).join(',');
    if (!migrationOutput) throw new Error('migration gate output was not recognized');
    return { imageIdentity: { imageId, indexSha256, buildSha, detectorHash }, migrationOutput, composeExitCode, postSwitch, detectorHash, timings };
  } finally {
    fs.rmSync(checkout, { recursive: true, force: true });
    fs.rmSync(archive, { force: true });
  }
}
