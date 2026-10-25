import test from 'node:test';
import assert from 'node:assert/strict';
import {stateFromLiveVps} from './deployment-run-state.mjs';

const sha = 'a'.repeat(40);
const image = `sha256:${'b'.repeat(64)}`;
function facts(overrides = {}) {
  return {LIVE_RELEASE_ENV_SHA: sha, LIVE_API_SHA: sha, LIVE_WORKER_SHA: sha, LIVE_BOT_SHA: sha,
    LIVE_MINIAPP_SHA: sha, LIVE_AGATA_API_SHA: sha, LIVE_AGATA_WORKER_SHA: sha,
    LIVE_API_RUNNING: 'true', LIVE_WORKER_RUNNING: 'true', LIVE_BOT_RUNNING: 'true', LIVE_API_HEALTH: 'healthy',
    LIVE_API_IMAGE: image, LIVE_WORKER_IMAGE: image, LIVE_BOT_IMAGE: image,
    LIVE_AGATA_API_RUNNING: 'true', LIVE_AGATA_WORKER_RUNNING: 'true',
    LIVE_AGATA_API_HEALTH: 'healthy', LIVE_AGATA_WORKER_HEALTH: 'healthy',
    LIVE_AGATA_API_IMAGE_ID: image, LIVE_AGATA_WORKER_IMAGE_ID: image,
    LIVE_GSALES_API_IMAGE_ID: image, LIVE_GSALES_API_STATUS: 'running', LIVE_GSALES_API_EXIT_CODE: '0', LIVE_GSALES_API_OOM_KILLED: 'false', LIVE_GSALES_API_RUNNING: 'true', LIVE_GSALES_API_HEALTH: 'healthy', LIVE_GSALES_API_SHA: sha,
    LIVE_GSALES_AGATA_API_IMAGE_ID: image, LIVE_GSALES_AGATA_API_STATUS: 'running', LIVE_GSALES_AGATA_API_EXIT_CODE: '0', LIVE_GSALES_AGATA_API_OOM_KILLED: 'false', LIVE_GSALES_AGATA_API_RUNNING: 'true', LIVE_GSALES_AGATA_API_HEALTH: 'healthy', LIVE_GSALES_AGATA_API_SHA: sha,
    LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID: image, LIVE_GSALES_AGATA_MEMORY_WORKER_STATUS: 'running', LIVE_GSALES_AGATA_MEMORY_WORKER_EXIT_CODE: '0', LIVE_GSALES_AGATA_MEMORY_WORKER_OOM_KILLED: 'false', LIVE_GSALES_AGATA_MEMORY_WORKER_RUNNING: 'true', LIVE_GSALES_AGATA_MEMORY_WORKER_HEALTH: 'healthy', LIVE_GSALES_AGATA_MEMORY_WORKER_SHA: sha,
    LIVE_PRISMA_HEAD: '20260922090000_example', LIVE_AGATA_HEAD: 'agata0008',
    LIVE_GSALES_HEAD: '001_example', LIVE_GSALES_MIGRATION_SHA256: 'c'.repeat(64), ...overrides};
}

test('first Deploy baseline accepts a populated VPS and existing database without Deployment history', () => {
  const state = stateFromLiveVps(facts());
  assert.equal(state.currentProductionSha, sha);
  assert.equal(state.currentProductionDbHead, '20260922090000_example');
  assert.equal(state.releaseOrigin, 'read-only-vps-baseline');
  assert.equal(state.currentProductionRef, null);
});

test('first Deploy baseline rejects unknown or mixed installed runtime identities', () => {
  assert.throws(() => stateFromLiveVps(facts({LIVE_MINIAPP_SHA: 'c'.repeat(40)})), /mixed or unverified/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_PRISMA_HEAD: 'NONE'})), /Prisma migration head/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_IMAGE_ID: 'unknown'})), /GSales API .*image identity/);
});

test('V5 baseline accepts all three GSales containers intentionally stopped with exact identities', () => {
  const stopped = Object.fromEntries(['API', 'AGATA_API', 'AGATA_MEMORY_WORKER'].flatMap(role => [
    [`LIVE_GSALES_${role}_STATUS`, 'exited'], [`LIVE_GSALES_${role}_RUNNING`, 'false'],
    [`LIVE_GSALES_${role}_HEALTH`, 'none'], [`LIVE_GSALES_${role}_EXIT_CODE`, '0'],
    [`LIVE_GSALES_${role}_OOM_KILLED`, 'false'],
  ]));
  const state = stateFromLiveVps(facts(stopped));
  assert.equal(state.currentProductionSha, sha);
  assert.equal(state.gsalesRuntimeState, 'stopped');
});

test('V5 baseline rejects partial, dead, or unidentified GSales stopped state', () => {
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: 'exited', LIVE_GSALES_API_RUNNING: 'false', LIVE_GSALES_API_HEALTH: 'none'})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: 'dead'})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: undefined})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_RUNNING: 'false'})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: 'exited', LIVE_GSALES_API_RUNNING: 'false', LIVE_GSALES_API_HEALTH: 'none', LIVE_GSALES_API_SHA: 'c'.repeat(40)})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: 'exited', LIVE_GSALES_API_RUNNING: 'false', LIVE_GSALES_API_HEALTH: 'none', LIVE_GSALES_API_EXIT_CODE: '137'})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_STATUS: 'exited', LIVE_GSALES_API_RUNNING: 'false', LIVE_GSALES_API_HEALTH: 'none', LIVE_GSALES_API_OOM_KILLED: 'true'})), /GSales.*state/);
});

test('V5 baseline rejects damaged running GSales state', () => {
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_EXIT_CODE: '137'})), /GSales.*state/);
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_OOM_KILLED: 'true'})), /GSales.*state/);
});
