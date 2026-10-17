import {createHash} from 'node:crypto';

const SHA40 = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const IMAGE_BY_RUNTIME_ROLE = Object.freeze({
  api: 'app', worker: 'app', bot: 'app',
  agataApi: 'agata', agataMemoryWorker: 'agata',
  gsalesApi: 'gsales', gsalesAgataApi: 'gsales', gsalesAgataMemoryWorker: 'gsales',
});
const REQUIRED_LANES = Object.freeze({
  GPOINT: 'agata-worker-queue-status-v1:gpoint',
  GSALES: 'agata-worker-queue-status-v1:gsales',
});
const IMAGE_REPOSITORIES = Object.freeze({
  app: 'ghcr.io/akhripunov/gpoint-runtime',
  agata: 'ghcr.io/akhripunov/gpoint-agata-runtime',
  gsales: 'ghcr.io/akhripunov/gpoint-g-sales-runtime',
});

export class RecoveryEvidenceError extends Error {
  constructor(message) { super(message); this.name = 'RecoveryEvidenceError'; }
}

function requireThat(condition, message) {
  if (!condition) throw new RecoveryEvidenceError(message);
}

function timestamp(value, name) {
  const parsed = Date.parse(value ?? '');
  requireThat(Number.isFinite(parsed), `${name} timestamp is invalid`);
  return parsed;
}

function assertFresh(value, now, maxAgeMs, name) {
  const time = timestamp(value, name);
  requireThat(time <= now + 5_000 && now - time <= maxAgeMs, `${name} evidence is stale or from the future`);
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

/** Validate an immutable post-switch failure and independent live readback.
 * The returned append record only restores RECOVERY_REQUIRED state. It never
 * authorizes Apply, migrations, rollback, production-ref publication or COMPLETE.
 */
export function reconcilePostSwitchFailure(input, {nowMs = Date.now(), readbackMaxAgeMs = 5 * 60_000,
  workerMaxAgeMs = 90_000} = {}) {
  const {candidateSha, observedAt, ledger, incident, main, runtime, expectedMigrations, refs, rollback, worker} = input ?? {};
  requireThat(SHA40.test(candidateSha ?? ''), 'candidate SHA is invalid');
  const observed = timestamp(observedAt, 'reconciliation');
  requireThat(observed <= nowMs + 5_000 && nowMs - observed <= readbackMaxAgeMs, 'reconciliation observation is stale');

  const run = incident?.run ?? {};
  const runId = String(run.id ?? '');
  const attempt = String(run.attempt ?? '');
  requireThat(/^[1-9][0-9]*$/.test(runId) && /^[1-9][0-9]*$/.test(attempt), 'failure run identity is invalid');
  requireThat(run.headSha === candidateSha && run.event === 'workflow_dispatch'
    && run.path === '.github/workflows/gpoint-deploy.yml' && run.conclusion === 'failure', 'failure run provenance mismatch');
  requireThat(incident.validateJob?.status === 'completed' && incident.validateJob?.conclusion === 'success'
    && incident.validateJob?.headSha === candidateSha, 'Deploy validation job evidence is not successful for candidate');
  requireThat(incident.deployJob?.status === 'completed' && incident.deployJob?.conclusion === 'failure'
    && incident.deployJob?.headSha === candidateSha, 'Deploy job failure evidence is invalid');

  const previousSha = ledger?.state?.currentProductionSha;
  requireThat(SHA40.test(ledger?.headSha ?? '') && SHA40.test(previousSha ?? ''), 'production ledger identity is invalid');
  requireThat(ledger.state.lastReleaseStatus === 'COMPLETE' && ledger.state.recoveryRequired === false
    && ledger.state.lastRecoverySourceRunId !== runId, 'stale ledger cannot be reconciled or incident was already reconciled');
  requireThat(ledger.previousProductionRefTarget === previousSha, 'ledger production reference does not match recorded baseline');

  const authorization = incident.authorization ?? {};
  requireThat(authorization.mode === 'TASK_SCOPED_AUTO' && authorization.status === 'PASS'
    && authorization.candidateSha === candidateSha && authorization.baselineSha === previousSha
    && typeof authorization.authorizationId === 'string' && authorization.authorizationId.length > 0,
  'original task authorization evidence is missing or mismatched');

  const result = incident.result ?? {};
  requireThat(result.candidateSha === candidateSha && String(result.automationRunId) === runId
    && String(result.automationRunAttempt) === attempt && result.status === 'RECOVERY_REQUIRED'
    && result.failureStage === 'POST_SWITCH' && result.previousProductionSha === previousSha
    && result.productionRef == null
    && typeof result.failureReason === 'string'
    && /GitHub production tag push failed on a non-transient error; refusing retry/.test(result.failureReason),
  'immutable Deploy result does not prove the expected post-switch tag failure');
  requireThat(typeof result.releaseId === 'string' && result.releaseId.length > 0, 'failed release identity is missing');
  const failureManifest = incident.failureManifest ?? {};
  const failureManifestBytesBase64 = incident.failureManifestBytesBase64;
  requireThat(typeof failureManifestBytesBase64 === 'string', 'immutable failure manifest bytes are missing');
  const failureManifestBytes = Buffer.from(failureManifestBytesBase64, 'base64');
  requireThat(failureManifestBytes.toString('base64') === failureManifestBytesBase64
    && createHash('sha256').update(failureManifestBytes).digest('hex') === incident.failureManifestSha256,
  'immutable failure manifest digest does not match its exact bytes');
  let parsedFailureManifest;
  try { parsedFailureManifest = JSON.parse(failureManifestBytes.toString('utf8')); }
  catch { throw new RecoveryEvidenceError('immutable failure manifest bytes are invalid JSON'); }
  const manifestWithoutLocalName = {...failureManifest};
  delete manifestWithoutLocalName.fileName;
  requireThat(stable(parsedFailureManifest) === stable(manifestWithoutLocalName),
    'immutable failure manifest object differs from its verified bytes');
  requireThat(failureManifest.candidateSha === candidateSha && failureManifest.releaseId === result.releaseId
    && failureManifest.status === 'RECOVERY_REQUIRED' && failureManifest.failureStage === 'POST_SWITCH'
    && failureManifest.previousProductionSha === previousSha && failureManifest.productionShaAfter == null
    && failureManifest.failureReason === result.failureReason
    && typeof failureManifest.fileName === 'string'
    && failureManifest.fileName === `${result.releaseId}.json`
    && SHA256.test(incident.failureManifestSha256 ?? ''),
  'immutable failure manifest does not match Deploy result');

  const evidence = incident.evidence ?? {};
  const expectedEvidenceTag = `gpoint-evidence-${candidateSha}-${runId}-${attempt}`;
  requireThat(evidence.tag === expectedEvidenceTag && evidence.immutable === true
    && DIGEST.test(evidence.resultDigest ?? '') && DIGEST.test(evidence.manifestDigest ?? '')
    && evidence.resultChecksumVerified === true && evidence.manifestChecksumVerified === true,
  'immutable failure evidence asset or checksum is invalid');

  const prepare = incident.prepare ?? {};
  const prepareRun = String(prepare.runId ?? '');
  const prepareAttempt = String(prepare.attempt ?? '');
  requireThat(/^[1-9][0-9]*$/.test(prepareRun) && /^[1-9][0-9]*$/.test(prepareAttempt)
    && prepare.status === 'success' && prepare.candidateSha === candidateSha && prepare.immutable === true
    && prepare.tag === `gpoint-prepare-${candidateSha}-${prepareRun}-${prepareAttempt}`
    && DIGEST.test(prepare.bundleDigest ?? ''), 'successful exact-SHA Prepare provenance is invalid');
  const receipt = prepare.receipt ?? {};
  requireThat(receipt.candidateSha === candidateSha && String(receipt.runId) === prepareRun
    && String(receipt.runAttempt) === prepareAttempt && receipt.status === 'READY'
    && receipt.schema === 3 && receipt.transport === 'ghcr-oci'
    && typeof receipt.prismaHead === 'string' && typeof receipt.agataHead === 'string'
    && typeof receipt.gsalesMigrationHead === 'string' && SHA256.test(receipt.gsalesMigrationSha256 ?? ''),
  'Prepare receipt identity or migration provenance is invalid');
  for (const role of ['app', 'agata', 'gsales']) {
    const image = receipt.images?.[role];
    requireThat(image?.repository === IMAGE_REPOSITORIES[role] && image?.sourceSha === candidateSha && DIGEST.test(image.digest ?? '')
      && DIGEST.test(image.configDigest ?? '') && image.platform === 'linux/amd64',
      `Prepare image provenance is missing for ${role}`);
  }

  requireThat(main?.containsCandidate === true && SHA40.test(main.sha ?? ''), 'candidate is not proven contained in current main');
  requireThat(refs?.productionTagExists === false && refs?.productionTagTarget == null,
    'candidate production tag already exists or its state is unknown');
  const releaseTimestamp = result.releaseId.replace(/^gpoint-release-/, '');
  const expectedProductionTag = `gpoint-production-${releaseTimestamp}`;
  requireThat(/^\d{8}T\d{9}Z$/.test(releaseTimestamp)
    && refs?.productionTagName === expectedProductionTag,
  'candidate production tag name is not derived from the immutable release identity');
  requireThat(refs.previousProductionTagName === ledger.state.currentProductionRef
    && refs.previousProductionTagTarget === previousSha, 'previous production ref target is unknown or changed');
  const runtimeAt = timestamp(runtime?.observedAt, 'runtime readback');
  requireThat(runtimeAt <= observed + 5_000 && observed - runtimeAt <= readbackMaxAgeMs,
    'runtime readback is stale relative to reconciliation');
  requireThat(runtime?.sourceSha === candidateSha, 'runtime source SHA mismatch');
  for (const role of Object.keys(IMAGE_BY_RUNTIME_ROLE)) {
    const actual = runtime.services?.[role];
    const prepared = receipt.images[IMAGE_BY_RUNTIME_ROLE[role]];
    requireThat(actual?.sourceSha === candidateSha && actual.running === true && actual.health === 'healthy'
      && actual.imageConfigDigest === prepared.configDigest,
    `runtime parity/health failed for ${role}`);
  }
  for (const key of ['prismaHead', 'agataHead', 'gsalesHead', 'gsalesChecksum']) {
    requireThat(typeof expectedMigrations?.[key] === 'string' && expectedMigrations[key].length > 0
      && runtime.migrations?.[key] === expectedMigrations[key], `migration identity mismatch: ${key}`);
  }
  requireThat(expectedMigrations.prismaHead === receipt.prismaHead
    && expectedMigrations.agataHead === receipt.agataHead
    && expectedMigrations.gsalesHead === receipt.gsalesMigrationHead
    && expectedMigrations.gsalesChecksum === receipt.gsalesMigrationSha256,
  'live migration expectations are not bound to immutable Prepare receipt');
  requireThat(SHA256.test(expectedMigrations.gsalesChecksum), 'GSales migration checksum is invalid');

  requireThat(rollback?.ready === true && rollback.baselineSha === previousSha
    && SHA256.test(rollback.previousManifestSha256 ?? '') && rollback.previousImagesAvailable === true
    && rollback.backup?.available === true && SHA256.test(rollback.backup.sha256 ?? '')
    && typeof rollback.backup.path === 'string' && rollback.backup.path.startsWith('/opt/gpoint/backups/'),
  'backup or previous-production rollback readiness is not proven');

  requireThat(worker?.launchAgentLoaded === true, 'Product Worker LaunchAgent is not loaded');
  for (const [lane, sourceId] of Object.entries(REQUIRED_LANES)) {
    const status = worker.lanes?.[lane];
    requireThat(status?.status === 'HEALTHY' && status.health === 'HEALTHY' && status.snapshotState === 'VERIFIED'
      && status.sourceId === sourceId && Number.isSafeInteger(status.pending) && status.pending === 0
      && Number.isSafeInteger(status.running) && status.running === 0,
    `${lane} Product Worker readiness is not proven`);
    assertFresh(status.snapshotAt, observed, workerMaxAgeMs, `${lane} Worker queue snapshot`);
  }

  const manifest = {
    eventType: 'POST_SWITCH_RECOVERY_RECONCILIATION',
    reconciliationId: `gpoint-recovery-reconcile-${runId}-${attempt}`,
    candidateSha,
    failedReleaseId: result.releaseId,
    sourceRun: {id: Number(runId), attempt: Number(attempt), workflowPath: run.path, conclusion: run.conclusion},
    prepare: {runId: Number(prepareRun), attempt: Number(prepareAttempt), bundleDigest: prepare.bundleDigest},
    immutableEvidence: {tag: evidence.tag, resultDigest: evidence.resultDigest, manifestDigest: evidence.manifestDigest},
    failedManifestSha256: incident.failureManifestSha256,
    authorizationId: authorization.authorizationId,
    previousProductionSha: previousSha,
    observedRuntimeSha: runtime.sourceSha,
    ledgerParentSha: ledger.headSha,
    runtimeObservedAt: runtime.observedAt,
    migrationHeads: {...runtime.migrations},
    rollbackReady: true,
    expectedProductionTag,
    expectedProductionTagExists: false,
    workerSnapshotAt: observedAt,
    status: 'RECOVERY_REQUIRED',
    applyPerformed: false,
    migrationApplyPerformed: false,
    rollbackPerformed: false,
    productionTagPushed: false,
    nextAction: 'RECOVERY_FINALIZATION_REQUIRES_SEPARATE_CANONICAL_OPERATION',
  };
  const recordSha256 = createHash('sha256').update(stable(manifest)).digest('hex');
  return {
    status: 'RECOVERY_REQUIRED',
    applyAllowed: false,
    statePatch: {
      currentProductionSha: previousSha,
      lastReleaseId: result.releaseId,
      lastReleaseStatus: 'RECOVERY_REQUIRED',
      serverDeployStatus: 'RECOVERY_REQUIRED',
      recoveryRequired: true,
      lastRecoverySourceRunId: runId,
      lastRecoveryEvidenceDigest: evidence.resultDigest,
      lastReleaseFailureManifest: `docs/release/manifests/${failureManifest.fileName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${manifest.reconciliationId}.json`,
    },
    manifest: {...manifest, recordSha256},
  };
}
