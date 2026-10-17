import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { recoveryProductionInspectionSource } from './release-gpoint-vps-adapter.mjs';
import { spawnSync } from 'node:child_process';

const workflow = fs.readFileSync(new URL('../.github/workflows/gpoint-deploy.yml', import.meta.url), 'utf8');
const releaseEntrypoint = fs.readFileSync(new URL('./automated-release.mjs', import.meta.url), 'utf8');
const canonicalRelease = fs.readFileSync(new URL('./release-gpoint.mjs', import.meta.url), 'utf8');
const start = workflow.indexOf('  recovery-diagnostics:\n');
const end = workflow.indexOf('\n  publish-recovery-diagnostics:\n', start);
const diagnosticJob = workflow.slice(start, end);

test('recovery diagnostics remains read-only and publishes through isolated immutable Release job', () => {
  assert.ok(start >= 0 && end > start);
  assert.match(diagnosticJob, /permissions:\n\s+contents: read\n\s+actions: read\n\s+packages: read/);
  assert.doesNotMatch(diagnosticJob, /contents: write|release_assets\.py publish evidence|--apply|--finalize-recovery/);
  assert.doesNotMatch(diagnosticJob, /actions\/(?:upload|download)-artifact/);
  assert.match(diagnosticJob, /permissions:\n\s+contents: read\n\s+actions: read\n\s+packages: read/);
  assert.match(diagnosticJob, /diagnostic_bundle: \$\{\{ steps\.package\.outputs\.recovery_diagnostic_bundle \}\}/);
  const publisher = workflow.split('  publish-recovery-diagnostics:\n', 2)[1]?.split('\n  publish:\n', 1)[0];
  assert.ok(publisher, 'separate diagnostic release publisher is required');
  assert.match(publisher, /permissions:\n\s+contents: write/);
  assert.match(publisher, /needs: \[validate, recovery-diagnostics\]/);
  assert.match(publisher, /release_assets\.py publish-recovery-diagnostics/);
  assert.doesNotMatch(publisher, /GPOINT_RELEASE_BUNDLE|GHCR_READ_TOKEN|GITHUB_ENV.*(?:VPS|DB)/);
  assert.match(workflow, /RECOVERY_DIAGNOSTICS_BUNDLE_B64: \$\{\{ needs\.recovery-diagnostics\.outputs\.diagnostic_bundle \}\}/);
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
  assert.match(canonicalRelease, /recoveryFinalizationIdentityFromFailureEvidence\(incident\.manifest, incident\.result, candidate\)/);
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
  assert.match(source, /com\.docker\.compose\.project=shared/);
  assert.match(source, /com\.docker\.compose\.service=postgres/);
  assert.match(source, /shared_postgres_1/);
  assert.doesNotMatch(source, /compose-postgres-1/);
  assert.match(source, /facts\['postgresContainerIdentity'\]/);
  assert.match(source, /postgres_container_id/);
  assert.match(source, /'docker','inspect'/);
  assert.doesNotMatch(source, /docker (?:compose up|restart|rm|stop|pull)|prisma migrate|alembic upgrade|pg_restore .*--clean|DROP\s+TABLE/i);
  assert.throws(() => recoveryProductionInspectionSource('bad'), /Exact recovery candidate SHA/);
});

test('recovery probe uses the uniquely verified shared_postgres_1 for read-only SQL and fails closed on ambiguity', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-recovery-postgres-test-'));
  const docker = path.join(directory, 'docker');
  const callsFile = path.join(directory, 'docker-calls.jsonl');
  const fakeDocker = `#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ['FAKE_DOCKER_CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[:2]==['ps','-aq']:
 ids=os.environ.get('FAKE_POSTGRES_IDS','${'a'.repeat(64)}').split(',') if '--filter' in args else []
 print('\\n'.join(ids))
elif args[:2]==['inspect','${'a'.repeat(64)}']:
 print(json.dumps([{'Name':'/shared_postgres_1','Config':{'Labels':{'com.docker.compose.project':'shared','com.docker.compose.service':'postgres'}},'State':{'Running':True}}]))
elif 'pg_restore' in args:
 data=sys.stdin.buffer.read()
 if not data: sys.exit(3)
 print('TABLE DATA agata messages')
elif 'psql' in args:
 print('LIVE_PRISMA_HEAD=prisma-test\\nNOTICE: LIVE_AGATA_HEAD=agata-test\\nNOTICE: LIVE_GSALES_IDENTITY=gsales-test|${'b'.repeat(64)}')
sys.exit(0)
`;
  const backupFile = path.join(directory, 'existing.dump');
  fs.writeFileSync(backupFile, 'synthetic recovery dump');
  fs.chmodSync(backupFile, 0o600);
  fs.writeFileSync(docker, fakeDocker, {mode: 0o700});
  try {
    const source = recoveryProductionInspectionSource('d367c2ec24242a733dbb2c4145c91d2d4682ba2c');
    const runProbe = (postgresIds, includeBackup = false) => {
      fs.writeFileSync(callsFile, '');
      const wrappedSource = `import glob, os\nglob.glob=lambda _pattern: ([os.environ['FAKE_BACKUP_PATH']] if os.environ.get('FAKE_BACKUP_PATH') else [])\nexec(compile(${JSON.stringify(source)}, 'recovery_probe.py', 'exec'))`;
      const result = spawnSync('python3', ['-c', wrappedSource], {encoding: 'utf8', env: {
        ...process.env, PATH: `${directory}:${process.env.PATH}`, FAKE_DOCKER_CALLS: callsFile,
        ...(postgresIds ? {FAKE_POSTGRES_IDS: postgresIds} : {FAKE_POSTGRES_IDS: 'a'.repeat(64)}),
        ...(includeBackup ? {FAKE_BACKUP_PATH: backupFile} : {}),
      }});
      assert.equal(result.status, 0, result.stderr);
      const line = result.stdout.split(/\r?\n/).find(value => value.startsWith('RECOVERY_RUNTIME_FACTS='));
      assert.ok(line, result.stdout);
      return {facts: JSON.parse(line.slice('RECOVERY_RUNTIME_FACTS='.length)), calls: fs.readFileSync(callsFile, 'utf8').trim().split(/\r?\n/).filter(Boolean).map(JSON.parse)};
    };
    const good = runProbe('a'.repeat(64), true);
    assert.deepEqual(good.facts.postgresContainerIdentity, {name: '/shared_postgres_1', project: 'shared', service: 'postgres', running: true, containerId: 'a'.repeat(64)});
    assert.equal(good.facts.databaseReadOnlyQueryExitCode, 0);
    const psql = good.calls.find(args => args.includes('psql'));
    assert.ok(psql);
    assert.ok(psql.includes('a'.repeat(64)));
    assert.deepEqual(good.facts.backup, {status: 'VERIFIED', path: backupFile,
      sha256: good.facts.backup.sha256, mode: '600', readback: 'PASS'});
    assert.ok(good.calls.some(args => args.includes('pg_restore') && args.includes('a'.repeat(64))));
    const ambiguous = runProbe(`${'a'.repeat(64)},${'c'.repeat(64)}`);
    assert.equal(ambiguous.facts.postgresContainerIdentityError, 'POSTGRES_CONTAINER_IDENTITY_INVALID');
    assert.equal(ambiguous.facts.databaseReadOnlyQueryExitCode, 1);
    assert.equal(ambiguous.facts.prismaHead, null);
    assert.equal(ambiguous.calls.some(args => args.includes('psql')), false);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
