import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {reconcilePostSwitchFailure} from './release-recovery.mjs';

const candidate = 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c';
const baseline = 'a700198a843c12a84f2e537178de752e3eddd54d';
const ledgerHead = 'b'.repeat(40);
const now = '2026-10-17T07:17:00.000Z';
const roles = ['api','worker','bot','agataApi','agataMemoryWorker','gsalesApi','gsalesAgataApi','gsalesAgataMemoryWorker'];
const imageRoles = ['app','agata','gsales'];
const digest = n => `sha256:${String(n).repeat(64)}`;
const reconcile = input => reconcilePostSwitchFailure(input, {nowMs: Date.parse(now)});

function fixture() {
  const repositories = {app: 'ghcr.io/akhripunov/gpoint-runtime', agata: 'ghcr.io/akhripunov/gpoint-agata-runtime',
    gsales: 'ghcr.io/akhripunov/gpoint-g-sales-runtime'};
  const images = Object.fromEntries(imageRoles.map((role, i) => [role, {repository: repositories[role], digest: digest((i + 1).toString()),
    configDigest: digest((i + 4).toString()), sourceSha: candidate, platform: 'linux/amd64'}]));
  const imageByService = {api:'app',worker:'app',bot:'app',agataApi:'agata',agataMemoryWorker:'agata',
    gsalesApi:'gsales',gsalesAgataApi:'gsales',gsalesAgataMemoryWorker:'gsales'};
  const input = {
    candidateSha: candidate,
    observedAt: now,
    ledger: {headSha: ledgerHead, state: {currentProductionSha: baseline, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
      currentProductionRef: 'gpoint-production-20261001T120000000Z'}, previousProductionRefTarget: baseline},
    incident: {
      run: {id: 39010750184, attempt: 1, headSha: candidate, event: 'workflow_dispatch', path: '.github/workflows/gpoint-deploy.yml', conclusion: 'failure'},
      validateJob: {status: 'completed', conclusion: 'success', headSha: candidate},
      deployJob: {status: 'completed', conclusion: 'failure', headSha: candidate},
      authorization: {mode: 'TASK_SCOPED_AUTO', status: 'PASS', candidateSha: candidate, baselineSha: baseline, authorizationId: 'gpoint-unified-release-v1-recovery-20261017'},
      evidence: {tag: `gpoint-evidence-${candidate}-39010750184-1`, immutable: true, resultDigest: digest('a'), manifestDigest: digest('b'), resultChecksumVerified: true, manifestChecksumVerified: true},
      result: {candidateSha: candidate, automationRunId: 39010750184, automationRunAttempt: 1, status: 'RECOVERY_REQUIRED', failureStage: 'POST_SWITCH',
        releaseId: 'gpoint-release-20261017T064101334Z', previousProductionSha: baseline, productionRef: null,
        failureReason: 'GitHub production tag push failed on a non-transient error; refusing retry'},
      failureManifest: {candidateSha: candidate, releaseId: 'gpoint-release-20261017T064101334Z', status: 'RECOVERY_REQUIRED', failureStage: 'POST_SWITCH',
        previousProductionSha: baseline, productionShaAfter: null, fileName: 'gpoint-release-20261017T064101334Z.json',
        failureReason: 'GitHub production tag push failed on a non-transient error; refusing retry'},
      failureManifestSha256: '',
      failureManifestBytesBase64: '',
      prepare: {runId: 39009939478, attempt: 1, status: 'success', candidateSha: candidate, immutable: true,
        tag: `gpoint-prepare-${candidate}-39009939478-1`, bundleDigest: digest('c'), receipt: {candidateSha: candidate,
          schema: 3, transport: 'ghcr-oci', runId: 39009939478, runAttempt: 1, status: 'READY',
          prismaHead: '20261015130000_codex_telegram_challenge',
          agataHead: 'agata0011', gsalesMigrationHead: '001_gsales', gsalesMigrationSha256: 'c'.repeat(64), images}},
    },
    main: {sha: '9'.repeat(40), containsCandidate: true},
    runtime: {observedAt: now, sourceSha: candidate, services: Object.fromEntries(roles.map(role => [role, {sourceSha: candidate,
      imageConfigDigest: images[imageByService[role]].configDigest, running: true, health: 'healthy'}])),
      migrations: {prismaHead: '20261015130000_codex_telegram_challenge', agataHead: 'agata0011', gsalesHead: '001_gsales', gsalesChecksum: 'c'.repeat(64)}},
    expectedMigrations: {prismaHead: '20261015130000_codex_telegram_challenge', agataHead: 'agata0011', gsalesHead: '001_gsales', gsalesChecksum: 'c'.repeat(64)},
    refs: {productionTagName: 'gpoint-production-20261017T064101334Z', productionTagExists: false,
      previousProductionTagName: 'gpoint-production-20261001T120000000Z', previousProductionTagTarget: baseline},
    rollback: {ready: true, baselineSha: baseline, previousManifestSha256: 'd'.repeat(64), previousImagesAvailable: true,
      backup: {available: true, sha256: 'e'.repeat(64), path: '/opt/gpoint/backups/verified.dump'}},
    worker: {launchAgentLoaded: true, lanes: Object.fromEntries(['GPOINT','GSALES'].map(lane => [lane, {
      status: 'HEALTHY', health: 'HEALTHY', snapshotState: 'VERIFIED', sourceId: `agata-worker-queue-status-v1:${lane.toLowerCase()}`,
      snapshotAt: now, pending: 0, running: 0,
    }]))},
  };
  const {fileName: _fileName, ...rawManifest} = input.incident.failureManifest;
  const manifestBytes = Buffer.from(JSON.stringify(rawManifest));
  input.incident.failureManifestBytesBase64 = manifestBytes.toString('base64');
  input.incident.failureManifestSha256 = createHash('sha256').update(manifestBytes).digest('hex');
  return input;
}

test('stale COMPLETE ledger is reconciled append-only as RECOVERY_REQUIRED without changing baseline', () => {
  const input = fixture();
  const result = reconcile(input);
  assert.equal(result.status, 'RECOVERY_REQUIRED');
  assert.equal(result.statePatch.currentProductionSha, baseline);
  assert.equal(result.statePatch.recoveryRequired, true);
  assert.equal(result.manifest.eventType, 'POST_SWITCH_RECOVERY_RECONCILIATION');
  assert.equal(result.manifest.ledgerParentSha, ledgerHead);
  assert.equal(result.manifest.applyPerformed, false);
  assert.equal(result.manifest.migrationApplyPerformed, false);
  assert.equal(result.manifest.productionTagPushed, false);
  assert.notEqual(input.ledger.state.recoveryRequired, true);
});

test('rejects a duplicate reconciliation for a previously reconciled incident', () => {
  const input = fixture();
  input.ledger.state.lastRecoverySourceRunId = '39010750184';
  assert.throws(() => reconcile(input), /already reconciled/);
});

test('does not trust caller-supplied mutation counters and never authorizes another mutation', () => {
  const input = fixture();
  input.incident.mutationHistory = {applyCount: 900, migrationApplyCount: 900, rollbackCount: 900,
    recoveryFinalizationCount: 900};
  const result = reconcile(input);
  assert.equal(result.applyAllowed, false);
  assert.equal(result.manifest.applyPerformed, false);
  assert.equal(result.manifest.migrationApplyPerformed, false);
  assert.equal(result.manifest.rollbackPerformed, false);
  assert.equal(result.manifest.productionTagPushed, false);
  assert.equal(result.status, 'RECOVERY_REQUIRED');
});

test('rejects source SHA, run, authorization or immutable asset provenance drift', () => {
  for (const alter of [
    x => { x.incident.result.candidateSha = 'f'.repeat(40); },
    x => { x.incident.run.headSha = 'f'.repeat(40); },
    x => { x.incident.authorization.status = 'NOT_REQUIRED'; },
    x => { x.incident.evidence.immutable = false; },
    x => { x.incident.evidence.manifestChecksumVerified = false; },
    x => { x.incident.prepare.receipt.runAttempt = 2; },
  ]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input));
  }
});

test('requires the exact immutable production tag derived from the failed release id', () => {
  const input = fixture();
  input.refs.productionTagName = 'gpoint-production-20261017T064101335Z';
  assert.throws(() => reconcile(input), /tag name/);
});

test('requires failure result and manifest to agree on the post-switch tag error', () => {
  const input = fixture();
  input.incident.failureManifest.failureReason = 'a different failure';
  assert.throws(() => reconcile(input), /failure manifest/);
});

test('rejects a modified immutable failure manifest or detached hash', () => {
  const alteredContent = fixture();
  alteredContent.incident.failureManifest.failureReason = 'different reason';
  assert.throws(() => reconcile(alteredContent), /manifest object differs/);
  const alteredHash = fixture();
  alteredHash.incident.failureManifestSha256 = 'e'.repeat(64);
  assert.throws(() => reconcile(alteredHash), /digest does not match/);
});

test('binds all live migration heads and GSales checksum to immutable Prepare receipt', () => {
  for (const [key, value] of [['prismaHead', '20261015120000_old'], ['agataHead', 'agata0010'],
    ['gsalesMigrationHead', '000_old'], ['gsalesMigrationSha256', 'd'.repeat(64)]]) {
    const input = fixture();
    input.incident.prepare.receipt[key] = value;
    assert.throws(() => reconcile(input), /immutable Prepare receipt/);
  }
});

test('rejects receipt schema, transport, or image repository substitution', () => {
  for (const alter of [
    x => { x.incident.prepare.receipt.schema = 2; },
    x => { x.incident.prepare.receipt.transport = 'archive'; },
    x => { x.incident.prepare.receipt.images.app.repository = 'ghcr.io/attacker/image'; },
  ]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input), /Prepare receipt identity|image provenance/);
  }
});

test('rejects missing, stale, mixed or unhealthy independent runtime readback', () => {
  for (const alter of [
    x => { x.runtime.sourceSha = baseline; },
    x => { x.runtime.services.api.health = 'starting'; },
    x => { x.runtime.services.gsalesApi.imageConfigDigest = digest('f'); },
    x => { x.runtime.migrations.agataHead = 'agata0010'; },
    x => { x.runtime.observedAt = '2026-10-17T07:00:00.000Z'; },
  ]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input));
  }
});

test('rejects unknown or changed refs and baseline drift', () => {
  for (const alter of [
    x => { x.refs.productionTagExists = true; },
    x => { x.refs.previousProductionTagTarget = 'f'.repeat(40); },
    x => { x.ledger.previousProductionRefTarget = 'f'.repeat(40); },
    x => { x.main.containsCandidate = false; },
    x => { x.ledger.headSha = 'g'.repeat(40); },
  ]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input));
  }
});

test('rejects absent rollback or backup readiness', () => {
  for (const alter of [x => { x.rollback.ready = false; }, x => { x.rollback.previousImagesAvailable = false; },
    x => { x.rollback.backup.sha256 = ''; }, x => { x.rollback.baselineSha = 'f'.repeat(40); }]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input));
  }
});

test('rejects stale or mismatched Product Worker snapshots', () => {
  for (const alter of [
    x => { x.worker.launchAgentLoaded = false; },
    x => { x.worker.lanes.GPOINT.sourceId = 'agata-worker-queue-status-v1:gsales'; },
    x => { x.worker.lanes.GSALES.snapshotState = 'UNAVAILABLE'; },
    x => { x.worker.lanes.GPOINT.pending = 1; },
    x => { x.worker.lanes.GSALES.snapshotAt = '2026-10-17T07:00:00.000Z'; },
  ]) {
    const input = fixture(); alter(input);
    assert.throws(() => reconcile(input));
  }
});

test('never authorizes another Apply or reports COMPLETE during reconciliation', () => {
  const input = fixture();
  const result = reconcile(input);
  assert.equal(result.status, 'RECOVERY_REQUIRED');
  assert.notEqual(result.status, 'COMPLETE');
  assert.equal(result.applyAllowed, false);
  assert.equal(result.manifest.nextAction, 'RECOVERY_FINALIZATION_REQUIRES_SEPARATE_CANONICAL_OPERATION');
});
