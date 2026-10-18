import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {decideRecoveryFinalizationStep, recoveryFinalizationIdentity, recoveryFinalizationIdentityFromFailureEvidence, runRecoveryFinalization,
  validateRecoveryFinalizationApproval, assertNoConcurrentRelease, loadCompleteDeployInventory} from './recovery-finalization.mjs';

const candidate = 'd'.repeat(40);
const baseline = 'a'.repeat(40);
const releaseId = 'gpoint-release-20261017T064101334Z';
const identity = recoveryFinalizationIdentity(releaseId, candidate);
const sha256 = 'c'.repeat(64);
const failureAssetDigest = `sha256:${'f'.repeat(64)}`;
const failureManifestSha256 = 'e'.repeat(64);

function recoveryState() {
  return {currentProductionSha: baseline, currentProductionRef: 'gpoint-production-baseline',
    lastReleaseId: releaseId, lastReleaseStatus: 'RECOVERY_REQUIRED', recoveryRequired: true,
    lastReleaseFailureManifest: `docs/release/manifests/${releaseId}.json`,
    recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
    recoveryReconciliationManifestSha256: reconciliation.contentSha256};
}

function snapshot(overrides = {}) {
  const failure = {releaseId, candidateSha: candidate, previousProductionSha: baseline,
    status: 'RECOVERY_REQUIRED', failureStage: 'POST_SWITCH', runId: '39010750184', attempt: '1',
    failureManifestAssetDigest: failureAssetDigest, failureManifestSha256};
  return {
    candidateSha: candidate,
    failedRelease: failure,
    ledger: {headSha: 'b'.repeat(40), state: {currentProductionSha: baseline,
      currentProductionRef: 'gpoint-production-baseline', lastReleaseId: 'older-release',
      lastReleaseStatus: 'COMPLETE', recoveryRequired: false}},
    productionRefTarget: baseline,
    productionTagTarget: null,
    reconciliationManifest: null,
    finalizationManifest: null,
    vpsReachable: true,
    githubReachable: true,
    concurrentRelease: false,
    runtime: {observedAt: new Date().toISOString(), sourceSha: candidate, healthy: true,
      imageParity: true, databaseHeadsVerified: true, workerLanesVerified: true,
      prismaHead: '20261015130000_codex_telegram_challenge', agataHead: 'agata0011', gsalesHead: '001_gsales'},
    ownerAuthorization: {valid: true, candidateSha: candidate, baselineSha: baseline, operation: 'FINALIZATION_ONLY'},
    databaseRiskDecision: {accepted: true, candidateSha: candidate, baselineSha: baseline, instructionId: 'OWNER_DB_RISK_ME38'},
    backup: {verified: true, baselineSha: baseline, sha256, path: `/opt/gpoint/backups/gpoint-recovery-current-${candidate}-1729152000000.dump`,
      candidate_sha: candidate, database: 'gpoint', purpose: 'CURRENT_AFTER_CANDIDATE_DEPLOY',
      createdAt: new Date().toISOString()},
    ...overrides,
  };
}

const reconciliation = {name: identity.reconciliationManifestName, candidateSha: candidate,
  releaseId: identity.reconciliationReleaseId, eventType: 'RECOVERY_STATE_RECONCILIATION', failedReleaseId: releaseId,
  failedDeployRunId: '39010750184', failedDeployRunAttempt: '1', failureManifestAssetDigest: failureAssetDigest,
  failureManifestSha256,
  previousProductionSha: baseline, productionShaAfter: null, failureStage: 'POST_SWITCH',
  status: 'RECOVERY_REQUIRED', backup: snapshot().backup, contentSha256: sha256};
const finalManifest = {name: identity.manifestName, candidateSha: candidate,
  releaseId: identity.releaseId, eventType: 'RECOVERY_FINALIZATION', failedReleaseId: releaseId,
  failedDeployRunId: '39010750184', failedDeployRunAttempt: '1', failedDeployManifestDigest: failureAssetDigest,
  failedDeployManifestSha256: failureManifestSha256, status: 'COMPLETE',
  previousProductionSha: baseline, productionShaAfter: candidate, productionRef: identity.productionTag,
  runtimeSwitchOccurred: false, databaseApplyOccurred: false, inferenceAcceptance: 'NOT_VERIFIED',
  e2eAcceptance: 'NOT_VERIFIED', dbHeadAfter: '20261015130000_codex_telegram_challenge',
  agataDbHeadAfter: 'agata0011', gsalesDbHeadAfter: '001_gsales', backup: snapshot().backup,
  contentSha256: 'e'.repeat(64)};

test('finalization identity is deterministic and binds the original incident', () => {
  assert.deepEqual(identity, {
    releaseId: 'gpoint-recovery-finalization-20261017T064101334Z',
    reconciliationReleaseId: 'gpoint-recovery-reconcile-20261017T064101334Z',
    productionTag: 'gpoint-production-20261017T064101334Z',
    manifestName: 'gpoint-recovery-finalization-20261017T064101334Z.json',
    reconciliationManifestName: 'gpoint-recovery-reconcile-20261017T064101334Z.json',
  });
  assert.deepEqual(recoveryFinalizationIdentity(releaseId, candidate), identity);
});

test('diagnostic identity uses the verified canonical failure manifest releaseId, not the numeric run id', () => {
  const manifest = {releaseId, candidateSha: candidate, status: 'RECOVERY_REQUIRED', failureStage: 'POST_SWITCH'};
  const result = {releaseId, candidateSha: candidate};
  assert.deepEqual(recoveryFinalizationIdentityFromFailureEvidence(manifest, result, candidate), identity);
  assert.throws(() => recoveryFinalizationIdentityFromFailureEvidence(manifest, result, '39010750184'), /candidate SHA/);
  assert.throws(() => recoveryFinalizationIdentityFromFailureEvidence(manifest, {...result, releaseId: '39010750184'}, candidate), /release identity mismatch/);
  assert.throws(() => recoveryFinalizationIdentityFromFailureEvidence({...manifest, releaseId: undefined}, result, candidate), /release identity mismatch/);
  assert.throws(() => recoveryFinalizationIdentityFromFailureEvidence({...manifest, releaseId: '39010750184'}, {...result, releaseId: '39010750184'}, candidate), /failed release identity/);
});

test('stale COMPLETE ledger is reconciled before production tag or final state writes', () => {
  assert.equal(decideRecoveryFinalizationStep(snapshot()).action, 'RECORD_RECOVERY_REQUIRED');
});

test('finalization validates or creates a protected backup before any recovery state or tag write', () => {
  const source = fs.readFileSync(new URL('./release-gpoint.mjs', import.meta.url), 'utf8');
  const finalizer = source.slice(source.indexOf('async function finalizeRecovery(candidate)'));
  const backupGate = finalizer.indexOf('if (current.currentProductionSha !== candidate)');
  const backupCreate = finalizer.indexOf('createRecoveryBackup(candidate, baselineSha, beforeBackup.runtime.facts)');
  const firstRecoveryWrite = finalizer.indexOf('const persistRecoveryRequired = async');
  const orchestrator = finalizer.indexOf('await runRecoveryFinalization({readSnapshot, persistRecoveryRequired');
  const productionTagWrite = finalizer.indexOf('pushProductionTag(stableIdentity.productionTag, candidate)');
  const ledgerWrite = finalizer.indexOf('persistRecoveryLedgerCheckpoint(reconciliation, nextState, expectedLedgerHead)');
  assert.ok(backupGate >= 0 && backupCreate > backupGate);
  assert.ok(firstRecoveryWrite > backupCreate);
  assert.ok(orchestrator > backupCreate);
  assert.ok(orchestrator > firstRecoveryWrite);
  assert.ok(productionTagWrite > firstRecoveryWrite);
  assert.ok(ledgerWrite > firstRecoveryWrite);
  assert.match(finalizer.slice(backupGate, firstRecoveryWrite), /validateRecoveryBackup\(backup, candidate, baselineSha\)/);
  assert.match(finalizer.slice(backupGate, firstRecoveryWrite), /assertRecoveryFinalizationAuthorization\(candidate, baselineSha, 'current protected backup creation'\)/);
  assert.doesNotMatch(finalizer, /applyCandidate\(|prisma migrate deploy|alembic upgrade|docker compose up|docker restart/);
});

test('invalid recovery backup prevents reconciliation, production tag, and ledger writes', async () => {
  let writes = 0;
  await assert.rejects(runRecoveryFinalization({
    readSnapshot: async () => snapshot({backup: {...snapshot().backup, verified: false}}),
    persistRecoveryRequired: async () => { writes += 1; },
    ensureProductionTag: async () => { writes += 1; },
    persistCompleteState: async () => { writes += 1; },
  }), /current protected backup is absent or invalid/);
  assert.equal(writes, 0);
});

test('durable recovery state proceeds to exact tag then one atomic complete ledger append', () => {
  const state = recoveryState();
  const input = snapshot({ledger: {headSha: 'b'.repeat(40), state}, reconciliationManifest: reconciliation});
  assert.equal(decideRecoveryFinalizationStep(input).action, 'PUSH_EXACT_TAG');
  assert.equal(decideRecoveryFinalizationStep({...input, productionTagTarget: candidate}).action, 'WRITE_COMPLETE_STATE');
});

test('an exact tag left by a prior interrupted attempt resumes at ledger finalization without another push', () => {
  const input = snapshot({ledger: {headSha: 'b'.repeat(40), state: recoveryState()},
    reconciliationManifest: reconciliation, productionTagTarget: candidate});
  assert.equal(decideRecoveryFinalizationStep(input).action, 'WRITE_COMPLETE_STATE');
});

test('a finalization manifest without matching COMPLETE ledger state fails closed', () => {
  const input = snapshot({ledger: {headSha: 'b'.repeat(40), state: recoveryState()},
    reconciliationManifest: reconciliation, productionTagTarget: candidate, finalizationManifest: finalManifest});
  assert.throws(() => decideRecoveryFinalizationStep(input), /manifest exists before the COMPLETE ledger commit/);
});

test('wrong tag, changed ledger baseline, or changed production ref fail closed', () => {
  assert.throws(() => decideRecoveryFinalizationStep(snapshot({productionTagTarget: 'f'.repeat(40)})), /different candidate/);
  assert.throws(() => decideRecoveryFinalizationStep(snapshot({ledger: {headSha: 'b'.repeat(40), state: {
    currentProductionSha: 'f'.repeat(40), lastReleaseStatus: 'COMPLETE', recoveryRequired: false}}})), /baseline drifted/);
  assert.throws(() => decideRecoveryFinalizationStep(snapshot({productionRefTarget: 'f'.repeat(40)})), /baseline drifted/);
});

test('active Deploy inventory must be complete and contain no other queued or running release', () => {
  const current = {id: 22, path: '.github/workflows/gpoint-deploy.yml@refs/heads/main', status: 'in_progress'};
  assert.equal(assertNoConcurrentRelease({runs: [current], totalCount: 1, currentRunId: '22'}), true);
  assert.throws(() => assertNoConcurrentRelease({runs: [current], totalCount: 2, currentRunId: '22'}), /inventory is incomplete/);
  assert.throws(() => assertNoConcurrentRelease({runs: [current,
    {id: 23, path: '.github/workflows/gpoint-deploy.yml@refs/heads/main', status: 'queued'}], totalCount: 2, currentRunId: '22'}), /another Deploy/);
  assert.throws(() => assertNoConcurrentRelease({runs: [{...current, path: 'unknown'}], totalCount: 1, currentRunId: '22'}), /unknown workflow/);
  assert.throws(() => assertNoConcurrentRelease({runs: [current], totalCount: 1, currentRunId: ''}), /inventory is incomplete/);
});

function inventoryPage(runs, totalCount) {
  return {ok: true, json: async () => ({total_count: totalCount, workflow_runs: runs})};
}

function fakeInventory(runs, {mutate = null, failAt = null, duplicateAt = null, shortAt = null} = {}) {
  const calls = [];
  return {
    calls,
    fetchPage: async page => {
      calls.push(page);
      if (page === failAt) throw new Error('network unavailable');
      const snapshot = mutate?.(calls.length) ?? runs;
      const total = snapshot.length;
      const start = (page - 1) * 100;
      let pageRuns = snapshot.slice(start, start + 100);
      if (page === duplicateAt && page > 1) pageRuns = [snapshot[0], ...pageRuns.slice(1)];
      if (page === shortAt) pageRuns = pageRuns.slice(0, Math.max(0, pageRuns.length - 1));
      return inventoryPage(pageRuns, total);
    },
  };
}

function inventoryRuns(count) {
  return Array.from({length: count}, (_, index) => ({
    id: String(count - index), status: 'completed',
    path: '.github/workflows/gpoint-deploy.yml@refs/heads/main',
    head_sha: candidate, run_attempt: 1, event: 'workflow_dispatch',
  }));
}

test('complete Deploy inventory pagination handles 100, 116, and 201 runs', async () => {
  for (const count of [100, 116, 201]) {
    const source = fakeInventory(inventoryRuns(count));
    const inventory = await loadCompleteDeployInventory({fetchPage: source.fetchPage, currentRunId: String(count)});
    assert.equal(inventory.total_count, count);
    assert.equal(inventory.workflow_runs.length, count);
    assert.equal(new Set(inventory.workflow_runs.map(run => String(run.id))).size, count);
    assertNoConcurrentRelease({runs: inventory.workflow_runs, totalCount: inventory.total_count, currentRunId: String(count)});
    assert.equal(source.calls.length, Math.ceil(count / 100) * 2, 'two stable full reads must be compared');
  }
});

test('a queued Deploy on page two blocks recovery finalization', async () => {
  const runs = inventoryRuns(116);
  runs[110] = {...runs[110], status: 'queued'};
  const source = fakeInventory(runs);
  await assert.rejects(loadCompleteDeployInventory({fetchPage: source.fetchPage, currentRunId: '116'}), /another Deploy/);
});

test('Deploy inventory pagination rejects duplicate IDs across pages', async () => {
  const source = fakeInventory(inventoryRuns(116), {duplicateAt: 2});
  await assert.rejects(loadCompleteDeployInventory({fetchPage: source.fetchPage, currentRunId: '116'}), /duplicate run ID/);
});

test('Deploy inventory pagination rejects total-count drift between pages', async () => {
  const runs = inventoryRuns(116);
  const calls = [];
  const source = async page => {
    calls.push(page);
    const pageRuns = runs.slice((page - 1) * 100, page * 100);
    return inventoryPage(pageRuns, calls.length === 1 ? 116 : 117);
  };
  await assert.rejects(loadCompleteDeployInventory({fetchPage: source, currentRunId: '116'}), /total_count changed/);
});

test('Deploy inventory pagination rejects incomplete pages and network failures', async () => {
  const incomplete = fakeInventory(inventoryRuns(116), {shortAt: 2});
  await assert.rejects(loadCompleteDeployInventory({fetchPage: incomplete.fetchPage, currentRunId: '116'}), /page is incomplete/);
  const network = fakeInventory(inventoryRuns(116), {failAt: 2});
  await assert.rejects(loadCompleteDeployInventory({fetchPage: network.fetchPage, currentRunId: '116'}), /network unavailable/);
});

test('Deploy inventory pagination rejects same-count membership or status drift', async () => {
  let reads = 0;
  const runs = inventoryRuns(116);
  const source = async page => {
    if (page === 1) reads += 1;
    const snapshot = reads === 1 ? runs : runs.map((run, index) => index === 110 ? {...run, status: 'in_progress'} : run);
    return inventoryPage(snapshot.slice((page - 1) * 100, page * 100), snapshot.length);
  };
  await assert.rejects(loadCompleteDeployInventory({fetchPage: source, currentRunId: '116'}), /inventory changed during read/);
});

test('Deploy inventory pagination rejects same-count membership replacement', async () => {
  let pass = 0;
  const runs = inventoryRuns(116);
  const source = async page => {
    if (page === 1) pass += 1;
    const current = pass === 1 ? runs : runs.map((run, index) => index === 115 ? {...run, id: '117'} : run);
    return inventoryPage(current.slice((page - 1) * 100, page * 100), current.length);
  };
  await assert.rejects(loadCompleteDeployInventory({fetchPage: source, currentRunId: '116'}), /inventory changed during read/);
});

test('Deploy inventory pagination rejects malformed response and run structures', async () => {
  await assert.rejects(loadCompleteDeployInventory({currentRunId: '22', fetchPage: async () => inventoryPage([], '1')}),
    /invalid structure/);
  await assert.rejects(loadCompleteDeployInventory({currentRunId: '22', fetchPage: async () => inventoryPage([
    {id: 21, status: 'in_progress', path: '.github/workflows/gpoint-deploy.yml'}], 1)}),
  /current workflow run/);
  await assert.rejects(loadCompleteDeployInventory({currentRunId: '22', fetchPage: async () => inventoryPage([
    {id: 22, status: 'in_progress', path: 'unknown-workflow'}], 1)}), /unknown workflow/);
  await assert.rejects(loadCompleteDeployInventory({currentRunId: '22', fetchPage: async () => inventoryPage([
    {id: 0, status: 'in_progress', path: '.github/workflows/gpoint-deploy.yml'}], 1)}), /malformed run/);
});

test('complete 116-run inventory is required before read-only failure can reach any recovery write', async () => {
  const source = fakeInventory(inventoryRuns(116));
  const writes = [];
  await assert.rejects(runRecoveryFinalization({
    readSnapshot: async () => {
      const inventory = await loadCompleteDeployInventory({fetchPage: source.fetchPage, currentRunId: '116'});
      assert.equal(inventory.total_count, 116);
      return snapshot({vpsReachable: false});
    },
    persistRecoveryRequired: async () => writes.push('ledger'),
    ensureProductionTag: async () => writes.push('tag'),
    persistCompleteState: async () => writes.push('complete'),
  }), /VPS or GitHub read-only verification/);
  assert.deepEqual(writes, []);
});

test('isolated finalization rehearsal uses the verified incident evidence shape and completes backup, tag, and ledger stages', async () => {
  const evidence = JSON.parse(fs.readFileSync(new URL('../tests/fixtures/me38-recovery-incident-evidence.json', import.meta.url), 'utf8'));
  const incidentCandidate = evidence.incidentResult.candidateSha;
  const incidentBaseline = evidence.incidentResult.previousProductionSha;
  const incidentReleaseId = evidence.incidentResult.releaseId;
  const incidentIdentity = recoveryFinalizationIdentity(incidentReleaseId, incidentCandidate);
  assert.equal(evidence.source.repository, 'akhripunov/gpoint');
  assert.equal(evidence.source.immutable, true);
  assert.equal(evidence.source.tag,
    'gpoint-evidence-d367c2ec24242a733dbb2c4145c91d2d4682ba2c-39010750184-1');
  for (const asset of Object.values(evidence.source.assets)) {
    assert.ok(Number.isSafeInteger(asset.id) && asset.id > 0);
    assert.match(asset.sha256, /^[a-f0-9]{64}$/);
    assert.ok(asset.size > 0);
  }
  assert.match(incidentCandidate, /^[a-f0-9]{40}$/);
  assert.match(incidentBaseline, /^[a-f0-9]{40}$/);
  assert.equal(evidence.incidentResult.status, 'RECOVERY_REQUIRED');
  assert.equal(evidence.incidentResult.automationRunId, '39010750184');
  assert.equal(evidence.incidentResult.automationRunAttempt, '1');
  assert.equal(evidence.incidentResult.prepareRunId, '39009939478');
  assert.equal(evidence.incidentManifest.releaseId, evidence.incidentResult.releaseId);
  assert.equal(evidence.incidentManifest.failureStage, 'POST_SWITCH');
  assert.equal(evidence.incidentManifest.productionShaAfter, null);

  const events = [];
  const deployInventory = fakeInventory(inventoryRuns(116));
  const incident = {
    ...snapshot().failedRelease,
    releaseId: incidentReleaseId,
    candidateSha: incidentCandidate,
    previousProductionSha: incidentBaseline,
    status: evidence.incidentResult.status,
    failureStage: evidence.incidentResult.failureStage,
    runId: evidence.incidentResult.automationRunId,
    attempt: evidence.incidentResult.automationRunAttempt,
    failureReason: evidence.incidentResult.failureReason,
    failureManifestAssetDigest: `sha256:${evidence.source.assets['manifests.zip'].sha256}`,
    failureManifestSha256: evidence.source.incidentManifestSha256,
  };
  let current = snapshot({candidateSha: incidentCandidate, failedRelease: incident, backup: undefined,
    ledger: {headSha: 'b'.repeat(40), state: {currentProductionSha: incidentBaseline,
      currentProductionRef: 'gpoint-production-baseline', lastReleaseId: 'older-release',
      lastReleaseStatus: 'COMPLETE', recoveryRequired: false}},
    productionRefTarget: incidentBaseline, productionTagTarget: null,
    runtime: {...snapshot().runtime, sourceSha: incidentCandidate},
    ownerAuthorization: {valid: true, candidateSha: incidentCandidate, baselineSha: incidentBaseline, operation: 'FINALIZATION_ONLY'},
    databaseRiskDecision: {accepted: true, candidateSha: incidentCandidate, baselineSha: incidentBaseline,
      instructionId: 'ME-38-DB-HISTORICAL-UNCERTAINTY-20261017-01'},
    reconciliationManifest: null, finalizationManifest: null});

  // This mirrors the production pre-backup read: all read-only identity and inventory gates pass first.
  const beforeBackup = await loadCompleteDeployInventory({fetchPage: deployInventory.fetchPage, currentRunId: '116'});
  assert.equal(beforeBackup.total_count, 116);
  assert.equal(current.failedRelease.failureManifestAssetDigest,
    `sha256:${evidence.source.assets['manifests.zip'].sha256}`);
  events.push('readonly-preflight');

  // Isolated backup adapter models create -> validate; a bad result stops before ledger/tag writes.
  const backup = {verified: true, baselineSha: incidentBaseline, sha256, path: '/opt/gpoint/backups/gpoint-recovery-current-fixture.dump',
    candidate_sha: incidentCandidate, database: 'gpoint', purpose: 'CURRENT_AFTER_CANDIDATE_DEPLOY',
    createdAt: new Date().toISOString()};
  events.push('backup-create');
  assert.equal(backup.verified, true);
  assert.match(backup.sha256, /^[a-f0-9]{64}$/);
  assert.equal(backup.baselineSha, incidentBaseline);
  events.push('backup-verify');
  current = {...current, backup};
  const incidentReconciliation = {...reconciliation, releaseId: incidentIdentity.reconciliationReleaseId,
    candidateSha: incidentCandidate, failedReleaseId: incidentReleaseId,
    previousProductionSha: incidentBaseline,
    failedDeployRunId: incident.runId, failedDeployRunAttempt: incident.attempt,
    failureManifestAssetDigest: incident.failureManifestAssetDigest,
    failureManifestSha256: incident.failureManifestSha256, backup};
  const incidentFinalManifest = {...finalManifest, releaseId: incidentIdentity.releaseId,
    candidateSha: incidentCandidate, failedReleaseId: incidentReleaseId,
    failedDeployRunId: incident.runId, failedDeployRunAttempt: incident.attempt,
    failedDeployManifestDigest: incident.failureManifestAssetDigest,
    failedDeployManifestSha256: incident.failureManifestSha256,
    previousProductionSha: incidentBaseline, productionShaAfter: incidentCandidate,
    productionRef: incidentIdentity.productionTag,
    dbHeadAfter: current.runtime.prismaHead, agataDbHeadAfter: current.runtime.agataHead,
    gsalesDbHeadAfter: current.runtime.gsalesHead, backup};
  const incidentRecoveryState = {currentProductionSha: incidentBaseline,
    currentProductionRef: 'gpoint-production-baseline', lastReleaseId: incidentReleaseId,
    lastReleaseStatus: 'RECOVERY_REQUIRED', recoveryRequired: true,
    lastReleaseFailureManifest: `docs/release/manifests/${incidentReleaseId}.json`,
    recoveryReconciliationManifest: `docs/release/manifests/${incidentIdentity.reconciliationManifestName}`,
    recoveryReconciliationManifestSha256: incidentReconciliation.contentSha256};

  const persistRecoveryRequired = async ({snapshot: before, expectedLedgerHead}) => {
    assert.equal(before.backup.sha256, backup.sha256);
    assert.equal(before.failedRelease.failureManifestSha256, evidence.source.incidentManifestSha256);
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    events.push('append-recovery-required');
    current = {...current, ledger: {headSha: '1'.repeat(40), state: incidentRecoveryState},
      reconciliationManifest: incidentReconciliation};
  };
  const ensureProductionTag = async ({snapshot: before, expectedLedgerHead}) => {
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    assert.equal(before.productionRefTarget, incidentBaseline);
    assert.equal(before.productionTagTarget, null);
    assert.equal(current.reconciliationManifest.backup.sha256, backup.sha256);
    events.push('write-exact-production-tag');
    current = {...current, productionTagTarget: incidentCandidate};
  };
  const persistCompleteState = async ({snapshot: before, expectedLedgerHead}) => {
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    assert.equal(before.productionTagTarget, incidentCandidate);
    assert.equal(before.failedRelease.runId, '39010750184');
    assert.equal(before.failedRelease.attempt, '1');
    events.push('append-finalization-manifest');
    events.push('write-complete-ledger');
    current = {...current, ledger: {headSha: '2'.repeat(40), state: {
      currentProductionSha: incidentCandidate, currentProductionRef: incidentIdentity.productionTag,
      lastReleaseId: incidentReleaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
      recoveryFinalizationManifest: `docs/release/manifests/${incidentIdentity.manifestName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${incidentIdentity.reconciliationManifestName}`,
      recoveryFinalizationManifestSha256: incidentFinalManifest.contentSha256,
      recoveryReconciliationManifestSha256: incidentReconciliation.contentSha256}},
      productionRefTarget: incidentCandidate, finalizationManifest: {...incidentFinalManifest, backup,
        runtimeSwitchOccurred: false, databaseApplyOccurred: false}};
  };

  const result = await runRecoveryFinalization({
    readSnapshot: async () => {
      const inventory = await loadCompleteDeployInventory({fetchPage: deployInventory.fetchPage, currentRunId: '116'});
      assert.equal(inventory.total_count, 116);
      return current;
    },
    persistRecoveryRequired, ensureProductionTag, persistCompleteState,
  });
  assert.equal(result.status, 'COMPLETE');
  assert.deepEqual(events, ['readonly-preflight', 'backup-create', 'backup-verify',
    'append-recovery-required', 'write-exact-production-tag', 'append-finalization-manifest', 'write-complete-ledger']);
  assert.equal(events.some(event => /apply|migration|runtime-switch|rollback/i.test(event)), false);
  assert.equal(current.ledger.state.lastReleaseStatus, 'COMPLETE');
  assert.equal(current.ledger.state.currentProductionSha, incidentCandidate);
  assert.equal(current.productionTagTarget, incidentCandidate);
  assert.equal(current.finalizationManifest.runtimeSwitchOccurred, false);
  assert.equal(current.finalizationManifest.databaseApplyOccurred, false);
  assert.equal(deployInventory.calls.length % 4, 0, 'each recovery snapshot must compare two complete inventory reads');
});

test('missing or invalid protected current backup blocks finalization', () => {
  for (const backup of [undefined, {verified: false}, {verified: true, baselineSha: baseline,
    sha256: 'bad', path: '/opt/gpoint/backups/current.sql.gz'}]) {
    assert.throws(() => decideRecoveryFinalizationStep(snapshot({backup})), /current protected backup is absent or invalid/);
  }
});

test('unaccepted historical database outcome and invalid owner scope block finalization', () => {
  assert.throws(() => decideRecoveryFinalizationStep(snapshot({databaseRiskDecision: {accepted: false}})), /historical database uncertainty/);
  assert.throws(() => decideRecoveryFinalizationStep(snapshot({ownerAuthorization: {valid: false}})), /OWNER authorization/);
});

test('exact finalization approval binds candidate, baseline, risk decision, and metadata-only operations', () => {
  const approval = {releaseMode: 'recovery-finalization', approveProduction: true, approvalActor: 'akhripunov', workflowCandidate: candidate,
    candidateSha: candidate, baselineSha: baseline,
    databaseRiskDecision: {accepted: true, candidateSha: candidate, baselineSha: baseline,
      instructionId: 'OWNER_DB_RISK_ME38',
      allowedOperations: ['create_current_backup', 'backup_verify', 'production_tag_reconcile', 'append_recovery_manifest', 'ledger_state_write', 'read_only_verify'],
      forbiddenOperations: ['runtime_apply', 'database_migration', 'runtime_switch', 'rollback']}};
  assert.equal(validateRecoveryFinalizationApproval(approval), true);
  for (const invalid of [
    {...approval, workflowCandidate: baseline},
    {...approval, approveProduction: false},
    {...approval, approvalActor: 'another-maintainer'},
    {...approval, databaseRiskDecision: {...approval.databaseRiskDecision, baselineSha: candidate}},
    {...approval, databaseRiskDecision: {...approval.databaseRiskDecision, allowedOperations: ['runtime_apply']}},
    {...approval, databaseRiskDecision: {...approval.databaseRiskDecision, forbiddenOperations: []}},
  ]) assert.throws(() => validateRecoveryFinalizationApproval(invalid), /approval|risk decision|operations/);
});

test('conflicting reconciliation or finalization manifest is never overwritten', () => {
  const state = recoveryState();
  const input = snapshot({ledger: {headSha: 'b'.repeat(40), state},
    reconciliationManifest: {...reconciliation, candidateSha: 'f'.repeat(40)}, productionTagTarget: candidate});
  assert.throws(() => decideRecoveryFinalizationStep(input), /reconciliation manifest is missing or conflicts/);
  const inputWithBadFinal = snapshot({ledger: {headSha: 'b'.repeat(40), state}, reconciliationManifest: reconciliation,
    productionTagTarget: candidate, finalizationManifest: {...finalManifest, contentSha256: 'bad'}});
  assert.throws(() => decideRecoveryFinalizationStep(inputWithBadFinal), /finalization manifest conflicts/);
  const badAssetDigest = snapshot({ledger: {headSha: 'b'.repeat(40), state: recoveryState()},
    reconciliationManifest: {...reconciliation, failureManifestAssetDigest: 'sha256:' + 'a'.repeat(64)},
    productionTagTarget: candidate});
  assert.throws(() => decideRecoveryFinalizationStep(badAssetDigest), /reconciliation manifest is missing or conflicts/);
  const badRawDigest = snapshot({ledger: {headSha: 'b'.repeat(40), state: recoveryState()},
    reconciliationManifest: {...reconciliation, failureManifestSha256: 'a'.repeat(64)},
    productionTagTarget: candidate});
  assert.throws(() => decideRecoveryFinalizationStep(badRawDigest), /reconciliation manifest is missing or conflicts/);
});

test('only a fully verified exact recovery is an idempotent no-op', () => {
  const done = snapshot({ledger: {headSha: 'b'.repeat(40), state: {currentProductionSha: candidate,
    currentProductionRef: identity.productionTag, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
    recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
    recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
    recoveryFinalizationManifestSha256: finalManifest.contentSha256,
    recoveryReconciliationManifestSha256: reconciliation.contentSha256}},
    productionRefTarget: candidate, productionTagTarget: candidate, reconciliationManifest: reconciliation,
    finalizationManifest: finalManifest});
  assert.equal(decideRecoveryFinalizationStep(done).action, 'ALREADY_COMPLETE');
  assert.throws(() => decideRecoveryFinalizationStep({...done, productionTagTarget: baseline}), /baseline drifted/);
});

test('idempotent COMPLETE requires matching incident ID and ledger-bound manifest digest', () => {
  const completeState = {currentProductionSha: candidate, currentProductionRef: identity.productionTag,
    lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
    recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
    recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
    recoveryFinalizationManifestSha256: finalManifest.contentSha256,
    recoveryReconciliationManifestSha256: reconciliation.contentSha256};
  const base = snapshot({ledger: {headSha: 'b'.repeat(40), state: completeState},
    productionRefTarget: candidate, productionTagTarget: candidate, reconciliationManifest: reconciliation,
    finalizationManifest: finalManifest});
  assert.equal(decideRecoveryFinalizationStep(base).action, 'ALREADY_COMPLETE');
  assert.throws(() => decideRecoveryFinalizationStep({...base, ledger: {headSha: 'b'.repeat(40),
    state: {...completeState, lastReleaseId: 'other-release'}}}), /baseline drifted/);
  assert.throws(() => decideRecoveryFinalizationStep({...base, ledger: {headSha: 'b'.repeat(40),
    state: {...completeState, recoveryFinalizationManifestSha256: 'a'.repeat(64)}}}), /baseline drifted/);
  assert.throws(() => decideRecoveryFinalizationStep({...base, finalizationManifest: {...finalManifest,
    contentSha256: 'a'.repeat(64)}}), /baseline drifted/);
});

test('stale-ledger recovery persists durable state before tag and resumes idempotently', async () => {
  let current = snapshot();
  const counts = {state: 0, tag: 0, complete: 0};
  const deployInventory = fakeInventory(inventoryRuns(116));
  let completeInventoryReads = 0;
  const persistRecoveryRequired = async ({expectedLedgerHead}) => {
    counts.state += 1;
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    current = {...current, ledger: {headSha: '1'.repeat(40), state: recoveryState()},
      reconciliationManifest: reconciliation};
  };
  const ensureProductionTag = async ({expectedLedgerHead}) => {
    counts.tag += 1;
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    current = {...current, productionTagTarget: candidate};
  };
  const persistCompleteState = async ({expectedLedgerHead}) => {
    counts.complete += 1;
    assert.equal(expectedLedgerHead, current.ledger.headSha);
    current = {...current, ledger: {headSha: '2'.repeat(40), state: {currentProductionSha: candidate,
      currentProductionRef: identity.productionTag, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
      recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
      recoveryFinalizationManifestSha256: finalManifest.contentSha256,
      recoveryReconciliationManifestSha256: reconciliation.contentSha256}},
      productionRefTarget: candidate, finalizationManifest: finalManifest};
  };
  const run = () => runRecoveryFinalization({readSnapshot: async () => {
    const inventory = await loadCompleteDeployInventory({fetchPage: deployInventory.fetchPage, currentRunId: '116'});
    assert.equal(inventory.total_count, 116);
    completeInventoryReads += 1;
    return current;
  },
    persistRecoveryRequired, ensureProductionTag, persistCompleteState});
  const result = await run();
  assert.equal(result.status, 'COMPLETE');
  assert.deepEqual(counts, {state: 1, tag: 1, complete: 1});
  const retry = await run();
  assert.equal(retry.idempotent, true);
  assert.deepEqual(counts, {state: 1, tag: 1, complete: 1});
  assert.ok(completeInventoryReads > 0, 'the complete recovery path must use the fully paginated Deploy inventory');
});

test('retry after tag push ambiguity reuses exact remote tag and does not push twice', async () => {
  let current = snapshot();
  let tagPushes = 0;
  let failedOnce = false;
  const readSnapshot = async () => current;
  const persistRecoveryRequired = async () => {
    current = {...current, ledger: {headSha: '1'.repeat(40), state: recoveryState()},
      reconciliationManifest: reconciliation};
  };
  const ensureProductionTag = async () => {
    tagPushes += 1;
    current = {...current, productionTagTarget: candidate};
    if (!failedOnce) { failedOnce = true; throw new Error('simulated response loss after exact tag appeared'); }
  };
  const persistCompleteState = async () => {
    current = {...current, ledger: {headSha: '2'.repeat(40), state: {currentProductionSha: candidate,
      currentProductionRef: identity.productionTag, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
      recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
      recoveryFinalizationManifestSha256: finalManifest.contentSha256,
      recoveryReconciliationManifestSha256: reconciliation.contentSha256}},
      productionRefTarget: candidate, finalizationManifest: finalManifest};
  };
  const run = () => runRecoveryFinalization({readSnapshot, persistRecoveryRequired,
    ensureProductionTag, persistCompleteState});
  await assert.rejects(run(), /response loss/);
  assert.equal((await run()).status, 'COMPLETE');
  assert.equal(tagPushes, 1);
});

test('retry after atomic COMPLETE ledger publication is a verified no-op', async () => {
  let current = snapshot({ledger: {headSha: 'b'.repeat(40), state: recoveryState()},
    reconciliationManifest: reconciliation, productionTagTarget: candidate});
  let completeWrites = 0;
  const persistCompleteState = async () => {
    completeWrites += 1;
    current = {...current, ledger: {headSha: '2'.repeat(40), state: {currentProductionSha: candidate,
      currentProductionRef: identity.productionTag, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
      recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
      recoveryFinalizationManifestSha256: finalManifest.contentSha256,
      recoveryReconciliationManifestSha256: reconciliation.contentSha256}},
      productionRefTarget: candidate, finalizationManifest: finalManifest};
    throw new Error('simulated response lost after verified durable ledger commit');
  };
  const run = () => runRecoveryFinalization({readSnapshot: async () => current,
    persistRecoveryRequired: async () => assert.fail('recovery state must already exist'),
    ensureProductionTag: async () => assert.fail('tag must already exist'), persistCompleteState});
  await assert.rejects(run(), /response lost/);
  const retry = await run();
  assert.equal(retry.idempotent, true);
  assert.equal(completeWrites, 1);
});

test('failure after exact tag but before final ledger commit resumes without repeated tag or Apply', async () => {
  let current = snapshot({ledger: {headSha: '1'.repeat(40), state: recoveryState()},
    reconciliationManifest: reconciliation, productionTagTarget: candidate});
  let tagWrites = 0, completeWrites = 0;
  const adapters = {readSnapshot: async () => current,
    persistRecoveryRequired: async () => assert.fail('reconciliation is already durable'),
    ensureProductionTag: async () => { tagWrites += 1; },
    persistCompleteState: async ({expectedLedgerHead}) => {
      assert.equal(expectedLedgerHead, current.ledger.headSha);
      completeWrites += 1;
      throw new Error('ledger write outcome interrupted');
    }};
  await assert.rejects(runRecoveryFinalization(adapters), /ledger write outcome interrupted/);
  assert.equal(tagWrites, 0);
  assert.equal(completeWrites, 1);
  current = {...current, ledger: {headSha: '1'.repeat(40), state: recoveryState()},
    finalizationManifest: null};
  const success = await runRecoveryFinalization({...adapters,
    persistCompleteState: async ({expectedLedgerHead}) => {
      assert.equal(expectedLedgerHead, current.ledger.headSha);
      completeWrites += 1;
      current = {...current, ledger: {headSha: '2'.repeat(40), state: {currentProductionSha: candidate,
        currentProductionRef: identity.productionTag, lastReleaseId: releaseId, lastReleaseStatus: 'COMPLETE', recoveryRequired: false,
        recoveryFinalizationManifest: `docs/release/manifests/${identity.manifestName}`,
        recoveryReconciliationManifest: `docs/release/manifests/${identity.reconciliationManifestName}`,
        recoveryFinalizationManifestSha256: finalManifest.contentSha256,
        recoveryReconciliationManifestSha256: reconciliation.contentSha256}},
        productionRefTarget: candidate, finalizationManifest: finalManifest};
    }});
  assert.equal(success.status, 'COMPLETE');
  assert.deepEqual({tagWrites, completeWrites}, {tagWrites: 0, completeWrites: 2});
});

test('failed VPS, GitHub, stale readback, concurrent release, or unverified Worker lanes block', () => {
  for (const [overrides, pattern] of [
    [{concurrentRelease: true}, /concurrent production release/],
    [{vpsReachable: false}, /VPS or GitHub/],
    [{githubReachable: false}, /VPS or GitHub/],
    [{runtime: {...snapshot().runtime, observedAt: '2020-01-01T00:00:00.000Z'}}, /readback is stale/],
    [{runtime: {...snapshot().runtime, sourceSha: 'f'.repeat(40)}}, /live runtime/],
    [{runtime: {...snapshot().runtime, healthy: false}}, /live runtime/],
    [{runtime: {...snapshot().runtime, imageParity: false}}, /live runtime/],
    [{runtime: {...snapshot().runtime, databaseHeadsVerified: false}}, /live runtime/],
    [{runtime: {...snapshot().runtime, workerLanesVerified: false}}, /live runtime/],
  ]) assert.throws(() => decideRecoveryFinalizationStep(snapshot(overrides)), pattern);
});
