#!/usr/bin/env node
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {acquireReleaseLock} from './release-lock.mjs';
import {runRemoteCommandWithInput} from './release-gpoint-vps-adapter.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const SOURCE = path.join(ROOT, 'scripts/eros-postgres-acl.py');
const SHA = /^[a-f0-9]{40}$/;
const PLAN = '/opt/gpoint/backups/eros-acl/me36-plan.json';
const OWNER_RE = /^[A-Za-z0-9-]{1,39}$/;

export function validateAclDeployRequest(env) {
  const candidate = env.CANDIDATE_SHA ?? '';
  const mode = env.RELEASE_MODE ?? '';
  if (!SHA.test(candidate) || !['eros-acl-plan', 'eros-acl-apply', 'eros-acl-rollback', 'eros-acl-reopen'].includes(mode))
    throw new Error('ACL operation requires an exact candidate and a dedicated ACL mode');
  if (env.GITHUB_EVENT_NAME !== 'workflow_dispatch' || env.GITHUB_REF !== 'refs/heads/main')
    throw new Error('ACL operation requires the trusted main Deploy workflow');
  if (mode === 'eros-acl-plan' && env.APPROVE_EROS_ACL !== 'false')
    throw new Error('read-only ACL planning rejects Apply approval');
  if (mode !== 'eros-acl-plan') {
    const owner = env.GITHUB_REPOSITORY_OWNER ?? '';
    if (env.APPROVE_EROS_ACL !== 'true' || !OWNER_RE.test(owner)
        || env.GITHUB_ACTOR !== owner || env.GITHUB_TRIGGERING_ACTOR !== owner)
      throw new Error('ACL mutation requires explicit approval by the repository owner');
  }
  return {candidate, mode};
}

export function remoteHelperArgs({candidate, mode}, owner = '') {
  const pathArg = `--plan ${PLAN}`;
  if (mode === 'eros-acl-plan') {
    return `plan --candidate ${candidate} --production "$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1)" --maintenance-inventory /opt/gpoint/backups/eros-acl/maintenance-clients.json`;
  }
  const operation = {'eros-acl-apply': 'apply', 'eros-acl-rollback': 'rollback', 'eros-acl-reopen': 'reopen'}[mode];
  return `EROS_ACL_APPLY_APPROVED=true GITHUB_REPOSITORY_OWNER=${owner} GITHUB_ACTOR=${owner} GITHUB_TRIGGERING_ACTOR=${owner} GITHUB_EVENT_NAME=workflow_dispatch GITHUB_REF=refs/heads/main python3 - ${operation} ${pathArg} --candidate ${candidate} --owner-approved-sha ${candidate}`;
}

export function runAclDeploy({env = process.env, remote = runRemoteCommandWithInput,
  readSource = file => fs.readFileSync(file, 'utf8'), lock = acquireReleaseLock,
  home = os.homedir()} = {}) {
  const request = validateAclDeployRequest(env);
  const lockPath = path.join(home, '.config/gpoint/release-production.lock');
  const legacyPath = path.join(ROOT, '.gpoint-release.lock');
  const unlock = lock(lockPath, `eros-acl-${request.candidate}`, legacyPath);
  try {
    const invocation = remoteHelperArgs(request, env.GITHUB_REPOSITORY_OWNER ?? '');
    const command = `set -e; ${request.mode === 'eros-acl-plan' ? `python3 - ${invocation}` : invocation}`;
    const result = remote(command, readSource(SOURCE));
    const expectedStatus = {'eros-acl-plan': 'PLAN_READY', 'eros-acl-apply': 'ACL_APPLIED',
      'eros-acl-rollback': 'ACL_ROLLED_BACK', 'eros-acl-reopen': 'ACL_ADMISSION_RESTORED'}[request.mode];
    if (!String(result).includes(`"status": "${expectedStatus}"`))
      throw new Error('ACL operation returned no matching completion evidence');
    process.stdout.write(`${String(result).trim()}\n`);
    return result;
  } finally {
    unlock();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { runAclDeploy(); }
  catch (error) {
    process.stderr.write(`BLOCKED: ${error.message}\n`);
    process.exitCode = 1;
  }
}
