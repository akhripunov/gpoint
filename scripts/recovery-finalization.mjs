import {createHash} from 'node:crypto';

const SHA40 = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const ASSET_DIGEST = /^sha256:[a-f0-9]{64}$/;
const RELEASE_ID = /^gpoint-release-(\d{8}T\d{9}Z)$/;
const OWNER_GITHUB_LOGIN = 'akhripunov';

export class RecoveryFinalizationError extends Error {
  constructor(message) { super(message); this.name = 'RecoveryFinalizationError'; }
}

function requireThat(condition, message) {
  if (!condition) throw new RecoveryFinalizationError(message);
}

function digest(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

/** Stable append-only identities are derived from the immutable failed attempt. */
export function recoveryFinalizationIdentity(failedReleaseId, candidateSha) {
  const match = RELEASE_ID.exec(failedReleaseId ?? '');
  requireThat(match && SHA40.test(candidateSha ?? ''), 'failed release identity or candidate SHA is invalid');
  return Object.freeze({
    releaseId: `gpoint-recovery-finalization-${match[1]}`,
    reconciliationReleaseId: `gpoint-recovery-reconcile-${match[1]}`,
    productionTag: `gpoint-production-${match[1]}`,
    manifestName: `gpoint-recovery-finalization-${match[1]}.json`,
    reconciliationManifestName: `gpoint-recovery-reconcile-${match[1]}.json`,
  });
}

/** Bind a derived recovery identity to the already verified immutable failure evidence. */
export function recoveryFinalizationIdentityFromFailureEvidence(manifest, result, candidateSha) {
  requireThat(manifest && result && manifest.releaseId === result.releaseId,
    'immutable failure manifest and result release identity mismatch');
  requireThat(manifest.candidateSha === candidateSha && result.candidateSha === candidateSha,
    'immutable failure evidence candidate SHA mismatch');
  requireThat(manifest.status === 'RECOVERY_REQUIRED' && manifest.failureStage === 'POST_SWITCH',
    'immutable failure evidence is not the expected post-switch failure');
  return recoveryFinalizationIdentity(manifest.releaseId, candidateSha);
}

function verifySnapshot(snapshot, {nowMs, maxReadbackAgeMs}) {
  const {candidateSha, failedRelease, ledger, ownerAuthorization, databaseRiskDecision,
    backup, runtime} = snapshot ?? {};
  requireThat(SHA40.test(candidateSha ?? ''), 'candidate SHA is invalid');
  requireThat(failedRelease?.status === 'RECOVERY_REQUIRED'
    && failedRelease.failureStage === 'POST_SWITCH'
    && failedRelease.candidateSha === candidateSha
    && /^\d+$/.test(String(failedRelease.runId ?? ''))
    && /^\d+$/.test(String(failedRelease.attempt ?? ''))
    && ASSET_DIGEST.test(failedRelease.failureManifestAssetDigest ?? '')
    && SHA256.test(failedRelease.failureManifestSha256 ?? '')
    && RELEASE_ID.test(failedRelease.releaseId ?? ''), 'immutable post-switch failure evidence is invalid');
  const baselineSha = failedRelease.previousProductionSha;
  requireThat(SHA40.test(baselineSha ?? ''), 'failed release baseline is invalid');
  const observedAt = Date.parse(runtime?.observedAt ?? '');
  requireThat(Number.isFinite(observedAt) && observedAt <= nowMs + 5_000
    && nowMs - observedAt <= maxReadbackAgeMs, 'VPS readback is stale or invalid');
  requireThat(snapshot.vpsReachable === true && snapshot.githubReachable === true,
    'VPS or GitHub read-only verification is unavailable');
  requireThat(snapshot.concurrentRelease === false, 'a concurrent production release is active or unknown');
  requireThat(runtime.sourceSha === candidateSha && runtime.healthy === true
    && runtime.imageParity === true && runtime.databaseHeadsVerified === true
    && runtime.workerLanesVerified === true,
  'live runtime, image parity, health, database heads, or Worker lanes are not verified');
  requireThat(ownerAuthorization?.valid === true
    && ownerAuthorization?.candidateSha === candidateSha
    && ownerAuthorization?.baselineSha === baselineSha
    && ownerAuthorization?.operation === 'FINALIZATION_ONLY',
  'finalization-only OWNER authorization is missing or out of scope');
  requireThat(databaseRiskDecision?.accepted === true
    && databaseRiskDecision?.candidateSha === candidateSha
    && databaseRiskDecision?.baselineSha === baselineSha
    && typeof databaseRiskDecision?.instructionId === 'string'
    && databaseRiskDecision.instructionId.length >= 8,
  'historical database uncertainty has no explicit OWNER risk decision');
  requireThat(backup?.verified === true && backup?.baselineSha === baselineSha
    && SHA256.test(backup?.sha256 ?? '') && typeof backup?.path === 'string'
    && backup.path.startsWith('/opt/gpoint/backups/'),
  'current protected backup is absent or invalid');
  requireThat(ledger && SHA40.test(ledger.headSha ?? ''), 'production ledger head is missing or invalid');
  return {candidateSha, baselineSha, failedRelease, identity: recoveryFinalizationIdentity(failedRelease.releaseId, candidateSha), ledger};
}

function manifestMatches(manifest, expected) {
  return manifest?.name === expected.name
    && Object.entries(expected).every(([key, value]) => key === 'name' || manifest?.[key] === value)
    && SHA256.test(manifest?.contentSha256 ?? '');
}

function sameBackupIdentity(left, right) {
  return left?.verified === true && right?.verified === true
    && left.path === right.path && left.sha256 === right.sha256
    && left.database === right.database && left.candidate_sha === right.candidate_sha
    && left.baselineSha === right.baselineSha;
}

export function assertNoConcurrentRelease({runs, totalCount, currentRunId}) {
  requireThat(Array.isArray(runs) && Number.isInteger(totalCount) && totalCount === runs.length
    && typeof currentRunId === 'string' && /^\d+$/.test(currentRunId),
  'active release inventory is incomplete or current workflow identity is unavailable');
  const runIds = runs.map(run => String(run?.id ?? ''));
  requireThat(new Set(runIds).size === runIds.length,
    'active release inventory contains a duplicate run ID');
  requireThat(runs.every(run => /^\d+$/.test(String(run?.id ?? ''))
    && typeof run?.status === 'string'
    && typeof run?.path === 'string'
    && run.path.split('@')[0] === '.github/workflows/gpoint-deploy.yml'),
  'active release inventory contains an unknown workflow identity');
  const conflicting = runs.filter(run => String(run.id) !== currentRunId && run.status !== 'completed');
  requireThat(conflicting.length === 0, 'another Deploy workflow is active or queued');
  return true;
}

const DEPLOY_RUNS_PER_PAGE = 100;
const MAX_DEPLOY_RUNS = 10_000;

function deployInventoryFingerprint(runs) {
  return JSON.stringify(runs.map(run => ({id: String(run.id), status: run.status, path: run.path,
    head_sha: run.head_sha ?? null, run_attempt: run.run_attempt ?? null, event: run.event ?? null}))
    .sort((left, right) => left.id.localeCompare(right.id)));
}

/** Read the entire Deploy history twice and reject any unstable or incomplete view. */
export async function loadCompleteDeployInventory({fetchPage, currentRunId, perPage = DEPLOY_RUNS_PER_PAGE}) {
  requireThat(typeof fetchPage === 'function' && Number.isInteger(perPage) && perPage > 0 && perPage <= 100,
    'Deploy inventory reader configuration is invalid');

  const readPass = async () => {
    const runs = [];
    const seen = new Set();
    let totalCount = null;
    for (let page = 1; ; page += 1) {
      requireThat(page <= Math.ceil(MAX_DEPLOY_RUNS / perPage),
        'active release inventory exceeds the supported complete-read limit');
      const response = await fetchPage(page, perPage);
      requireThat(response && response.ok === true && typeof response.json === 'function',
        'active Deploy inventory request failed');
      const body = await response.json();
      requireThat(body && !Array.isArray(body) && Number.isInteger(body.total_count)
        && body.total_count >= 0 && Array.isArray(body.workflow_runs),
      'active Deploy inventory response has an invalid structure');
      if (totalCount === null) {
        totalCount = body.total_count;
        requireThat(totalCount <= MAX_DEPLOY_RUNS, 'active release inventory exceeds the supported complete-read limit');
      } else {
        requireThat(body.total_count === totalCount, 'active Deploy inventory total_count changed during read');
      }
      const expectedPageLength = Math.min(perPage, Math.max(0, totalCount - runs.length));
      requireThat(body.workflow_runs.length === expectedPageLength,
        'active Deploy inventory page is incomplete or exceeds total_count');
      for (const run of body.workflow_runs) {
        const id = run && (typeof run.id === 'number' && Number.isSafeInteger(run.id) && run.id > 0
          ? String(run.id) : typeof run.id === 'string' && /^[1-9][0-9]*$/.test(run.id) ? run.id : '');
        requireThat(id && typeof run.status === 'string' && run.status.length > 0
          && typeof run.path === 'string' && run.path.length > 0,
        'active Deploy inventory contains a malformed run');
        requireThat(!seen.has(id), 'active Deploy inventory contains a duplicate run ID');
        seen.add(id);
        runs.push(run);
      }
      if (runs.length === totalCount) break;
      requireThat(body.workflow_runs.length === perPage,
        'active Deploy inventory page is incomplete before the declared total_count');
    }
    requireThat(totalCount !== null && runs.length === totalCount && seen.size === totalCount,
      'active Deploy inventory is incomplete');
    return {runs, totalCount, fingerprint: deployInventoryFingerprint(runs)};
  };

  const first = await readPass();
  const second = await readPass();
  requireThat(first.totalCount === second.totalCount && first.fingerprint === second.fingerprint,
    'active Deploy inventory changed during read');
  requireThat(first.runs.some(run => String(run.id) === currentRunId),
    'active Deploy inventory does not contain the current workflow run');
  assertNoConcurrentRelease({runs: first.runs, totalCount: first.totalCount, currentRunId});
  return {workflow_runs: first.runs, total_count: first.totalCount};
}

/** Choose one metadata-only step from a fresh read-only production snapshot. */
export function decideRecoveryFinalizationStep(snapshot, {nowMs = Date.now(), maxReadbackAgeMs = 5 * 60_000} = {}) {
  const verified = verifySnapshot(snapshot, {nowMs, maxReadbackAgeMs});
  const {candidateSha, baselineSha, failedRelease, identity, ledger} = verified;
  const {productionRefTarget, productionTagTarget, reconciliationManifest, finalizationManifest} = snapshot;
  const state = ledger.state;
  if (state?.currentProductionSha === candidateSha
      && state.lastReleaseStatus === 'COMPLETE'
      && state.recoveryRequired === false
      && state.lastReleaseId === failedRelease.releaseId
      && state.currentProductionRef === identity.productionTag
      && state.recoveryFinalizationManifest === `docs/release/manifests/${identity.manifestName}`
      && state.recoveryReconciliationManifest === `docs/release/manifests/${identity.reconciliationManifestName}`
      && reconciliationManifest?.contentSha256 === state.recoveryReconciliationManifestSha256
      && manifestMatches(reconciliationManifest, {name: identity.reconciliationManifestName,
        releaseId: identity.reconciliationReleaseId, eventType: 'RECOVERY_STATE_RECONCILIATION',
        candidateSha, failedReleaseId: failedRelease.releaseId,
        failedDeployRunId: failedRelease.runId, failedDeployRunAttempt: failedRelease.attempt,
        failureManifestAssetDigest: failedRelease.failureManifestAssetDigest,
        failureManifestSha256: failedRelease.failureManifestSha256, status: 'RECOVERY_REQUIRED',
        previousProductionSha: baselineSha, productionShaAfter: null, failureStage: 'POST_SWITCH'})
      && productionTagTarget === candidateSha
      && productionRefTarget === candidateSha
      && finalizationManifest?.contentSha256 === state.recoveryFinalizationManifestSha256
      && manifestMatches(finalizationManifest, {name: identity.manifestName, releaseId: identity.releaseId,
        eventType: 'RECOVERY_FINALIZATION',
        candidateSha, failedReleaseId: failedRelease.releaseId, status: 'COMPLETE',
        failedDeployRunId: failedRelease.runId, failedDeployRunAttempt: failedRelease.attempt,
        failedDeployManifestDigest: failedRelease.failureManifestAssetDigest,
        failedDeployManifestSha256: failedRelease.failureManifestSha256,
        previousProductionSha: baselineSha, productionShaAfter: candidateSha,
        productionRef: identity.productionTag, runtimeSwitchOccurred: false,
        databaseApplyOccurred: false, inferenceAcceptance: 'NOT_VERIFIED', e2eAcceptance: 'NOT_VERIFIED',
        dbHeadAfter: snapshot.runtime.prismaHead, agataDbHeadAfter: snapshot.runtime.agataHead,
        gsalesDbHeadAfter: snapshot.runtime.gsalesHead})
      && sameBackupIdentity(finalizationManifest.backup, snapshot.backup)) {
    return {action: 'ALREADY_COMPLETE', identity};
  }

  requireThat(state?.currentProductionSha === baselineSha && productionRefTarget === baselineSha,
    'ledger or production ref baseline drifted');
  requireThat(productionTagTarget == null || productionTagTarget === candidateSha,
    'existing production tag points to a different candidate');

  if (state.recoveryRequired === true) {
    requireThat(state.lastReleaseStatus === 'RECOVERY_REQUIRED'
      && state.lastReleaseId === failedRelease.releaseId,
    'durable recovery state does not match the immutable incident');
    requireThat(state.lastReleaseFailureManifest === `docs/release/manifests/${failedRelease.releaseId}.json`,
      'durable recovery state does not reference the original failure manifest');
    requireThat(manifestMatches(reconciliationManifest, {name: identity.reconciliationManifestName,
      releaseId: identity.reconciliationReleaseId,
      eventType: 'RECOVERY_STATE_RECONCILIATION', candidateSha, failedReleaseId: failedRelease.releaseId,
      failedDeployRunId: failedRelease.runId, failedDeployRunAttempt: failedRelease.attempt,
      failureManifestAssetDigest: failedRelease.failureManifestAssetDigest,
      failureManifestSha256: failedRelease.failureManifestSha256,
      status: 'RECOVERY_REQUIRED', previousProductionSha: baselineSha, productionShaAfter: null,
      failureStage: 'POST_SWITCH'}),
    'durable recovery reconciliation manifest is missing or conflicts');
    requireThat(reconciliationManifest.contentSha256 === state.recoveryReconciliationManifestSha256,
      'durable recovery reconciliation digest differs from the production ledger');
    requireThat(sameBackupIdentity(reconciliationManifest.backup, snapshot.backup),
      'durable recovery manifest backup differs from the verified current backup');
  } else {
    requireThat(state.recoveryRequired === false && state.lastReleaseStatus === 'COMPLETE',
      'ledger state is neither the expected stale baseline nor the matching recovery state');
    requireThat(!reconciliationManifest, 'stale ledger already contains an unexpected recovery reconciliation');
    return {action: 'RECORD_RECOVERY_REQUIRED', identity};
  }

  if (productionTagTarget == null) return {action: 'PUSH_EXACT_TAG', identity};
  if (finalizationManifest) {
    requireThat(manifestMatches(finalizationManifest, {name: identity.manifestName,
      releaseId: identity.releaseId,
      eventType: 'RECOVERY_FINALIZATION', candidateSha, failedReleaseId: failedRelease.releaseId,
      failedDeployRunId: failedRelease.runId, failedDeployRunAttempt: failedRelease.attempt,
      failedDeployManifestDigest: failedRelease.failureManifestAssetDigest,
      failedDeployManifestSha256: failedRelease.failureManifestSha256,
      status: 'COMPLETE', previousProductionSha: baselineSha, productionShaAfter: candidateSha,
      productionRef: identity.productionTag, runtimeSwitchOccurred: false, databaseApplyOccurred: false,
      inferenceAcceptance: 'NOT_VERIFIED', e2eAcceptance: 'NOT_VERIFIED'}),
    'existing finalization manifest conflicts with the immutable incident');
    throw new RecoveryFinalizationError('finalization manifest exists before the COMPLETE ledger commit; state is ambiguous');
  }
  return {action: 'WRITE_COMPLETE_STATE', identity};
}

/** Canonical digest for detecting a conflicting pre-existing recovery record. */
export function recoveryManifestDigest(manifest) {
  return digest(manifest);
}

export function validateRecoveryFinalizationApproval({releaseMode, approveProduction, approvalActor, workflowCandidate,
  candidateSha, baselineSha, databaseRiskDecision}) {
  requireThat(releaseMode === 'recovery-finalization' && approveProduction === true
    && approvalActor === OWNER_GITHUB_LOGIN
    && workflowCandidate === candidateSha && SHA40.test(candidateSha ?? '')
    && SHA40.test(baselineSha ?? ''), 'exact-candidate OWNER finalization approval is missing');
  requireThat(databaseRiskDecision?.accepted === true
    && databaseRiskDecision.candidateSha === candidateSha
    && databaseRiskDecision.baselineSha === baselineSha
    && typeof databaseRiskDecision.instructionId === 'string'
    && databaseRiskDecision.instructionId.length >= 8,
  'separate historical database risk decision is missing or out of scope');
  const allowed = ['create_current_backup', 'backup_verify', 'production_tag_reconcile', 'append_recovery_manifest',
    'ledger_state_write', 'read_only_verify'];
  const forbidden = ['runtime_apply', 'database_migration', 'runtime_switch', 'rollback'];
  requireThat(JSON.stringify([...(databaseRiskDecision.allowedOperations ?? [])].sort()) === JSON.stringify([...allowed].sort())
    && JSON.stringify([...(databaseRiskDecision.forbiddenOperations ?? [])].sort()) === JSON.stringify([...forbidden].sort()),
  'finalization authorization operations are not exact');
  return true;
}

/** Execute finalization steps using injected, audited I/O adapters. */
export async function runRecoveryFinalization({readSnapshot, persistRecoveryRequired,
  ensureProductionTag, persistCompleteState, maxSteps = 8, now = Date.now}) {
  const operations = {
    RECORD_RECOVERY_REQUIRED: persistRecoveryRequired,
    PUSH_EXACT_TAG: ensureProductionTag,
    WRITE_COMPLETE_STATE: persistCompleteState,
  };
  for (let step = 0; step < maxSteps; step += 1) {
    const snapshot = await readSnapshot();
    const decision = decideRecoveryFinalizationStep(snapshot, {nowMs: now()});
    if (decision.action === 'ALREADY_COMPLETE') return {status: 'COMPLETE', idempotent: true, identity: decision.identity};
    const operation = operations[decision.action];
    if (!operation) throw new RecoveryFinalizationError('recovery finalization step is unsupported');
    await operation({snapshot, identity: decision.identity, expectedLedgerHead: snapshot.ledger.headSha});
  }
  throw new RecoveryFinalizationError('recovery finalization did not converge within the bounded step count');
}
