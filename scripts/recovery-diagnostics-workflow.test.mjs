import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { recoveryProductionInspectionSource } from './release-gpoint-vps-adapter.mjs';
import { spawnSync } from 'node:child_process';

const workflow = fs.readFileSync(new URL('../.github/workflows/gpoint-deploy.yml', import.meta.url), 'utf8');
const releaseEntrypoint = fs.readFileSync(new URL('./automated-release.mjs', import.meta.url), 'utf8');
const canonicalRelease = fs.readFileSync(new URL('./release-gpoint.mjs', import.meta.url), 'utf8');
const start = workflow.indexOf('  recovery-diagnostics:\n');
const end = workflow.indexOf('\n  publish:\n', start);
const diagnosticJob = workflow.slice(start, end);

test('recovery diagnostics workflow is read-only and artifacts are uploaded without ledger publication', () => {
  assert.ok(start >= 0 && end > start);
  assert.match(diagnosticJob, /permissions:\n\s+contents: read\n\s+actions: read\n\s+packages: read/);
  assert.doesNotMatch(diagnosticJob, /contents: write|release_assets\.py publish evidence|--apply|--finalize-recovery/);
  assert.match(diagnosticJob, /actions\/upload-artifact@v4/);
  assert.match(workflow, /if: inputs\.mode != 'recovery-diagnostics'/);
  assert.match(workflow, /if: always\(\).*inputs\.mode != 'recovery-diagnostics'/);
  assert.match(workflow, /snapshot_sha256: \${{ steps\.execute\.outputs\.snapshot_sha256 }}/);
  assert.doesNotMatch(releaseEntrypoint, /GPOINT_RECOVERY_ACTIVE_DEPLOYS = JSON\.stringify\(recoveryActiveDeploys\)/);
  assert.match(releaseEntrypoint, /createRecoveryActiveDeploysFile\(recoveryActiveDeploys\)/);
  assert.match(releaseEntrypoint, /GPOINT_RECOVERY_ACTIVE_DEPLOYS_FILE: inventoryFile\.filePath/);
  assert.match(releaseEntrypoint, /delete childEnv\.GPOINT_RECOVERY_ACTIVE_DEPLOYS/);
  assert.match(canonicalRelease, /activeRuns\.total_count !== runs\.length/);
  assert.match(canonicalRelease, /const runs = activeRuns\.workflow_runs/);
  assert.match(canonicalRelease, /runs\.some\(run => !\/\^\\d\+\$\/\.test/);
  assert.match(canonicalRelease, /GPOINT_RECOVERY_ACTIVE_DEPLOYS_FILE/);
  assert.match(releaseEntrypoint, /snapshot_sha256=\$\{evidence\.recoverySnapshot\?\.snapshotSha256/);
  assert.match(canonicalRelease, /const liveSha = facts\.LIVE_RELEASE_ENV_SHA;[\s\S]*liveSha !== expected\.runtimeSha/);
  assert.match(canonicalRelease, /if \(recoveryReadOnlyDiagnostics\) return recoveryDiagnostics\(candidateSha\(args\)\);/);
});

test('recovery diagnostics selector receives the pinned incident run identity', () => {
  const selectorStep = diagnosticJob
    .split('      - name: Verify original candidate and Prepare provenance\n', 2)[1]
    ?.split('      - name:', 1)[0];
  assert.ok(selectorStep, 'recovery diagnostics must retain the provenance selector step');
  assert.match(selectorStep, /RECOVERY_FAILURE_RUN_ID: \$\{\{ inputs\.recovery_failure_run_id \}\}/);
  assert.match(selectorStep, /RECOVERY_FAILURE_RUN_ATTEMPT: \$\{\{ inputs\.recovery_failure_run_attempt \}\}/);
  assert.match(selectorStep, /run: python3 scripts\/select-release-run\.py/);
});

test('recovery VPS probe is valid Python and contains only inspection/read-only DB operations', () => {
  const source = recoveryProductionInspectionSource('d367c2ec24242a733dbb2c4145c91d2d4682ba2c');
  const compiled = spawnSync('python3', ['-c', 'import sys; compile(sys.stdin.read(), "recovery_probe.py", "exec")'],
    { input: source, encoding: 'utf8' });
  assert.equal(compiled.status, 0, compiled.stderr);
  assert.match(source, /BEGIN READ ONLY/);
  assert.match(source, /facts\['databaseReadOnlyQueryExitCode'\]=db\.returncode/);
  assert.match(source, /'docker','inspect'/);
  assert.doesNotMatch(source, /docker (?:compose up|restart|rm|stop|pull)|prisma migrate|alembic upgrade|pg_restore .*--clean|DROP\s+TABLE/i);
  assert.throws(() => recoveryProductionInspectionSource('bad'), /Exact recovery candidate SHA/);
});
