import test from 'node:test';
import assert from 'node:assert/strict';
import {decideRecoveryFinalizationStep, recoveryFinalizationIdentity, runRecoveryFinalization,
  validateRecoveryFinalizationApproval, assertNoConcurrentRelease} from './recovery-finalization.mjs';

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

test('stale COMPLETE ledger is reconciled before production tag or final state writes', () => {
  assert.equal(decideRecoveryFinalizationStep(snapshot()).action, 'RECORD_RECOVERY_REQUIRED');
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
  const run = () => runRecoveryFinalization({readSnapshot: async () => current,
    persistRecoveryRequired, ensureProductionTag, persistCompleteState});
  const result = await run();
  assert.equal(result.status, 'COMPLETE');
  assert.deepEqual(counts, {state: 1, tag: 1, complete: 1});
  const retry = await run();
  assert.equal(retry.idempotent, true);
  assert.deepEqual(counts, {state: 1, tag: 1, complete: 1});
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
