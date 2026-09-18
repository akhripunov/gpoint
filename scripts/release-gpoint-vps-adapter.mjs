import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const REPO = 'git@github.com:akhripunov/gpoint.git';

function run(command, env = {}) {
  const runner = process.env.GPOINT_REMOTE_RUNNER;
  if (!runner) throw new Error('GPOINT_REMOTE_RUNNER is required for apply mode; no implicit SSH credentials are used');
  return execFileSync(runner, [], { encoding: 'utf8', env: { ...process.env, ...env, REMOTE_CMD: command }, stdio: ['ignore', 'pipe', 'pipe'] });
}

function upload(localFile, remoteFile) {
  const runner = process.env.GPOINT_SCP_RUNNER;
  if (!runner) throw new Error('GPOINT_SCP_RUNNER is required for apply mode; no implicit SSH credentials are used');
  return execFileSync(runner, [], { encoding: 'utf8', env: { ...process.env, LOCAL_FILE: localFile, REMOTE_FILE: remoteFile }, stdio: ['ignore', 'pipe', 'pipe'] });
}

function exactCheckout(sha) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-apply-'));
  execFileSync('git', ['clone', '--no-local', '--depth', '1', `file://${ROOT}`, dir], { stdio: 'ignore' });
  execFileSync('git', ['checkout', sha], { cwd: dir, stdio: 'ignore' });
  const actual = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir, encoding: 'utf8' }).trim();
  if (actual !== sha) throw new Error('apply checkout SHA mismatch');
  return dir;
}

export function applyCandidate(sha) {
  const checkout = exactCheckout(sha);
  const releaseDir = `/opt/gpoint/releases/${sha}`;
  const archive = `${checkout}.tar.gz`;
  const compose = path.join(checkout, 'infra/production/docker-compose.yml');
  const dockerfile = path.join(checkout, 'infra/production/Gpoint.Dockerfile');
  execFileSync('tar', ['-czf', archive, '-C', path.dirname(checkout), path.basename(checkout)]);
  try {
    upload(archive, `/tmp/gpoint-${sha}.tar.gz`);
    upload(compose, `/tmp/gpoint-compose-${sha}.yml`);
    upload(dockerfile, `/tmp/gpoint-dockerfile-${sha}`);
    const build = [
      'set -e',
      `rm -rf ${releaseDir}`,
      `mkdir -p ${releaseDir}`,
      `tar -xzf /tmp/gpoint-${sha}.tar.gz -C ${releaseDir}`,
      `mv ${releaseDir}/${path.basename(checkout)} ${releaseDir}/source`,
      `docker build --no-cache --build-arg GPOINT_BUILD_SHA=${sha} --label org.opencontainers.image.revision=${sha} --label org.opencontainers.image.source=${REPO} -f /tmp/gpoint-dockerfile-${sha} -t gpoint-runtime:${sha} ${releaseDir}/source`,
      `docker image inspect gpoint-runtime:${sha} | grep -q ${sha}`,
      'echo GPOINT_BUILD_OK'
    ].join('; ');
    const buildOutput = run(build); if (!buildOutput.includes('GPOINT_BUILD_OK')) throw new Error('remote image build failed');
    const migration = run(`set -e; docker run --rm --network gpoint_app --network-alias gpoint-migration --network shared_internal --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate status || true; docker run --rm --network gpoint_app --network-alias gpoint-migration --network shared_internal --env-file /opt/gpoint/env/production.env gpoint-runtime:${sha} pnpm --filter @gpoint/db exec prisma migrate deploy; echo GPOINT_MIGRATION_OK`);
    if (!migration.includes('GPOINT_MIGRATION_OK')) throw new Error('remote migration deploy failed');
    const deploy = [
      'set -e',
      `cp /tmp/gpoint-compose-${sha}.yml /opt/gpoint/compose/docker-compose.yml`,
      `sed -i.bak s/^RELEASE_SHA=.*/RELEASE_SHA=${sha}/ /opt/gpoint/env/production.env`,
      'rm -f /opt/gpoint/env/production.env.bak',
      `rm -rf /opt/gpoint/app/miniapp/assets`,
      `docker rm -f gpoint-static-${sha.slice(0, 8)} >/dev/null 2>&1 || true`,
      `docker create --name gpoint-static-${sha.slice(0, 8)} gpoint-runtime:${sha} >/dev/null`,
      `mkdir -p /opt/gpoint/app/miniapp`,
      `docker cp gpoint-static-${sha.slice(0, 8)}:/app/apps/miniapp/dist/. /opt/gpoint/app/miniapp/`,
      `docker rm gpoint-static-${sha.slice(0, 8)} >/dev/null`,
      'chmod -R a+rX /opt/gpoint/app/miniapp',
      'docker compose --env-file /opt/gpoint/env/production.env -f /opt/gpoint/compose/docker-compose.yml up -d --force-recreate api worker bot',
      'curl -fsS http://127.0.0.1:3000/health/live >/dev/null',
      'echo GPOINT_DEPLOY_OK'
    ].join('; ');
    const deployOutput = run(deploy); if (!deployOutput.includes('GPOINT_DEPLOY_OK')) throw new Error('remote runtime deploy failed');
    const identityOutput = run(`docker image inspect gpoint-runtime:${sha} | grep Id | head -1; sha256sum /opt/gpoint/app/miniapp/index.html; grep -o ${sha} /opt/gpoint/app/miniapp/index.html | head -1`).trim();
    const imageId = identityOutput.match(/"Id"\s*:\s*"(sha256:[a-f0-9]+)"/)?.[1];
    const indexSha256 = identityOutput.match(/\b([a-f0-9]{64})\s+\/opt\/gpoint\/app\/miniapp\/index\.html\b/)?.[1];
    const buildSha = identityOutput.includes(sha) ? sha : undefined;
    if (!imageId || !indexSha256 || buildSha !== sha) throw new Error('remote runtime identity verification failed');
    const migrationOutput = [
      migration.includes('Database schema is up to date!') ? 'DATABASE_SCHEMA_UP_TO_DATE' : null,
      migration.includes('No pending migrations to apply.') ? 'NO_PENDING_MIGRATIONS' : null,
      migration.includes('Applying migration') ? 'PENDING_MIGRATION_APPLIED' : null,
    ].filter(Boolean).join(',');
    if (!migrationOutput) throw new Error('migration gate output was not recognized');
    return { imageIdentity: { imageId, indexSha256, buildSha }, migrationOutput };
  } finally {
    fs.rmSync(checkout, { recursive: true, force: true });
    fs.rmSync(archive, { force: true });
  }
}
