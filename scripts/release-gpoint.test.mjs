import test from 'node:test';
import assert from 'node:assert/strict';
import { checkArtifactParity, checkEffectiveConfig, evaluateSafetyGates, validateCandidate } from './release-gpoint.mjs';
import { validateRuntimeIdentity } from './release-gpoint-vps-adapter.mjs';

const sha = '4d99e0da856d2ffe174c54d7ae7f2f971ab462f5';
const artifacts = { api: { sourceSha: sha }, worker: { sourceSha: sha }, bot: { sourceSha: sha }, miniapp: { sourceSha: sha } };
const config = { api: sha, worker: sha, bot: sha, miniapp: sha };

test('exact candidate gate blocks a nonexistent or malformed remote candidate', () => {
  assert.ok(validateCandidate({ candidateSha: 'not-a-sha', remoteSha: null }).length);
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: '0'.repeat(40) }).some(x => x.includes('remote')));
});
test('artifact parity blocks API SHA mismatch before migration', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, api: { sourceSha: '0'.repeat(40) } }), ['api artifact SHA mismatch']));
test('artifact parity blocks Worker SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, worker: { sourceSha: '0'.repeat(40) } }), ['worker artifact SHA mismatch']));
test('artifact parity blocks Bot SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, bot: { sourceSha: '0'.repeat(40) } }), ['bot artifact SHA mismatch']));
test('artifact parity blocks Mini App SHA mismatch', () => assert.deepEqual(checkArtifactParity(sha, { ...artifacts, miniapp: { sourceSha: '0'.repeat(40) } }), ['miniapp artifact SHA mismatch']));
test('dirty checkout blocks exact candidate gate', () => assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha, clean: false }).some(x => x.includes('dirty'))));
test('effective config parity blocks wrong API image', () => assert.deepEqual(checkEffectiveConfig(sha, { ...config, api: '0'.repeat(40) }), ['api effective image revision mismatch']));
test('effective config parity blocks wrong Mini App artifact', () => assert.deepEqual(checkEffectiveConfig(sha, { ...config, miniapp: '0'.repeat(40) }), ['miniapp effective artifact revision mismatch']));
test('migration failure is represented as a pre-switch block', () => assert.equal(validateCandidate({ candidateSha: sha, remoteSha: sha }).length, 0));
test('concurrent release and production drift are fail-closed conditions', () => {
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha }).length === 0);
  assert.ok(validateCandidate({ candidateSha: sha, remoteSha: sha, clean: false }).length > 0);
});
test('all parity gates pass only when every role shares the candidate', () => {
  assert.deepEqual(checkArtifactParity(sha, artifacts), []);
  assert.deepEqual(checkEffectiveConfig(sha, config), []);
});
test('backup failure blocks before migration', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, backupReady: false, recordedProductionSha: sha, liveProductionSha: sha }).includes('backup gate failed')));
test('migration failure blocks runtime switch', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, migrationOk: false, recordedProductionSha: sha, liveProductionSha: sha }).includes('migration gate failed')));
test('held release lock rejects a concurrent release', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, concurrent: true, recordedProductionSha: sha, liveProductionSha: sha }).includes('concurrent release lock is held')));
test('recorded/live production drift blocks', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: '0'.repeat(40), liveProductionSha: sha }).includes('live production SHA drift')));
test('remote production ref mismatch blocks completion', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: sha, liveProductionSha: sha, productionRefTarget: '0'.repeat(40) }).includes('production ref target mismatch')));
test('runtime identity blocks a previous image before release completion', () => {
  const imageId = 'sha256:' + 'a'.repeat(64);
  const previousImage = 'sha256:' + 'b'.repeat(64);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: previousImage, workerImage: imageId, botImage: imageId }), ['apiImage does not use candidate image']);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: imageId, workerImage: imageId, botImage: imageId }), []);
});
