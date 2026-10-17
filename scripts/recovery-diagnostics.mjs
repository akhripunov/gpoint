import crypto from 'node:crypto';

export const ME38_RECOVERY_INCIDENT = Object.freeze({
  runId: '39010750184',
  attempt: '1',
  candidateSha: 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c',
  baselineSha: 'a700198a843c12a84f2e537178de752e3eddd54d',
});

const SHA40 = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const REQUIRED_SERVICES = ['api', 'worker', 'bot', 'agata-api', 'agata-memory-worker', 'gsales-api'];
const MUTATIONS = ['apply', 'migration', 'rollback', 'runtime_switch', 'tag_write', 'ledger_write', 'config_write', 'restart'];

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object')
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
  return value;
}

function digest(value) {
  return crypto.createHash('sha256').update(JSON.stringify(stable(value))).digest('hex');
}

function isTime(value, nowMs) {
  const timestamp = Date.parse(value ?? '');
  return Number.isFinite(timestamp) && timestamp <= nowMs + 5_000;
}

/** Build an evidence-only snapshot. Missing facts remain explicit and never become READY. */
export function buildRecoveryDiagnosticsSnapshot(input, { nowMs = Date.now() } = {}) {
  const missingEvidence = [];
  const blockers = [];
  const incident = input?.incident ?? {};
  const ledger = input?.ledger ?? {};
  const runtime = input?.runtime ?? {};
  const workers = input?.workers ?? {};
  const backup = input?.backup ?? {};
  const locks = input?.locks ?? {};
  const productionTag = input?.productionTag ?? {};

  if (input?.candidateSha !== ME38_RECOVERY_INCIDENT.candidateSha
      || incident.runId !== ME38_RECOVERY_INCIDENT.runId
      || String(incident.attempt) !== ME38_RECOVERY_INCIDENT.attempt
      || incident.candidateSha !== ME38_RECOVERY_INCIDENT.candidateSha
      || incident.baselineSha !== ME38_RECOVERY_INCIDENT.baselineSha
      || incident.failureStage !== 'POST_SWITCH'
      || incident.status !== 'RECOVERY_REQUIRED'
      || !SHA256.test(incident.manifestSha256 ?? '')) {
    throw new Error('Recovery diagnostics are pinned to the verified ME-38 post-switch incident');
  }
  if ((ledger.productionSha != null && ledger.productionSha !== ME38_RECOVERY_INCIDENT.baselineSha)
      || (ledger.state?.currentProductionSha != null && ledger.state.currentProductionSha !== ME38_RECOVERY_INCIDENT.baselineSha)
      || incident.candidateSha !== ME38_RECOVERY_INCIDENT.candidateSha
      || input.candidateSha !== incident.candidateSha
      || (runtime.releaseEnvSha != null && runtime.releaseEnvSha !== ME38_RECOVERY_INCIDENT.candidateSha)) {
    throw new Error('Recovery diagnostics refuse any drift other than the known ME-38 incident drift');
  }

  const requireFact = (condition, name) => { if (!condition) missingEvidence.push(name); };
  requireFact(isTime(input.observedAt, nowMs), 'snapshot.observedAt');
  requireFact(isTime(ledger.observedAt, nowMs), 'ledger.observedAt');
  requireFact(SHA40.test(ledger.headSha ?? ''), 'ledger.headSha');
  requireFact(SHA40.test(ledger.productionSha ?? ''), 'ledger.productionSha');
  requireFact(SHA256.test(ledger.stateSha256 ?? ''), 'ledger.stateSha256');
  requireFact(SHA40.test(ledger.refTargetSha ?? '') || ledger.refTargetSha === null, 'ledger.refTargetSha');

  requireFact(isTime(runtime.observedAt, nowMs), 'runtime.observedAt');
  requireFact(runtime.releaseEnvSha === ME38_RECOVERY_INCIDENT.candidateSha, 'runtime.releaseEnvSha');
  requireFact(runtime.miniappSha === ME38_RECOVERY_INCIDENT.candidateSha, 'runtime.miniappSha');
  requireFact(runtime.databaseReadOnlyQueryExitCode === 0, 'runtime.databaseReadOnlyQueryExitCode');
  const services = runtime.services ?? {};
  for (const service of REQUIRED_SERVICES) {
    const value = services[service];
    requireFact(value && typeof value.running === 'boolean' && typeof value.health === 'string'
      && SHA40.test(value.sourceSha ?? '') && /^sha256:[a-f0-9]{64}$/.test(value.imageId ?? ''), `runtime.services.${service}`);
  }
  const heads = runtime.migrationHeads ?? {};
  for (const name of ['prisma', 'agata', 'gsales'])
    requireFact(typeof heads[name] === 'string' && heads[name].length > 0, `runtime.migrationHeads.${name}`);
  requireFact(SHA256.test(heads.gsalesMigrationSha256 ?? ''), 'runtime.migrationHeads.gsalesMigrationSha256');

  requireFact(isTime(workers.observedAt, nowMs), 'workers.observedAt');
  const lanes = workers.lanes ?? {};
  const workerChecks = [];
  for (const lane of ['GPOINT', 'GSALES']) {
    const value = lanes[lane];
    requireFact(value && typeof value.status === 'string' && typeof value.sourceId === 'string'
      && value.queueSnapshotState === 'VERIFIED' && isTime(value.snapshotAt, nowMs)
      && Number.isSafeInteger(value.pending) && value.pending >= 0
      && Number.isSafeInteger(value.running) && value.running >= 0, `workers.lanes.${lane}`);
    if (value) {
      const fresh = nowMs - Date.parse(value.snapshotAt ?? '') <= 90_000;
      const identityMatches = value.sourceId === `agata-worker-queue-status-v1:${lane.toLowerCase()}`;
      workerChecks.push(value.status === 'HEALTHY' && fresh && identityMatches && value.pending === 0 && value.running === 0);
    } else workerChecks.push(false);
  }

  requireFact(isTime(backup.observedAt, nowMs)
    && ['VERIFIED', 'MISSING', 'UNVERIFIED'].includes(backup.status), 'backup.status');
  if (backup.status === 'VERIFIED') {
    requireFact(typeof backup.path === 'string' && backup.path.startsWith('/opt/gpoint/backups/')
      && SHA256.test(backup.sha256 ?? '') && backup.mode === '600' && backup.readback === 'PASS', 'backup.integrity');
  }
  requireFact(isTime(locks.observedAt, nowMs) && locks.local?.checked === true
    && typeof locks.local.held === 'boolean' && locks.github?.checked === true
    && Number.isSafeInteger(locks.github.activeConflicts) && locks.github.activeConflicts >= 0, 'locks');
  requireFact(isTime(productionTag.observedAt, nowMs) && productionTag.baselineTagChecked === true
    && productionTag.candidateTagChecked === true
    && typeof productionTag.name === 'string' && productionTag.name.length > 0
    && /^gpoint-production-\d{8}T\d{9}Z$/.test(productionTag.candidateTagName ?? '')
    && (productionTag.targetSha === null || SHA40.test(productionTag.targetSha))
    && (productionTag.candidateTagTargetSha === null || SHA40.test(productionTag.candidateTagTargetSha)), 'productionTag');
  requireFact(isTime(input.incidentEvidence?.observedAt, nowMs)
    && input.incidentEvidence?.verified === true
    && input.incidentEvidence?.runId === ME38_RECOVERY_INCIDENT.runId
    && input.incidentEvidence?.attempt === ME38_RECOVERY_INCIDENT.attempt
    && SHA256.test(input.incidentEvidence?.assetSha256 ?? '')
    && SHA256.test(input.incidentEvidence?.resultAssetSha256 ?? '')
    && SHA256.test(input.incidentEvidence?.prepareBundleDigest ?? '')
    && /^\d+$/.test(input.incidentEvidence?.prepareRunId ?? '')
    && /^\d+$/.test(input.incidentEvidence?.prepareRunAttempt ?? ''), 'incidentEvidence');

  const sourceParity = REQUIRED_SERVICES.every(name => services[name]?.sourceSha === ME38_RECOVERY_INCIDENT.candidateSha);
  if (!sourceParity) blockers.push('RUNTIME_IMAGE_SOURCE_PARITY_FAILED');
  if (runtime.miniappSha !== ME38_RECOVERY_INCIDENT.candidateSha) blockers.push('MINIAPP_SOURCE_PARITY_FAILED');
  if (REQUIRED_SERVICES.some(name => services[name]?.running !== true || services[name]?.health !== 'healthy'))
    blockers.push('PRODUCTION_SERVICE_NOT_HEALTHY');
  const workersReady = workerChecks.length === 2 && workerChecks.every(Boolean);
  if (!workersReady) blockers.push('WORKER_LANES_NOT_VERIFIED_HEALTHY');
  if (backup.status !== 'VERIFIED') blockers.push(backup.status === 'MISSING' ? 'PROTECTED_BACKUP_MISSING' : 'PROTECTED_BACKUP_UNVERIFIED');
  if (locks.local?.held === true || locks.github?.activeConflicts !== 0) blockers.push('RELEASE_LOCK_CONFLICT');
  if (productionTag.targetSha !== ME38_RECOVERY_INCIDENT.baselineSha) blockers.push('PRODUCTION_TAG_BASELINE_NOT_VERIFIED');
  if (productionTag.candidateTagTargetSha != null && productionTag.candidateTagTargetSha !== ME38_RECOVERY_INCIDENT.candidateSha)
    blockers.push('CANDIDATE_PRODUCTION_TAG_CONFLICT');

  const status = missingEvidence.length ? 'INCOMPLETE' : 'FACTS_COMPLETE';
  if (missingEvidence.length) blockers.push('READ_ONLY_SNAPSHOT_INCOMPLETE');
  blockers.push('FINALIZATION_REQUIRES_SEPARATE_OWNER_AUTHORIZATION');
  const snapshot = {
    schema: 'gpoint-recovery-read-only-snapshot-v1',
    mode: 'recovery-diagnostics-read-only',
    status,
    releaseState: 'RECOVERY_DIAGNOSTICS_ONLY',
    candidateSha: input.candidateSha,
    incident: { ...incident },
    incidentEvidence: { ...input.incidentEvidence },
    ledger: { ...ledger },
    drift: { kind: 'KNOWN_POST_SWITCH_RUNTIME_AHEAD_OF_LEDGER', runtimeSha: runtime.releaseEnvSha,
      ledgerSha: ledger.productionSha },
    runtime: { ...runtime, imageParity: sourceParity },
    workers: { ...workers, ready: workersReady },
    backup: { ...backup },
    locks: { ...locks },
    productionTag: { ...productionTag },
    missingEvidence,
    blockers: [...new Set(blockers)],
    factsComplete: status === 'FACTS_COMPLETE',
    finalizationReady: false,
    mutationPolicy: { allowed: ['read_only_observation', 'write_diagnostic_snapshot_artifact'], forbidden: MUTATIONS },
  };
  return { ...snapshot, snapshotSha256: digest(snapshot) };
}

export function verifyRecoveryDiagnosticsSnapshot(snapshot) {
  if (!snapshot || snapshot.schema !== 'gpoint-recovery-read-only-snapshot-v1'
      || snapshot.mode !== 'recovery-diagnostics-read-only' || !SHA256.test(snapshot.snapshotSha256 ?? '')) return false;
  const { snapshotSha256, ...body } = snapshot;
  return digest(body) === snapshotSha256;
}

/** Execute a diagnostics-only collection. There is deliberately no mutation adapter. */
export async function runRecoveryDiagnostics({ read, persistSnapshot, requestedOperations = ['read_only'] }) {
  if (typeof read !== 'function' || typeof persistSnapshot !== 'function')
    throw new Error('Recovery diagnostics requires read and diagnostic-artifact adapters');
  if (JSON.stringify(requestedOperations) !== JSON.stringify(['read_only']))
    throw new Error('Recovery diagnostics read-only mode forbids production mutations');
  const snapshot = buildRecoveryDiagnosticsSnapshot(await read());
  await persistSnapshot(snapshot);
  return snapshot;
}
