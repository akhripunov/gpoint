import test from 'node:test';
import assert from 'node:assert/strict';
import { buildRecoveryDiagnosticsSnapshot, runRecoveryDiagnostics, verifyRecoveryDiagnosticsSnapshot } from './recovery-diagnostics.mjs';

const candidateSha = 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c';
const baselineSha = 'a700198a843c12a84f2e537178de752e3eddd54d';
const now = new Date().toISOString();

function completeInput(overrides = {}) {
  return {
    observedAt: now,
    candidateSha,
    incident: { runId: '39010750184', attempt: '1', candidateSha, baselineSha,
      failureStage: 'POST_SWITCH', status: 'RECOVERY_REQUIRED', manifestSha256: 'a'.repeat(64) },
    ledger: { observedAt: now, headSha: 'b'.repeat(40), productionSha: baselineSha,
      stateSha256: 'c'.repeat(64), refTargetSha: baselineSha,
      state: { currentProductionSha: baselineSha, recoveryRequired: false } },
    runtime: { observedAt: now, releaseEnvSha: candidateSha, miniappSha: candidateSha, databaseReadOnlyQueryExitCode: 0,
      services: Object.fromEntries(['api', 'worker', 'bot', 'agata-api', 'agata-memory-worker', 'gsales-api']
        .map(name => [name, { running: true, sourceSha: candidateSha, imageId: `sha256:${'1'.repeat(64)}`, health: 'healthy' }])),
      migrationHeads: { prisma: '20261015130000_codex_telegram_challenge', agata: 'agata0011',
        gsales: '001_gsales', gsalesMigrationSha256: 'd'.repeat(64) } },
    workers: { observedAt: now, lanes: Object.fromEntries(['GPOINT', 'GSALES'].map(lane => [lane,
      { status: 'HEALTHY', sourceId: `agata-worker-queue-status-v1:${lane.toLowerCase()}`,
        snapshotAt: now, pending: 0, running: 0, queueSnapshotState: 'VERIFIED' }])) },
    backup: { observedAt: now, status: 'VERIFIED', path: '/opt/gpoint/backups/recovery.dump',
      sha256: 'e'.repeat(64), mode: '600', readback: 'PASS' },
    locks: { observedAt: now, local: { checked: true, held: false },
      github: { checked: true, activeConflicts: 0 } },
    productionTag: { observedAt: now, baselineTagChecked: true, candidateTagChecked: true, name: 'gpoint-production-20261016T120000000Z',
      targetSha: baselineSha, candidateTagName: 'gpoint-production-20261017T064101334Z', candidateTagTargetSha: null },
    incidentEvidence: { observedAt: now, verified: true, runId: '39010750184', attempt: '1', assetSha256: 'f'.repeat(64),
      resultAssetSha256: '1'.repeat(64), prepareBundleDigest: '2'.repeat(64), prepareRunId: '39001890156', prepareRunAttempt: '1' },
    ...overrides,
  };
}

test('recovery-only diagnostics accepts only the known runtime/ledger drift and never authorizes finalization', () => {
  const result = buildRecoveryDiagnosticsSnapshot(completeInput());
  assert.equal(result.status, 'FACTS_COMPLETE', JSON.stringify(result.missingEvidence));
  assert.equal(result.drift.kind, 'KNOWN_POST_SWITCH_RUNTIME_AHEAD_OF_LEDGER');
  assert.equal(result.drift.runtimeSha, candidateSha);
  assert.equal(result.drift.ledgerSha, baselineSha);
  assert.equal(result.finalizationReady, false);
  assert.equal(result.factsComplete, true);
  assert.ok(result.blockers.includes('FINALIZATION_REQUIRES_SEPARATE_OWNER_AUTHORIZATION'));
  assert.equal(result.releaseState, 'RECOVERY_DIAGNOSTICS_ONLY');
  assert.equal(verifyRecoveryDiagnosticsSnapshot(result), true);
  assert.equal(verifyRecoveryDiagnosticsSnapshot({ ...result, drift: { ...result.drift, ledgerSha: candidateSha } }), false);
});

test('unknown or different SHA drift is rejected instead of being treated as the known incident', () => {
  assert.throws(() => buildRecoveryDiagnosticsSnapshot(completeInput({
    runtime: { ...completeInput().runtime, releaseEnvSha: 'f'.repeat(40) },
  })), /known ME-38 incident drift/);
  assert.throws(() => buildRecoveryDiagnosticsSnapshot(completeInput({
    ledger: { ...completeInput().ledger, productionSha: 'f'.repeat(40) },
  })), /known ME-38 incident drift/);
});

test('incomplete readback is explicitly incomplete and cannot become a usable snapshot', () => {
  const input = completeInput();
  delete input.runtime.migrationHeads.gsales;
  const result = buildRecoveryDiagnosticsSnapshot(input);
  assert.equal(result.status, 'INCOMPLETE');
  assert.ok(result.missingEvidence.includes('runtime.migrationHeads.gsales'));
  assert.equal(result.finalizationReady, false);
});

test('failed read-only database command cannot yield a complete snapshot even with parsed migration heads', () => {
  const input = completeInput();
  input.runtime.databaseReadOnlyQueryExitCode = 1;
  const result = buildRecoveryDiagnosticsSnapshot(input);
  assert.equal(result.status, 'INCOMPLETE');
  assert.ok(result.missingEvidence.includes('runtime.databaseReadOnlyQueryExitCode'));
  assert.equal(result.finalizationReady, false);
});

test('worker lane without a fresh verified queue snapshot makes the snapshot incomplete', () => {
  const input = completeInput();
  delete input.workers.lanes.GSALES.queueSnapshotState;
  const result = buildRecoveryDiagnosticsSnapshot(input);
  assert.equal(result.status, 'INCOMPLETE');
  assert.ok(result.missingEvidence.includes('workers.lanes.GSALES'));
  assert.equal(result.finalizationReady, false);
});

test('missing protected backup is recorded as a fact but blocks finalization readiness', () => {
  const result = buildRecoveryDiagnosticsSnapshot(completeInput({ backup: { observedAt: now, status: 'MISSING' } }));
  assert.equal(result.status, 'FACTS_COMPLETE', JSON.stringify(result.missingEvidence));
  assert.equal(result.backup.status, 'MISSING');
  assert.equal(result.finalizationReady, false);
  assert.ok(result.blockers.includes('PROTECTED_BACKUP_MISSING'));
});

test('diagnostic execution writes only the snapshot and rejects production mutations', async () => {
  let writes = 0;
  let mutations = 0;
  const read = async () => completeInput();
  const persistSnapshot = async value => { writes += 1; return value; };
  const result = await runRecoveryDiagnostics({ read, persistSnapshot, requestedOperations: ['read_only'] });
  assert.equal(writes, 1);
  assert.equal(mutations, 0);
  assert.equal(result.status, 'FACTS_COMPLETE', JSON.stringify(result.missingEvidence));
  await assert.rejects(runRecoveryDiagnostics({ read, persistSnapshot,
    requestedOperations: ['read_only', 'ledger_write'], mutate: async () => { mutations += 1; } }), /read-only mode forbids/);
  assert.equal(mutations, 0);
});
