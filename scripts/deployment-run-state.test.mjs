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
    LIVE_GSALES_API_IMAGE_ID: image, LIVE_GSALES_API_RUNNING: 'true', LIVE_GSALES_API_HEALTH: 'healthy', LIVE_GSALES_API_SHA: sha,
    LIVE_GSALES_AGATA_API_IMAGE_ID: image, LIVE_GSALES_AGATA_API_RUNNING: 'true', LIVE_GSALES_AGATA_API_HEALTH: 'healthy', LIVE_GSALES_AGATA_API_SHA: sha,
    LIVE_GSALES_AGATA_MEMORY_WORKER_IMAGE_ID: image, LIVE_GSALES_AGATA_MEMORY_WORKER_RUNNING: 'true', LIVE_GSALES_AGATA_MEMORY_WORKER_HEALTH: 'healthy', LIVE_GSALES_AGATA_MEMORY_WORKER_SHA: sha,
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
  assert.throws(() => stateFromLiveVps(facts({LIVE_GSALES_API_IMAGE_ID: 'unknown'})), /GSales API image identity/);
});
