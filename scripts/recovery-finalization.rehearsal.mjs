#!/usr/bin/env node
/**
 * Destructive only to a temporary Docker container and temporary local Git repo.
 * Run explicitly with ME38_EXISTING_EVIDENCE_DIR=/path/to/original/assets.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {execFileSync as rawExecFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {loadCompleteDeployInventory, runRecoveryFinalization, recoveryFinalizationIdentity,
  recoveryFinalizationIdentityFromFailureEvidence} from './recovery-finalization.mjs';
import {createRecoveryBackup} from './release-gpoint.mjs';
import {persistReleaseBookkeeping, verifyReleaseBookkeeping} from './release-bookkeeping.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const processAudit = [];
const FORBIDDEN_MUTATION = /docker\s+compose\s+(?:up|restart|stop|rm)|(?:prisma|alembic)\s+migrate\b|agata-production-migration\.py.{0,100}\bmigrate\b|runtime_switch|rollback|production (?:tag|ledger) push/i;
const ALLOWED_EXECUTABLES = new Set(['python3', 'git', 'docker', '/bin/bash']);
function execFileSync(command, args = [], options = {}) {
  const display = [command, ...args.map(value => String(value))].join(' ');
  processAudit.push({command, args: args.map(value => String(value))});
  assert.ok(ALLOWED_EXECUTABLES.has(command), 'rehearsal attempted an unapproved executable');
  assert.doesNotMatch(display, FORBIDDEN_MUTATION, 'rehearsal attempted a forbidden production mutation command');
  if (command === 'docker') {
    assert.ok(['run', 'exec', 'inspect', 'rm'].includes(String(args[0])), 'unexpected Docker operation');
    assert.match(display, /shared_postgres_1|--name shared_postgres_1/, 'Docker operation is not pinned to its isolated DB container');
    assert.doesNotMatch(display, /--network\s+(?!bridge\b|none\b)[^\s]+/,
      'rehearsal Docker operation must not attach to an application network');
    if (args[0] === 'exec') {
      let targetIndex = 1;
      while (String(args[targetIndex] ?? '').startsWith('-')) targetIndex += 1;
      assert.equal(String(args[targetIndex]), 'shared_postgres_1', 'Docker exec must target only the isolated database');
      assert.ok(['pg_isready', 'psql', 'pg_dump', 'createdb', 'pg_restore'].includes(String(args[targetIndex + 1])),
        'unexpected command inside the isolated database');
    }
    if (args[0] === 'inspect') assert.equal(String(args.at(-1)), 'shared_postgres_1');
    if (args[0] === 'rm') assert.equal(String(args.at(-1)), 'shared_postgres_1');
  }
  if (command === 'git') assert.doesNotMatch(display, /(?:https?:\/\/|ssh:\/\/|git@)/i,
    'rehearsal Git operation must stay local');
  return rawExecFileSync(command, args, options);
}
const sourceDir = process.env.ME38_EXISTING_EVIDENCE_DIR;
if (!sourceDir) throw new Error('ME38_EXISTING_EVIDENCE_DIR must point at the original immutable evidence assets');
const fixture = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/me38-recovery-incident-evidence.json'), 'utf8'));
const source = fixture.source;
const candidate = fixture.incidentResult.candidateSha;
const baseline = fixture.incidentResult.productionBaselineSha;
const failureReleaseId = fixture.incidentManifest.releaseId;
const identity = recoveryFinalizationIdentity(failureReleaseId, candidate);
const incidentManifestBase64 = execFileSync('python3', ['-c',
  `import base64,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); sys.stdout.write(base64.b64encode(z.read(${JSON.stringify(`manifests/${failureReleaseId}.json`)})).decode())`,
  path.join(sourceDir, 'manifests.zip')], {encoding: 'utf8'});
const incidentManifestBytes = Buffer.from(incidentManifestBase64, 'base64');
const incidentManifest = incidentManifestBytes.toString('utf8');
assert.equal(crypto.createHash('sha256').update(incidentManifestBytes).digest('hex'), source.incidentManifestSha256);

function git(cwd, args, input) {
  return execFileSync('git', ['-C', cwd, ...args], {encoding: 'utf8', input}).trim();
}
function sha256(value) { return crypto.createHash('sha256').update(value).digest('hex'); }
function writeJson(file, value) { fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, {mode: 0o600}); }
function assertNoMutationCommand(command) {
  assert.doesNotMatch(command, /docker\s+compose\s+(?:up|restart)|(?:prisma|alembic)\s+migrate\b|agata-production-migration\.py.{0,100}\bmigrate\b|runtime_switch|rollback|production (?:tag|ledger) push/i);
}

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'me38-finalization-rehearsal-'));
const backupPath = `/opt/gpoint/backups/gpoint-recovery-current-${candidate}-${Date.now()}.dump`;
const localDump = path.join(temp, 'protected-current.dump');
const containerName = 'shared_postgres_1';
let containerStarted = false;
let productionTagWrites = 0;
let ledgerWrites = 0;
const operationAudit = [];
const verifiedFailureFile = path.join(temp, 'verified-original-failure.json');

function docker(args, input) {
  return execFileSync('docker', args, {input, maxBuffer: 16 * 1024 * 1024});
}
function startDatabase() {
  const image = 'pgvector/pgvector:0.8.6-pg16';
  docker(['run', '-d', '--name', containerName,
    '--label', 'com.docker.compose.project=shared', '--label', 'com.docker.compose.service=postgres',
    '-e', 'POSTGRES_PASSWORD=me38-local-only', '-e', 'POSTGRES_DB=gpoint', image]);
  containerStarted = true;
  for (let tries = 0; tries < 40; tries += 1) {
    try { docker(['exec', containerName, 'pg_isready', '-U', 'postgres', '-d', 'gpoint']); break; }
    catch (error) { if (tries === 39) throw error; Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 250); }
  }
  docker(['exec', containerName, 'psql', '-U', 'postgres', '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', '-c',
    'CREATE SCHEMA agata; CREATE TABLE agata.messages(id integer PRIMARY KEY, body text NOT NULL); INSERT INTO agata.messages VALUES (1, \'local recovery rehearsal\'); CREATE TABLE public.recovery_fixture(id integer PRIMARY KEY); INSERT INTO public.recovery_fixture VALUES (1);']);
}

function backupRemoteRun(command) {
  if (command.includes("sys.argv=['agata-production-migration.py','backup'")) {
    const localPath = backupPath;
    const containerId = docker(['inspect', '--format', '{{.Id}}', containerName]).toString().trim();
    const escaped = localPath.replaceAll("'", "'\\''");
    const dump = docker(['exec', containerName, 'pg_dump', '-U', 'postgres', '--format=custom', '--no-owner',
      '--schema=public', '--schema=agata', '--dbname=gpoint']);
    fs.writeFileSync(localDump, dump, {mode: 0o600, flag: 'wx'});
    fs.chmodSync(localDump, 0o600);
    const actualSha = sha256(dump);
    const record = {status: 'COMPLETE_PROTECTED', database: 'gpoint', candidate_sha: candidate,
      postgres_container_id: containerId.slice(0, 12), postgres_container_name: `/${containerName}`,
      path: localPath, sha256: actualSha, schemas: ['public', 'agata'], extensions: []};
    // Prove the real archive can be restored to a second isolated database.
    docker(['exec', containerName, 'createdb', '-U', 'postgres', 'gpoint_restore']);
    docker(['exec', containerName, 'psql', '-U', 'postgres', '-d', 'gpoint_restore', '-v', 'ON_ERROR_STOP=1', '-c',
      'DROP SCHEMA public CASCADE']);
    docker(['exec', '-i', containerName, 'pg_restore', '-U', 'postgres', '--no-owner', '--dbname=gpoint_restore'], dump);
    const restored = docker(['exec', containerName, 'psql', '-U', 'postgres', '-d', 'gpoint_restore', '-Atc',
      'SELECT id || \':\' || body FROM agata.messages ORDER BY id']).toString().trim();
    assert.equal(restored, '1:local recovery rehearsal');
    return `GPOINT_COMPLETE_BACKUP=${JSON.stringify(record)}`;
  }

  if (command.includes('agata-production-migration.py')) throw new Error('Unexpected second backup creation command');
  assert.match(command, /RECOVERY_BACKUP_READBACK=PASS/);
  assertNoMutationCommand(command);
  const rewritten = command.replace(`file='${backupPath}'`, `file='${localDump.replaceAll("'", "'\\''")}'`);
  assert.notEqual(rewritten, command, 'verification command should map only the synthetic remote path to local dump');
  const shim = path.join(temp, 'bin'); fs.mkdirSync(shim, {recursive: true});
  fs.writeFileSync(path.join(shim, 'stat'), '#!/bin/sh\n[ "$1" = "-c" ] && [ "$2" = "%a" ] || exit 9\n/usr/bin/stat -f %Lp "$3"\n', {mode: 0o700});
  fs.writeFileSync(path.join(shim, 'sha256sum'), '#!/bin/sh\nshasum -a 256 "$1"\n', {mode: 0o700});
  const output = execFileSync('/bin/bash', ['-c', rewritten], {encoding: 'utf8',
    env: {...process.env, PATH: `${shim}:${process.env.PATH}`}, maxBuffer: 1024 * 1024});
  return output;
}

function stableLedgerSeed(repo, bare, ledgerDir, originalManifest) {
  const seed = path.join(temp, 'ledger-seed'); fs.mkdirSync(seed);
  execFileSync('git', ['init', seed], {stdio: 'ignore'});
  execFileSync('git', ['-C', seed, 'config', 'user.name', 'ME-38 rehearsal']);
  execFileSync('git', ['-C', seed, 'config', 'user.email', 'me38-rehearsal@example.invalid']);
  const initialState = {currentProductionSha: baseline, currentProductionRef: 'gpoint-production-previous',
    lastReleaseId: 'gpoint-release-previous', lastReleaseStatus: 'COMPLETE', recoveryRequired: false};
  const seedState = path.join(seed, 'docs/release/state/production.json'); writeJson(seedState, initialState);
  const oldName = `${failureReleaseId}.json`;
  writeJson(path.join(seed, 'docs/release/manifests', oldName), JSON.parse(originalManifest));
  execFileSync('git', ['-C', seed, 'add', '.']);
  execFileSync('git', ['-C', seed, 'commit', '-m', 'seed isolated test ledger'], {stdio: 'ignore'});
  execFileSync('git', ['-C', seed, 'remote', 'add', 'origin', bare]);
  execFileSync('git', ['-C', seed, 'push', 'origin', `HEAD:refs/heads/gpoint-release-ledger`], {stdio: 'ignore'});
  const stateFile = path.join(ledgerDir, 'state/production.json'); writeJson(stateFile, initialState);
  const manifestDir = path.join(ledgerDir, 'manifests'); fs.mkdirSync(manifestDir, {recursive: true});
  fs.writeFileSync(path.join(manifestDir, oldName), originalManifest, {mode: 0o600});
  return {stateFile, manifestDir, state: initialState, oldName};
}

function completeInventory({conflict = false} = {}) {
  const runs = Array.from({length: 116}, (_, index) => ({id: String(index + 1), status: 'completed',
    path: '.github/workflows/gpoint-deploy.yml@refs/heads/main', head_sha: candidate, run_attempt: 1, event: 'workflow_dispatch'}));
  if (conflict) runs[110] = {...runs[110], status: 'queued'};
  return async (page, perPage) => {
    const start = (page - 1) * perPage;
    return {ok: true, json: async () => ({total_count: runs.length, workflow_runs: runs.slice(start, start + perPage)})};
  };
}

try {
  execFileSync('python3', ['-m', 'unittest', 'tests.test_me38_recovery_immutable_rehearsal'], {
    cwd: root, encoding: 'utf8', env: {...process.env, ME38_EXISTING_EVIDENCE_DIR: sourceDir,
      ME38_VERIFIED_FAILURE_OUTPUT: verifiedFailureFile},
    maxBuffer: 2 * 1024 * 1024, stdio: 'inherit',
  });
  const importedFailure = JSON.parse(fs.readFileSync(verifiedFailureFile, 'utf8'));
  assert.equal(importedFailure.result.candidateSha, candidate);
  assert.equal(importedFailure.result.productionBaselineSha, baseline);
  assert.equal(importedFailure.failureManifestSha256, source.incidentManifestSha256);
  assert.equal(importedFailure.prepare.bundleDigest, `sha256:${source.prepare.bundleSha256}`);
  assert.equal(importedFailure.run.id, String(fixture.incidentResult.automationRunId));
  assert.equal(importedFailure.run.attempt, String(fixture.incidentResult.automationRunAttempt));
  const verifiedCandidate = importedFailure.result.candidateSha;
  const verifiedBaseline = importedFailure.result.productionBaselineSha;
  const verifiedFailureReleaseId = importedFailure.failureManifest.releaseId;
  const verifiedIdentity = recoveryFinalizationIdentityFromFailureEvidence(
    importedFailure.failureManifest, importedFailure.result, verifiedCandidate);
  assert.equal(verifiedFailureReleaseId, failureReleaseId);
  startDatabase();
  const candidateObject = execFileSync('git', ['-C', root, 'cat-file', '-e', `${candidate}^{commit}`]);
  assert.equal(candidateObject.length, 0, 'incident candidate object must be available for exact local tag');

  // The generated command is inspected before a local adapter creates the same kind of isolated DB dump.
  let backupCommandSeen = false;
  const originalRemote = backupRemoteRun;
  const remoteRun = command => {
    if (!backupCommandSeen) {
      assert.match(command, /sys\.argv=\['agata-production-migration\.py','backup'/);
      assert.match(command, /'--backup-purpose','recovery-current'/);
      assertNoMutationCommand(command);
      operationAudit.push('create-protected-current-backup');
      backupCommandSeen = true;
    }
    return originalRemote(command);
  };
  const heads = {LIVE_PRISMA_HEAD: '20261015130000_codex_telegram_challenge', LIVE_AGATA_HEAD: 'agata0011',
    LIVE_GSALES_HEAD: '001_gsales', LIVE_GSALES_MIGRATION_SHA256: '8de19ec2efd81848010379645d51243ac3f4a4cd37af0be211d71cdf36bcd111'};
  const blockedOperations = {backup: 0, tag: 0, ledger: 0};
  const rejectBeforeAnyMutation = async () => runRecoveryFinalization({
    readSnapshot: async () => {
      const inventoryBeforeMutation = await loadCompleteDeployInventory({
        fetchPage: completeInventory({conflict: true}), currentRunId: '116'});
      return {...snapshotState, concurrentRelease: inventoryBeforeMutation.workflow_runs.some(
        run => run.status !== 'completed' && String(run.id) !== '116')};
    },
    persistRecoveryRequired: async () => { blockedOperations.ledger += 1; },
    ensureProductionTag: async () => { blockedOperations.tag += 1; },
    persistCompleteState: async () => { blockedOperations.ledger += 1; },
  });
  await assert.rejects(rejectBeforeAnyMutation(), /another Deploy/);
  assert.deepEqual(blockedOperations, {backup: 0, tag: 0, ledger: 0},
    'state-machine conflict must stop before backup, tag, or Ledger callbacks');
  const preflightThenBackup = async fetchPageForPreflight => {
    const complete = await loadCompleteDeployInventory({fetchPage: fetchPageForPreflight, currentRunId: '116'});
    assert.equal(complete.total_count, 116);
    blockedOperations.backup += 1;
    return createRecoveryBackup(candidate, baseline, heads, {remoteRun, readFacts: () => heads});
  };
  await assert.rejects(preflightThenBackup(completeInventory({conflict: true})), /another Deploy/);
  assert.deepEqual(blockedOperations, {backup: 0, tag: 0, ledger: 0},
    'a conflicting Deploy must stop before backup, tag, or ledger operations');
  const conflictBlockedOperations = {...blockedOperations};
  const backup = await preflightThenBackup(completeInventory());
  assert.equal(backup.verified, true);
  operationAudit.push('verify-protected-current-backup');
  assert.equal(fs.statSync(localDump).mode & 0o777, 0o600);
  assert.equal(backup.sha256, sha256(fs.readFileSync(localDump)));
  assert.match(docker(['exec', '-i', containerName, 'pg_restore', '--list'], fs.readFileSync(localDump)).toString(), /TABLE DATA agata messages/);

  const bare = path.join(temp, 'ledger.git');
  execFileSync('git', ['init', '--bare', bare], {stdio: 'ignore'});
  const workingRepo = path.join(temp, 'work');
  execFileSync('git', ['clone', '--shared', root, workingRepo], {stdio: 'ignore'});
  git(workingRepo, ['remote', 'set-url', 'origin', bare]);
  const ledgerDir = path.join(temp, 'ledger-view');
  const ledger = stableLedgerSeed(workingRepo, bare, ledgerDir, incidentManifest);
  git(workingRepo, ['checkout', '--detach', candidate]);
  const ledgerGit = (args, input) => git(workingRepo, args, input);
  const ledgerHead = () => git(workingRepo, ['ls-remote', 'origin', 'refs/heads/gpoint-release-ledger']).split(/\s+/)[0];
  assert.equal(git(workingRepo, ['remote', 'get-url', 'origin']), bare);
  ledger.headSha = ledgerHead();
  const writeLedger = (state, records, evidence, expectedParent) => {
    writeJson(ledger.stateFile, state);
    for (const record of records) writeJson(path.join(ledger.manifestDir, record.name), record.data);
    const recordSet = fs.readdirSync(ledger.manifestDir).filter(name => name.endsWith('.json'))
      .map(name => ({name, data: JSON.parse(fs.readFileSync(path.join(ledger.manifestDir, name), 'utf8'))}));
    persistReleaseBookkeeping({mode: 'production', records: recordSet, evidence, git: ledgerGit,
      stateFile: ledger.stateFile, manifestDir: ledger.manifestDir, ledgerSha: expectedParent});
    ledgerWrites += 1;
    operationAudit.push('append-recovery-ledger');
    return verifyReleaseBookkeeping({git: ledgerGit, stateFile: ledger.stateFile,
      manifestDir: ledger.manifestDir, expectedState: state}).headSha;
  };

  let snapshotState = {
    candidateSha: candidate,
    failedRelease: {releaseId: importedFailure.failureManifest.releaseId,
      candidateSha: importedFailure.result.candidateSha,
      previousProductionSha: importedFailure.result.productionBaselineSha,
      status: importedFailure.result.status, failureStage: importedFailure.result.failureStage,
      runId: importedFailure.run.id, attempt: importedFailure.run.attempt,
      failureManifestAssetDigest: importedFailure.evidence.manifestDigest,
      failureManifestSha256: importedFailure.failureManifestSha256},
    ledger: {headSha: ledgerHead(), state: ledger.state}, productionRefTarget: baseline, productionTagTarget: null,
    reconciliationManifest: null, finalizationManifest: null, vpsReachable: true, githubReachable: true,
    concurrentRelease: false,
    runtime: {observedAt: new Date().toISOString(), sourceSha: candidate, healthy: true, imageParity: true,
      databaseHeadsVerified: true, workerLanesVerified: true, prismaHead: heads.LIVE_PRISMA_HEAD,
      agataHead: heads.LIVE_AGATA_HEAD, gsalesHead: heads.LIVE_GSALES_HEAD},
    ownerAuthorization: {valid: true, candidateSha: candidate, baselineSha: baseline, operation: 'FINALIZATION_ONLY'},
    databaseRiskDecision: {accepted: true, candidateSha: candidate, baselineSha: baseline,
      instructionId: 'ME-38-DB-HISTORICAL-UNCERTAINTY-20261017-01'}, backup,
  };
  let reconciliation = null, finalManifest = null;
  const fetchPage = completeInventory();
  const inventory = await loadCompleteDeployInventory({fetchPage, currentRunId: '116'});
  assert.equal(inventory.total_count, 116);
  assert.equal(inventory.workflow_runs.length, 116);

  const manifest = (value, name) => {
    const bytes = Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
    fs.writeFileSync(path.join(ledger.manifestDir, name), bytes, {flag: 'wx', mode: 0o600});
    return {...value, name, contentSha256: sha256(bytes)};
  };
  const persistRecoveryRequired = async ({snapshot, identity: stable, expectedLedgerHead}) => {
    assert.equal(ledgerHead(), expectedLedgerHead);
    const originalPath = path.join(ledger.manifestDir, ledger.oldName);
    assert.equal(sha256(fs.readFileSync(originalPath)), source.incidentManifestSha256);
    reconciliation = manifest({releaseId: stable.reconciliationReleaseId, eventType: 'RECOVERY_STATE_RECONCILIATION',
      failedReleaseId: verifiedFailureReleaseId, failedDeployRunId: importedFailure.run.id,
      failedDeployRunAttempt: importedFailure.run.attempt,
      failureManifestAssetDigest: importedFailure.evidence.manifestDigest,
      failureManifestSha256: snapshot.failedRelease.failureManifestSha256, candidateSha: candidate,
      previousProductionSha: verifiedBaseline, productionShaAfter: null, status: 'RECOVERY_REQUIRED',
      failureStage: 'POST_SWITCH', backup}, stable.reconciliationManifestName);
    ledger.state = {...snapshot.ledger.state, currentProductionSha: baseline,
      lastReleaseId: verifiedFailureReleaseId, lastReleaseStatus: 'RECOVERY_REQUIRED', recoveryRequired: true,
      lastReleaseFailureManifest: `docs/release/manifests/${ledger.oldName}`,
      recoveryReconciliationManifest: `docs/release/manifests/${stable.reconciliationManifestName}`,
      recoveryReconciliationManifestSha256: reconciliation.contentSha256,
      recoveryFinalizationManifest: null};
    ledger.headSha = writeLedger(ledger.state, [{name: stable.reconciliationManifestName, data: reconciliation}], reconciliation, expectedLedgerHead);
    snapshotState = {...snapshotState, ledger: {headSha: ledger.headSha, state: ledger.state}, reconciliationManifest: reconciliation};
  };
  const ensureProductionTag = async ({snapshot, identity: stable, expectedLedgerHead}) => {
    assert.equal(ledgerHead(), expectedLedgerHead);
    assert.equal(snapshot.productionTagTarget, null);
    git(workingRepo, ['tag', '-a', stable.productionTag, candidate, '-m', 'isolated ME-38 recovery rehearsal']);
    operationAudit.push('write-exact-local-production-tag');
    assert.equal(git(workingRepo, ['rev-parse', `refs/tags/${stable.productionTag}^{}`]), candidate);
    productionTagWrites += 1;
    snapshotState = {...snapshotState, productionTagTarget: candidate};
  };
  const persistCompleteState = async ({snapshot, identity: stable, expectedLedgerHead}) => {
    assert.equal(ledgerHead(), expectedLedgerHead);
    assert.equal(snapshot.productionTagTarget, candidate);
    finalManifest = manifest({releaseId: stable.releaseId, eventType: 'RECOVERY_FINALIZATION',
      failedReleaseId: verifiedFailureReleaseId, failedDeployRunId: importedFailure.run.id,
      failedDeployRunAttempt: importedFailure.run.attempt,
      failedDeployManifestDigest: snapshot.failedRelease.failureManifestAssetDigest,
      failedDeployManifestSha256: snapshot.failedRelease.failureManifestSha256,
      candidateSha: verifiedCandidate, previousProductionSha: verifiedBaseline, productionShaAfter: verifiedCandidate,
      status: 'COMPLETE', productionRef: stable.productionTag, runtimeSwitchOccurred: false,
      databaseApplyOccurred: false, inferenceAcceptance: 'NOT_VERIFIED', e2eAcceptance: 'NOT_VERIFIED',
      dbHeadAfter: heads.LIVE_PRISMA_HEAD, agataDbHeadAfter: heads.LIVE_AGATA_HEAD,
      gsalesDbHeadAfter: heads.LIVE_GSALES_HEAD, backup}, stable.manifestName);
    ledger.state = {...ledger.state, currentProductionSha: candidate, currentProductionRef: stable.productionTag,
      previousProductionSha: verifiedBaseline, lastReleaseId: verifiedFailureReleaseId, lastReleaseStatus: 'COMPLETE',
      recoveryRequired: false, recoveryReconciliationManifest: `docs/release/manifests/${stable.reconciliationManifestName}`,
      recoveryReconciliationManifestSha256: reconciliation.contentSha256,
      recoveryFinalizationManifest: `docs/release/manifests/${stable.manifestName}`,
      recoveryFinalizationManifestSha256: finalManifest.contentSha256};
    ledger.headSha = writeLedger(ledger.state, [{name: stable.manifestName, data: finalManifest}], finalManifest, expectedLedgerHead);
    snapshotState = {...snapshotState, productionRefTarget: candidate,
      ledger: {headSha: ledger.headSha, state: ledger.state}, finalizationManifest: finalManifest};
  };
  const completeFetch = completeInventory();
  const result = await runRecoveryFinalization({readSnapshot: async () => {
    const freshInventory = await loadCompleteDeployInventory({fetchPage: completeFetch, currentRunId: '116'});
    assert.equal(freshInventory.total_count, 116);
    snapshotState = {...snapshotState, ledger: {headSha: ledgerHead(), state: ledger.state}};
    return snapshotState;
  }, persistRecoveryRequired, ensureProductionTag, persistCompleteState});
  assert.equal(result.status, 'COMPLETE');
  assert.equal(productionTagWrites, 1);
  assert.equal(ledgerWrites, 2);
  assert.deepEqual(operationAudit, ['create-protected-current-backup', 'verify-protected-current-backup',
    'append-recovery-ledger', 'write-exact-local-production-tag', 'append-recovery-ledger']);
  assert.doesNotMatch(operationAudit.join('\n'), /apply|migration|runtime.switch|rollback/i);
  assert.ok(processAudit.length > 0, 'subprocess audit must observe rehearsal commands');
  assert.equal(processAudit.filter(({command, args}) => command === 'git' && args.includes('push')).length, 3,
    'only the initial local ledger seed and two append-only local ledger commits are expected');
  assert.equal(processAudit.filter(({command, args}) => command === 'docker' && args[0] === 'exec'
    && /\b(?:migrate|compose|restart|stop|rm)\b/i.test(args.join(' '))).length, 0,
  'no Docker migration or runtime-control command may run');
  assert.equal(git(workingRepo, ['rev-parse', `refs/tags/${verifiedIdentity.productionTag}^{}`]), verifiedCandidate);
  assert.equal(verifyReleaseBookkeeping({git: ledgerGit, stateFile: ledger.stateFile,
    manifestDir: ledger.manifestDir, expectedState: ledger.state}).headSha, ledger.headSha);
  assert.equal(ledger.state.lastReleaseStatus, 'COMPLETE');
  assert.equal(finalManifest.runtimeSwitchOccurred, false);
  assert.equal(finalManifest.databaseApplyOccurred, false);

  // A second-page conflict fails before any backup, tag, or ledger mutation.
  const beforeNoMutation = {tagWrites: productionTagWrites, ledgerWrites, state: JSON.stringify(ledger.state)};
  await assert.rejects(loadCompleteDeployInventory({fetchPage: completeInventory({conflict: true}), currentRunId: '116'}),
    /another Deploy/);
  assert.deepEqual({tagWrites: productionTagWrites, ledgerWrites, state: JSON.stringify(ledger.state)}, beforeNoMutation);

  process.stdout.write(JSON.stringify({status: 'LOCAL_RECOVERY_REHEARSAL_PASS', inventoryRuns: 116,
    immutableAssetsVerified: 3, originalFailureImporter: 'PASS', postgresBackupRestore: 'PASS',
    backupSha256: backup.sha256, localProductionTagTarget: candidate,
    localLedgerHead: ledger.state.headSha, ledgerWrites, productionTagWrites,
    operationAudit, conflictBlockedOperations, subprocessesAudited: processAudit.length,
    forbiddenProductionMutationCommands: processAudit.filter(({command, args}) =>
      FORBIDDEN_MUTATION.test([command, ...args].join(' '))).length,
    e2eAcceptance: 'NOT_VERIFIED'}) + '\n');
} finally {
  if (containerStarted) {
    try { docker(['rm', '-f', containerName]); } catch { /* container cleanup is best effort after evidence collection */ }
  }
  fs.rmSync(temp, {recursive: true, force: true});
}
