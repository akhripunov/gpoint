import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { canCreateProductionRef, checkArtifactParity, checkEffectiveConfig, classifyReleaseFailure, evaluateSafetyGates, failureEvidence, releaseStateAfterError, shouldFinalizeRecovery, validateCandidate, verifiedRuntimeState } from './release-gpoint.mjs';
import { agataInternalConfigProvisionCommand, candidateBuildCacheCleanupCommand, classifyPostSwitchState, createSourceArchive, databaseNetworkArgs, detectorNeedsRebuild, detectorSourceHash, exactCheckout, GPOINT_DATABASE_NETWORK, migrationNeedsDeploy, planReleaseRetention, resolveRunnerPath, runRemoteCommand, shouldReuseCandidateImage, SOURCE_ARCHIVE_EXCLUDES, upload, validateAgataProviderConfig, validateAgataRuntimeIdentity, validateRuntimeIdentity } from './release-gpoint-vps-adapter.mjs';

const sha = '4d99e0da856d2ffe174c54d7ae7f2f971ab462f5';
const artifacts = { api: { sourceSha: sha }, worker: { sourceSha: sha }, bot: { sourceSha: sha }, miniapp: { sourceSha: sha } };
const config = { api: sha, worker: sha, bot: sha, miniapp: sha };

test('durable runner config is discovered in a fresh environment and explicit env wins', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-runner-config-test-'));
  const configFile = path.join(root, 'release-tools.env');
  fs.writeFileSync(configFile, 'GPOINT_REMOTE_RUNNER=$HOME/.local/bin/gpoint-vps-runner\nGPOINT_SCP_RUNNER=~/bin/gpoint-vps-scp-runner\n');
  try {
    assert.equal(resolveRunnerPath('GPOINT_REMOTE_RUNNER', { env: {}, configFile, home: root }), path.join(root, '.local/bin/gpoint-vps-runner'));
    assert.equal(resolveRunnerPath('GPOINT_SCP_RUNNER', { env: {}, configFile, home: root }), path.join(root, 'bin/gpoint-vps-scp-runner'));
    assert.equal(resolveRunnerPath('GPOINT_REMOTE_RUNNER', { env: { GPOINT_REMOTE_RUNNER: '/explicit/runner' }, configFile, home: root }), '/explicit/runner');
    assert.equal(resolveRunnerPath('UNKNOWN_RUNNER', { env: {}, configFile, home: root }), null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

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
test('pre-switch Docker Compose preflight error is not misclassified as a runtime switch', () => {
  const failure = classifyReleaseFailure({ command: 'docker compose version failed', switchStarted: false });
  assert.equal(failure.switched, false);
  assert.equal(failure.releaseState, 'FAILED_PRE_SWITCH');
  assert.equal(failure.failureStage, 'PRE_SWITCH');
});
test('held release lock rejects a concurrent release', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, concurrent: true, recordedProductionSha: sha, liveProductionSha: sha }).includes('concurrent release lock is held')));
test('recorded/live production drift blocks', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: '0'.repeat(40), liveProductionSha: sha }).includes('live production SHA drift')));
test('remote production ref mismatch blocks completion', () => assert.ok(evaluateSafetyGates({ candidateSha: sha, remoteSha: sha, artifacts, config, recordedProductionSha: sha, liveProductionSha: sha, productionRefTarget: '0'.repeat(40) }).includes('production ref target mismatch')));
test('runtime identity blocks a previous image before release completion', () => {
  const imageId = 'sha256:' + 'a'.repeat(64);
  const previousImage = 'sha256:' + 'b'.repeat(64);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: previousImage, workerImage: imageId, botImage: imageId }), ['apiImage does not use candidate image']);
  assert.deepEqual(validateRuntimeIdentity(sha, { imageId, buildSha: sha, apiImage: imageId, workerImage: imageId, botImage: imageId }), []);
});

test('AGATA runtime requires candidate image parity and healthy API/worker', () => {
  const imageId = 'sha256:' + 'c'.repeat(64);
  const runtime = { imageId, buildSha: sha, apiImage: imageId, workerImage: imageId, apiRunning: 'true', apiHealth: 'healthy', workerRunning: 'true', workerHealth: 'healthy' };
  assert.deepEqual(validateAgataRuntimeIdentity(sha, runtime), []);
  assert.ok(validateAgataRuntimeIdentity(sha, { ...runtime, workerHealth: 'starting' }).some(x => x.includes('worker')));
  assert.ok(validateAgataRuntimeIdentity('0'.repeat(40), runtime).some(x => x.includes('build SHA')));
});

test('AGATA provider-disabled config has internal keys but never requires LLM credentials', () => {
  const valid = { enabled: 'true', provider: 'disabled', databaseUrlPresent: true, tenant: true, internalAuth: true, messageKey: true, requestHmac: true, retrievalHmac: true, responseHmac: true, shadowHmac: true };
  assert.deepEqual(validateAgataProviderConfig(valid), []);
  assert.ok(validateAgataProviderConfig({ ...valid, provider: 'anymodel' }).some(x => x.includes('disabled')));
  const provision = agataInternalConfigProvisionCommand();
  const script = Buffer.from(provision.match(/printf '%s' '([^']+)'/)[1], 'base64').toString();
  assert.match(script, /AGATA_LLM_PROVIDER.*disabled/);
  assert.doesNotMatch(script, /AGATA_LLM_API_KEY=/);
});

test('all disposable Prisma/AGATA database runners use shared_internal', () => {
  const adapter = fs.readFileSync(new URL('./release-gpoint-vps-adapter.mjs', import.meta.url), 'utf8');
  assert.equal(GPOINT_DATABASE_NETWORK, 'shared_internal');
  assert.equal(databaseNetworkArgs('gpoint-preflight'), '--network shared_internal --network-alias gpoint-preflight');
  assert.ok((adapter.match(/\$\{databaseNetworkArgs\(/g) ?? []).length >= 8);
  assert.doesNotMatch(adapter, /--network gpoint_app --network-alias gpoint-(?:preflight|migration|agata-preflight|agata-migration|agata-verify)/);
});

test('release source archive excludes AppleDouble and Python cache artifacts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-source-archive-'));
  const checkout = path.join(root, 'checkout');
  const archive = path.join(root, 'source.tar.gz');
  fs.mkdirSync(path.join(checkout, 'services/agata/migrations/__pycache__'), { recursive: true });
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/env.py'), 'editable source');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/._env.py'), 'AppleDouble metadata');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/.DS_Store'), 'Finder metadata');
  fs.writeFileSync(path.join(checkout, 'services/agata/migrations/__pycache__/env.cpython-312.pyc'), 'bytecode');
  try {
    createSourceArchive(checkout, archive);
    const entries = execFileSync('tar', ['-tzf', archive], { encoding: 'utf8' });
    assert.match(entries, /checkout\/services\/agata\/migrations\/env\.py/);
    assert.doesNotMatch(entries, /\._env\.py|\.DS_Store|__pycache__|\.pyc/);
    assert.ok(SOURCE_ARCHIVE_EXCLUDES.includes('--exclude=*/._*'));
    assert.ok(SOURCE_ARCHIVE_EXCLUDES.includes('--exclude=*/__pycache__'));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('production runtime image includes and builds AGATA client before API', () => {
  const dockerfile = fs.readFileSync(new URL('../infra/production/Gpoint.Dockerfile', import.meta.url), 'utf8');
  assert.match(dockerfile, /COPY packages\/agata-client\/package\.json packages\/agata-client\/package\.json/);
  assert.ok(dockerfile.indexOf('RUN pnpm --filter @gpoint/agata-client build') < dockerfile.indexOf('RUN pnpm --filter @gpoint/api build'));
});

test('nonzero compose after candidate switch enters switched-unverified recovery', () => {
  const image = 'sha256:' + 'a'.repeat(64);
  assert.equal(classifyPostSwitchState({ candidate: sha, runtime: { imageId: image, buildSha: sha, apiImage: image, workerImage: image, botImage: image }, composeExitCode: 1 }).state, 'SWITCHED_UNVERIFIED');
  assert.equal(releaseStateAfterError({ switched: true, runtimeVerified: false }), 'SWITCHED_UNVERIFIED');
});

test('mixed runtime is recovery-required and cannot create a ref', () => {
  const image = 'sha256:' + 'a'.repeat(64);
  const old = 'sha256:' + 'b'.repeat(64);
  const state = classifyPostSwitchState({ candidate: sha, runtime: { imageId: image, buildSha: sha, apiImage: image, workerImage: old, botImage: image }, composeExitCode: 0 });
  assert.equal(state.state, 'RECOVERY_REQUIRED');
  assert.equal(canCreateProductionRef({ releaseState: 'RECOVERY_REQUIRED', candidate: sha, actualSha: sha, gatesPass: true }), false);
});

test('recovery can finalize exactly once only after all gates pass', () => {
  assert.equal(shouldFinalizeRecovery({ releaseState: 'RECOVERY_REQUIRED', candidate: sha, actualSha: sha, gatesPass: true }), true);
  assert.equal(canCreateProductionRef({ releaseState: 'VERIFIED', candidate: sha, actualSha: sha, mixedRuntime: false, health: true, dbVerified: true }), true);
  assert.equal(canCreateProductionRef({ releaseState: 'VERIFIED', candidate: sha, actualSha: sha, mixedRuntime: true, health: true, dbVerified: true }), false);
});

test('verified release reconciles activeRuntimeSha only after verified switch', () => {
  const prior = { currentProductionSha: 'a'.repeat(40), activeRuntimeSha: 'b'.repeat(40), activeRuntimeDbHead: 'old' };
  const next = verifiedRuntimeState(prior, { candidate: sha, dbHead: 'agata0007' });
  assert.equal(next.currentProductionSha, sha);
  assert.equal(next.activeRuntimeSha, sha);
  assert.equal(next.activeRuntimeDbHead, 'agata0007');
  assert.equal(prior.activeRuntimeSha, 'b'.repeat(40));
});

test('same candidate exact image is reusable and up-to-date migration is not redeployed', () => {
  assert.equal(shouldReuseCandidateImage({ candidate: sha, exactRuntimeImage: true }), true);
  assert.equal(migrationNeedsDeploy('Database schema is up to date!'), false);
  assert.equal(migrationNeedsDeploy('Following migrations have not yet been applied'), true);
});

test('release retention protects unresolved RECOVERY_REQUIRED candidate', () => {
  const plan = planReleaseRetention({
    currentSha: 'a'.repeat(40), rollbackSha: 'b'.repeat(40),
    candidates: [{ sha, status: 'RECOVERY_REQUIRED', recoveryRequired: true }],
    imageTags: [`gpoint-runtime:${sha}`, `gpoint-agata-runtime:${sha}`],
  });
  assert.deepEqual(plan.removableShas, []);
  assert.deepEqual(plan.protectedShas, ['a'.repeat(40), 'b'.repeat(40), sha].sort());
});

test('release retention removes a reconciled FAILED candidate and its attributable cache only', () => {
  const cacheId = 'cacheRecord_42';
  const plan = planReleaseRetention({
    currentSha: 'a'.repeat(40), rollbackSha: 'b'.repeat(40),
    candidates: [{ sha, status: 'FAILED_PRE_SWITCH', recoveryRequired: false }],
    imageTags: [`gpoint-runtime:${sha}`, `gpoint-agata-runtime:${sha}`],
    cacheRecords: [{ id: cacheId, candidateSha: sha, reclaimable: true }, { id: 'shared-cache', candidateSha: sha, reclaimable: false }],
  });
  assert.deepEqual(plan.removableShas, [sha]);
  assert.deepEqual(plan.removableCacheIds, [cacheId]);
});

test('release retention protects current and rollback SHAs', () => {
  const current = 'a'.repeat(40);
  const rollback = 'b'.repeat(40);
  const plan = planReleaseRetention({
    currentSha: current, rollbackSha: rollback,
    candidates: [{ sha: current, status: 'COMPLETE' }, { sha: rollback, status: 'COMPLETE' }],
    imageTags: [`gpoint-runtime:${current}`, `gpoint-runtime:${rollback}`],
  });
  assert.deepEqual(plan.removableShas, []);
});

test('release retention never plans volume removal', () => {
  const plan = planReleaseRetention({ candidates: [], imageTags: [], volumes: ['postgres-data'] });
  assert.deepEqual(plan.volumesToRemove, []);
  assert.equal(Object.hasOwn(plan, 'volumes'), false);
});

test('candidate cache cleanup is exact-SHA scoped and rejects ambiguous SHA', () => {
  const command = candidateBuildCacheCleanupCommand(sha);
  const encoded = command.match(/printf '%s' '([A-Za-z0-9+/=]+)'/)?.[1];
  const script = Buffer.from(encoded ?? '', 'base64').toString('utf8');
  assert.match(script, /docker','buildx/);
  assert.match(script, /CACHE_CLEANUP_SKIPPED=overlapping_unrelated_build/);
  assert.match(script, /id~=/);
  assert.match(script, /usage!='1'/);
  assert.throws(() => candidateBuildCacheCleanupCommand('not-a-sha'), /exact candidate SHA/);
});

test('detector identity is independent and rebuilds only when source inputs change', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-detector-test-'));
  const dir = path.join(root, 'infra/production/sensitive-detector');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'Dockerfile'), 'FROM node:22\n');
  fs.writeFileSync(path.join(dir, 'config.mjs'), 'export default {}\n');
  const first = detectorSourceHash(root);
  const same = detectorSourceHash(root);
  fs.writeFileSync(path.join(dir, 'config.mjs'), 'export default { port: 3009 }\n');
  const changed = detectorSourceHash(root);
  fs.rmSync(root, { recursive: true, force: true });
  assert.equal(first, same);
  assert.equal(detectorNeedsRebuild(first, same), false);
  assert.notEqual(first, changed);
  assert.equal(detectorNeedsRebuild(first, changed), true);
});

test('remote runner retains exact exit code and stderr without a parent pipe buffer', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-runner-test-'));
  const runner = path.join(root, 'runner.sh');
  fs.writeFileSync(runner, '#!/bin/sh\nprintf "bounded-output"\nprintf "diagnostic-stderr" >&2\nexit 17\n');
  fs.chmodSync(runner, 0o700);
  const previous = process.env.GPOINT_REMOTE_RUNNER;
  process.env.GPOINT_REMOTE_RUNNER = runner;
  let captured;
  assert.throws(() => runRemoteCommand('safe-test-command'), error => {
    captured = error;
    return error.exitCode === 17 && error.stderr.includes('diagnostic-stderr') && /^[0-9a-f]{64}$/.test(error.commandHash);
  });
  assert.equal(failureEvidence(captured, { stage: 'MIGRATION_APPLY' }).failureStage, 'MIGRATION_APPLY');
  assert.equal(failureEvidence(captured, { stage: 'MIGRATION_APPLY' }).failureExitCode, 17);
  if (previous === undefined) delete process.env.GPOINT_REMOTE_RUNNER; else process.env.GPOINT_REMOTE_RUNNER = previous;
  fs.rmSync(root, { recursive: true, force: true });
});

test('remote migration failure evidence preserves output and exit while redacting secrets and command payloads', () => {
  const error = Object.assign(new Error('remote command failed (exit=1, command_sha256=abc)'), {
    exitCode: 1,
    command: 'sha256:abc',
    commandHash: 'abc',
    stdout: 'migration output',
    stderr: 'permission denied; AGATA_INTERNAL_API_TOKEN=secret-value DATABASE_URL=postgresql://gpoint:db-secret@db/gpoint',
  });
  const evidence = failureEvidence(error, { stage: 'MIGRATION_APPLY' });
  assert.equal(evidence.failureStage, 'MIGRATION_APPLY');
  assert.equal(evidence.failureExitCode, 1);
  assert.equal(evidence.failureCommand, 'MIGRATION_APPLY');
  assert.equal(evidence.failureStdout, 'migration output');
  assert.match(evidence.failureStderr, /permission denied/);
  assert.doesNotMatch(JSON.stringify(evidence), /secret-value|db-secret/);
});

test('apply checkout uses the exact local commit in a clean isolated worktree', () => {
  const candidate = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  const checkout = exactCheckout(candidate);
  try {
    assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: checkout, encoding: 'utf8' }).trim(), candidate);
    assert.equal(execFileSync('git', ['status', '--porcelain'], { cwd: checkout, encoding: 'utf8' }).trim(), '');
  } finally {
    execFileSync('git', ['worktree', 'remove', '--force', checkout], { stdio: 'ignore' });
  }
});

test('source upload retries a transient SCP failure and preserves the exact paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-upload-test-'));
  const runner = path.join(root, 'runner.sh');
  const count = path.join(root, 'count');
  const local = path.join(root, 'source');
  fs.writeFileSync(local, 'exact-source');
  fs.writeFileSync(runner, '#!/bin/sh\ncount=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)\ncount=$((count + 1))\nprintf "%s" "$count" > "$COUNT_FILE"\nif [ "$count" -lt 2 ]; then echo "temporary connection failure" >&2; exit 255; fi\ntest "$LOCAL_FILE" = "$EXPECTED_LOCAL" && test "$REMOTE_FILE" = "$EXPECTED_REMOTE"\n');
  fs.chmodSync(runner, 0o700);
  const previous = { runner: process.env.GPOINT_SCP_RUNNER, count: process.env.COUNT_FILE, local: process.env.EXPECTED_LOCAL, remote: process.env.EXPECTED_REMOTE };
  Object.assign(process.env, { GPOINT_SCP_RUNNER: runner, COUNT_FILE: count, EXPECTED_LOCAL: local, EXPECTED_REMOTE: '/tmp/exact-source' });
  try {
    upload(local, '/tmp/exact-source');
    assert.equal(fs.readFileSync(count, 'utf8'), '2');
  } finally {
    for (const [key, value] of Object.entries({ GPOINT_SCP_RUNNER: previous.runner, COUNT_FILE: previous.count, EXPECTED_LOCAL: previous.local, EXPECTED_REMOTE: previous.remote })) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
});
