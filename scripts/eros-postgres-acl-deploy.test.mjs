import test from 'node:test';
import assert from 'node:assert/strict';
import {remoteHelperArgs, runAclDeploy, validateAclDeployRequest} from './eros-postgres-acl-deploy.mjs';

const sha = 'a'.repeat(40);
const ownerEnv = {CANDIDATE_SHA: sha, RELEASE_MODE: 'eros-acl-apply',
  GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_REF: 'refs/heads/main', APPROVE_EROS_ACL: 'true',
  GITHUB_REPOSITORY_OWNER: 'akhripunov', GITHUB_ACTOR: 'akhripunov', GITHUB_TRIGGERING_ACTOR: 'akhripunov'};

test('ACL modes are limited to an exact owner dispatch for Apply', () => {
  assert.deepEqual(validateAclDeployRequest(ownerEnv), {candidate: sha, mode: 'eros-acl-apply'});
  assert.throws(() => validateAclDeployRequest({...ownerEnv, GITHUB_ACTOR: 'collaborator'}), /repository owner/);
  assert.throws(() => validateAclDeployRequest({...ownerEnv, CANDIDATE_SHA: 'main'}), /exact candidate/);
  assert.throws(() => validateAclDeployRequest({...ownerEnv, RELEASE_MODE: 'production'}), /dedicated ACL mode/);
  assert.throws(() => validateAclDeployRequest({...ownerEnv, APPROVE_EROS_ACL: 'false'}), /explicit approval/);
});

test('read-only plan rejects any ACL approval and constructs no mutation command', () => {
  const env = {...ownerEnv, RELEASE_MODE: 'eros-acl-plan', APPROVE_EROS_ACL: 'false'};
  assert.deepEqual(validateAclDeployRequest(env), {candidate: sha, mode: 'eros-acl-plan'});
  assert.match(remoteHelperArgs({candidate: sha, mode: env.RELEASE_MODE}), /^plan --candidate/);
  assert.throws(() => validateAclDeployRequest({...env, APPROVE_EROS_ACL: 'true'}), /rejects Apply/);
});

test('Apply uses the helper from the exact checkout under the shared release lock', () => {
  const calls = [];
  const result = runAclDeploy({env: ownerEnv, home: '/private/tmp/home',
    lock: (...args) => { calls.push(['lock', ...args]); return () => calls.push(['unlock']); },
    readSource: file => { calls.push(['source', file]); return 'ACL_HELPER_SOURCE'; },
    remote: (command, input) => { calls.push(['remote', command, input]); return '{"status": "ACL_APPLIED"}'; }});
  assert.equal(result, '{"status": "ACL_APPLIED"}');
  assert.equal(calls[0][0], 'lock');
  assert.ok(calls[2][1].includes('python3 - apply --plan /opt/gpoint/backups/eros-acl/me36-plan.json'));
  assert.equal(calls[2][2], 'ACL_HELPER_SOURCE');
  assert.deepEqual(calls.at(-1), ['unlock']);
});

test('guarded rollback has its own exact owner-confirmed dispatch mode', () => {
  const env = {...ownerEnv, RELEASE_MODE: 'eros-acl-rollback'};
  assert.deepEqual(validateAclDeployRequest(env), {candidate: sha, mode: 'eros-acl-rollback'});
  assert.match(remoteHelperArgs({candidate: sha, mode: env.RELEASE_MODE}, 'akhripunov'), /python3 - rollback/);
  assert.throws(() => validateAclDeployRequest({...env, GITHUB_TRIGGERING_ACTOR: 'collaborator'}), /repository owner/);
});

test('an ambiguous or blocked remote response is not retried by the ACL driver', () => {
  let attempts = 0;
  assert.throws(() => runAclDeploy({env: ownerEnv, home: '/private/tmp/home',
    lock: () => () => {}, readSource: () => 'source',
    remote: () => { attempts += 1; throw new Error('remote outcome unknown'); }}), /remote outcome unknown/);
  assert.equal(attempts, 1);
});
