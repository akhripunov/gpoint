#!/usr/bin/env python3
"""Build and rehearse Linux artifacts; only explicitly disposable Docker DBs."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

# This module is also loaded via importlib.util by the existing migration
# inventory tests. Resolve our sibling module by its canonical path rather
# than depending on scripts/ being present in sys.path.
import importlib.util
_impact_spec = importlib.util.spec_from_file_location(
    'gpoint_release_impact_v3', Path(__file__).resolve().with_name('release_impact_v3.py'))
if _impact_spec is None or _impact_spec.loader is None:
    raise RuntimeError('V3 impact selector module is unavailable')
_impact_module = importlib.util.module_from_spec(_impact_spec)
_impact_spec.loader.exec_module(_impact_module)
load_verified_plan = _impact_module.load_verified_plan

ROOT = Path(__file__).resolve().parents[1]
EROS_UPSTREAM_SHA = 'd1a140083321687212fb7b17c09cc0c3c9639f5e'
EROS_UPSTREAM_URL = 'https://github.com/etherfunlab/eros-engine'


@contextmanager
def measured_stage(name, timings):
    """Emit machine-readable stage durations, including failed stages."""
    started = time.monotonic()
    print(f'PREPARE_STAGE_START={name}', flush=True)
    status = 'PASS'
    try:
        yield
    except BaseException:
        status = 'FAIL'
        raise
    finally:
        seconds = round(time.monotonic() - started, 3)
        timings[name] = {'status': status, 'seconds': seconds}
        print(f'PREPARE_STAGE={name} STATUS={status} SECONDS={seconds:.3f}', flush=True)


def run(*args, cwd=ROOT, env=None, capture=False):
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=1800)
    return result.stdout.strip() if capture else None


def start_background_stage(name, args, timings, *, cwd=ROOT, env=None):
    """Run an independent fail-closed gate alongside foreground preparation."""
    output = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
    stage = {'name': name, 'args': args, 'timings': timings,
             'started': time.monotonic(), 'output': output, 'finished': False}
    print(f'PREPARE_STAGE_START={name}', flush=True)
    try:
        stage['process'] = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
    except BaseException:
        stage['returncode'] = 127
        _finish_background_stage(stage, 'FAIL')
        raise
    return stage


def _finish_background_stage(stage, status):
    if stage['finished']:
        return
    output = stage['output']
    output.flush()
    output.seek(0)
    shutil.copyfileobj(output, sys.stdout)
    sys.stdout.flush()
    seconds = round(time.monotonic() - stage['started'], 3)
    stage['timings'][stage['name']] = {'status': status, 'seconds': seconds}
    stage['finished'] = True
    output.close()
    print(f"PREPARE_STAGE={stage['name']} STATUS={status} SECONDS={seconds:.3f}", flush=True)


def finish_background_stage(stage):
    """Wait for an independent gate and preserve its output and failure."""
    process = stage['process']
    try:
        returncode = process.wait(timeout=1800)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        _finish_background_stage(stage, 'FAIL')
        raise TimeoutError(f"background preparation stage timed out: {stage['name']}") from None
    status = 'PASS' if returncode == 0 else 'FAIL'
    _finish_background_stage(stage, status)
    if returncode:
        raise subprocess.CalledProcessError(returncode, stage['args'])


def abort_background_stage(stage):
    """Stop and reap an independent gate if another mandatory gate fails."""
    if stage['finished']:
        return
    process = stage.get('process')
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    _finish_background_stage(stage, 'FAIL')


def docker_build_command(tag, dockerfile, context, sha, *, dependency_manifest_context=None):
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError('Docker candidate SHA must be exact')
    cache_scope = f'gpoint-{tag}'
    command = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load']
    if tag == 'gpoint-runtime':
        if not dependency_manifest_context:
            raise ValueError('GPoint build requires dependency manifest context')
        command.extend(['--build-context',
                        f'dependency-manifests={dependency_manifest_context}'])
    elif dependency_manifest_context:
        raise ValueError('dependency manifest context is only valid for GPoint runtime')
    command.extend(['--build-arg', f'GPOINT_BUILD_SHA={sha}',
                    '--label', f'org.opencontainers.image.revision={sha}',
                    '--label', 'org.opencontainers.image.source=https://github.com/akhripunov/gpoint',
                    '--cache-from', f'type=gha,scope={cache_scope}',
                    '--cache-to', f'type=gha,scope={cache_scope},mode=max,ignore-error=true',
                    '-f', dockerfile, '-t', f'{tag}:{sha}', context])
    return command


def stock_eros_build_command(source_dir, candidate_sha):
    """Build the exact stock upstream tree as a candidate-bound pilot image."""
    if not re.fullmatch(r'[a-f0-9]{40}', candidate_sha):
        raise ValueError('stock Eros build requires an exact GPoint candidate SHA')
    source_dir = Path(source_dir).resolve()
    command = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load']
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        command.extend(['--cache-from', 'type=gha,scope=gpoint-eros-stock',
                        '--cache-to', 'type=gha,scope=gpoint-eros-stock,mode=max,ignore-error=true'])
    command.extend([
        '--label', f'org.opencontainers.image.revision={EROS_UPSTREAM_SHA}',
        '--label', f'org.opencontainers.image.source={EROS_UPSTREAM_URL}',
        '--label', f'com.gpoint.integration.revision={candidate_sha}',
        '--label', f'com.gpoint.eros.upstream-revision={EROS_UPSTREAM_SHA}',
        '--label', 'com.gpoint.eros.patch-set.sha256=none',
        '--file', str(source_dir / 'docker/Dockerfile'),
        '--tag', f'gpoint-eros-pilot:{candidate_sha}',
        str(source_dir),
    ])
    return command


def build_stock_eros_image(candidate_sha, timings):
    """Fetch, verify, build and smoke-check the pinned upstream without editing it."""
    with tempfile.TemporaryDirectory(prefix='gpoint-stock-eros-') as temporary:
        source_dir = Path(temporary) / 'source'
        with measured_stage('eros_pilot_source_checkout', timings):
            run('git', 'clone', '--quiet', '--no-checkout', EROS_UPSTREAM_URL, str(source_dir))
            run('git', 'checkout', '--quiet', '--detach', EROS_UPSTREAM_SHA, cwd=source_dir)
            if run('git', 'rev-parse', 'HEAD', cwd=source_dir, capture=True) != EROS_UPSTREAM_SHA:
                raise RuntimeError('stock Eros upstream revision mismatch')
            if run('git', 'status', '--porcelain', cwd=source_dir, capture=True):
                raise RuntimeError('stock Eros source checkout is dirty')
        with measured_stage('docker_build_eros_pilot_stock', timings):
            run(*stock_eros_build_command(source_dir, candidate_sha))
        tag = f'gpoint-eros-pilot:{candidate_sha}'
        with measured_stage('eros_pilot_image_smoke', timings):
            openapi = run('docker', 'run', '--rm', '--network', 'none', '--entrypoint',
                          '/usr/local/bin/eros-engine', tag, 'print-openapi', capture=True)
            if not openapi.startswith('{') or 'openapi' not in openapi[:256].lower():
                raise RuntimeError('stock Eros offline OpenAPI smoke failed')
        image = json.loads(run('docker', 'image', 'inspect', tag, capture=True))[0]
        labels = image.get('Config', {}).get('Labels') or {}
        if (image.get('Os') != 'linux' or image.get('Architecture') != 'amd64'
                or labels.get('org.opencontainers.image.revision') != EROS_UPSTREAM_SHA
                or labels.get('org.opencontainers.image.source') != EROS_UPSTREAM_URL
                or labels.get('com.gpoint.integration.revision') != candidate_sha
                or labels.get('com.gpoint.eros.upstream-revision') != EROS_UPSTREAM_SHA
                or labels.get('com.gpoint.eros.patch-set.sha256') != 'none'):
            raise RuntimeError('stock Eros pilot image identity verification failed')
        return {'id': image['Id'], 'sourceSha': candidate_sha,
                'upstreamSha': EROS_UPSTREAM_SHA, 'patchSetSha256': 'none',
                'platform': 'linux/amd64'}


def disposable_migration_tenant_inventory(postgres, database):
    """Read every tenant from the isolated rehearsal DB, failing closed if absent."""
    if not re.fullmatch(r'[a-z0-9_]+', database):
        raise RuntimeError('invalid disposable AGATA database name')
    output = run('docker', 'exec', postgres, 'psql', '-X', '-U', 'postgres', '-d', database,
                 '-v', 'ON_ERROR_STOP=1', '-At', '-c',
                 'SELECT id::text FROM agata.tenants ORDER BY id;', capture=True)
    tenants = []
    try:
        tenants = sorted({str(uuid.UUID(line.strip())) for line in output.splitlines() if line.strip()})
    except ValueError as exc:
        raise RuntimeError('complete disposable AGATA tenant inventory contains an invalid UUID') from exc
    if not tenants:
        raise RuntimeError('complete disposable AGATA tenant inventory is empty')
    return ','.join(tenants)


def prepare(sha, output, *, postgres_only=False):
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or run('git', 'rev-parse', 'HEAD', capture=True) != sha:
        raise RuntimeError('candidate must match the exact checkout')
    if run('git', 'status', '--porcelain', capture=True):
        raise RuntimeError('candidate checkout is dirty')
    output = Path(output).resolve()
    if output.is_relative_to(ROOT):
        raise RuntimeError('bundle must be stored outside checkout')
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / 'receipt.json'
    receipt.unlink(missing_ok=True)  # Never retain READY from a failed retry.
    timings = {}
    # A missing plan is FULL; malformed or stale evidence blocks rather than skipping.
    impact = load_verified_plan(os.environ.get('GPOINT_V3_IMPACT_PLAN_PATH'), sha, ROOT)
    if postgres_only:
        impact = load_verified_plan(None, sha, ROOT)
    print('V3_IMPACT_USED=' + impact['classification'], flush=True)
    if not postgres_only and impact['classification'] == 'UI_ONLY':
        # Fail closed: independent V4 git inventory must prove only regular
        # Mini App source files changed against immutable COMPLETE production.
        # No three-image Docker bundle, database startup or image transfer.
        from release_static_prepare_v4 import prepare_static
        return prepare_static(sha, output, impact)
    gsales_regression = None
    if not postgres_only:
        commands = (
            ('pnpm_install', ('pnpm', 'install', '--frozen-lockfile')),
            ('prisma_generate', ('pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'generate')),
            ('workspace_build', ('pnpm', 'build')),
            ('workspace_typecheck', ('pnpm', 'typecheck')),
            ('workspace_tests', ('pnpm', 'test')),
            ('release_tests', ('pnpm', 'test:release')),
            ('eros_pilot_compose_tests', (sys.executable, '-m', 'unittest', 'tests.test_eros_vps_pilot_compose')),
            ('eros_pilot_model_verifier_tests', ('node', 'services/eros-engine/verify-eros-pilot-model.test.mjs')),
            ('eros_pilot_embedding_relay_tests', ('node', 'services/eros-engine/eros-pilot-embedding-relay.test.mjs')),
            ('linear_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*linear*.py')),
            ('production_migration_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_agata_production_migration.py')),
            ('gsales_provision_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_gsales_provision.py')),
            ('gsales_mac_lane_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_configure_gsales_mac_lane.py')),
            ('prepare_inventory_tests', (sys.executable, '-m', 'pytest', '-q', 'tests/test_prepare_release_migration_inventory.py')),
            ('ghcr_receipt_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_publish_ghcr_receipt.py')),
            ('artifact_cache_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_prepared_artifact_cache.py')),
            ('gsales_compose_profile_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_release_gsales_compose.py')),
        )
        for name, command in commands:
            with measured_stage(name, timings):
                run(*command)
        # Start after CPU-heavy workspace tests so this database gate can
        # overlap the later disposable AGATA PostgreSQL rehearsal without
        # competing with the candidate Docker image builds.
        if impact['checks']['gsales_postgres_regression']:
            gsales_regression = start_background_stage(
                'gsales_postgres_regression',
                (sys.executable, 'services/gsales/tests/run_postgres.py'), timings)
        else:
            print('V3_COVERED_UNCHANGED=gsales_postgres_regression', flush=True)

    suffix = uuid.uuid4().hex[:12]
    network, postgres, api = (f'gpoint-ci-{suffix}-{x}' for x in ('net', 'pg', 'agata'))
    env = dict(os.environ)
    key = base64.b64encode(b'a' * 32).decode()
    env.update(AGATA_TENANT_ID='7508f36f-774a-48aa-8c6b-42f6f160415d',
               AGATA_INTERNAL_API_TOKEN='disposable-test-token', AGATA_LLM_PROVIDER='disabled')
    # Never inherit an operator's or CI runner's inventory into a disposable rehearsal.
    env.pop('AGATA_MIGRATION_TENANT_IDS', None)
    env['AGATA_ROLE_TENANT_BINDINGS'] = json.dumps({'ci_owner': env['AGATA_TENANT_ID']})
    for name in ('MESSAGE_AEAD_KEY', 'REQUEST_FINGERPRINT_HMAC_KEY', 'RETRIEVAL_QUERY_HMAC_KEY',
                 'RESPONSE_FINGERPRINT_HMAC_KEY', 'SHADOW_CANDIDATE_HMAC_KEY'):
        env[f'AGATA_{name}'] = key
    try:
        with measured_stage('disposable_postgres_setup', timings):
            run('docker', 'network', 'create', network)
            run('docker', 'run', '-d', '--name', postgres, '--network', network,
                '-p', '127.0.0.1::5432', '-e', 'POSTGRES_PASSWORD=disposable-ci-password', 'pgvector/pgvector:0.8.6-pg16')
            # The image entrypoint starts a temporary Unix-socket-only server
            # during initdb, then stops it. Wait for the final TCP server and an
            # authenticated query before creating fixtures.
            for _ in range(60):
                if subprocess.run(['docker', 'exec', '-e', 'PGPASSWORD=disposable-ci-password',
                                   postgres, 'psql', '-h', '127.0.0.1', '-U', 'postgres',
                                   '-w', '-v', 'ON_ERROR_STOP=1', '-At', '-c', 'SELECT 1'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('disposable Postgres did not become ready')
            run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c',
                "CREATE ROLE ci_owner LOGIN PASSWORD 'disposable-ci-password' NOSUPERUSER NOBYPASSRLS;")
            for db in ('gpoint_agata_test', 'rls_agata_test', 'runtime_agata_test'):
                run('docker', 'exec', postgres, 'createdb', '-U', 'postgres', '-O', 'ci_owner', db)
                run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', db, '-v', 'ON_ERROR_STOP=1', '-c',
                    'CREATE EXTENSION vector; CREATE EXTENSION pgcrypto;')
        port = run('docker', 'port', postgres, '5432/tcp', capture=True).rsplit(':', 1)[1]
        dsn = f'postgresql+psycopg://ci_owner:disposable-ci-password@127.0.0.1:{port}/gpoint_agata_test'
        env.update(AGATA_TEST_DATABASE_URL=dsn, AGATA_DATABASE_URL=dsn.replace('+psycopg', '+asyncpg'))
        env.pop('AGATA_MIGRATOR_DATABASE_URL', None)
        agata = ROOT / 'services/agata'
        # This test intentionally starts with an empty AGATA schema, then leaves
        # an upgraded representative legacy schema for the remaining tests.
        # Legacy backfill uses the migration administrator. The application
        # suite below uses a different role which cannot bypass tenant RLS.
        migration_env = dict(env)
        migration_env['AGATA_TEST_DATABASE_URL'] = dsn.replace('ci_owner:', 'postgres:')
        migration_env['AGATA_DATABASE_URL'] = migration_env['AGATA_TEST_DATABASE_URL'].replace('+psycopg', '+asyncpg')
        # The baseline schema and real non-bypass HTTP dialogue are application
        # smoke gates, not optional source-regression suites. Always run them.
        with measured_stage('agata_baseline_migration_tests', timings):
            run(sys.executable, '-m', 'pytest', '-q', 'tests/test_postgres_baseline.py', cwd=agata, env=migration_env)
        run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint_agata_test', '-v', 'ON_ERROR_STOP=1', '-c',
            'GRANT USAGE ON SCHEMA agata TO ci_owner; GRANT ALL ON ALL TABLES IN SCHEMA agata TO ci_owner; GRANT ALL ON ALL SEQUENCES IN SCHEMA agata TO ci_owner;')
        migration_env['AGATA_RLS_TEST_DATABASE_URL'] = dsn.replace('/gpoint_agata_test', '/rls_agata_test')
        if impact['checks']['agata_postgres_rehearsal']:
            with measured_stage('agata_postgres_integration_tests', timings):
                run(sys.executable, '-m', 'pytest', '-q', '--ignore=tests/test_postgres_baseline.py', cwd=agata, env=migration_env)
        else:
            print('V3_COVERED_UNCHANGED=agata_postgres_source_regression', flush=True)
        # Keep the real HTTP dialogue under the application's non-bypass role.
        with measured_stage('agata_api_e2e_tests', timings):
            run(sys.executable, '-m', 'pytest', '-q', 'tests/test_internal_api_e2e.py', cwd=agata, env=env)
        if postgres_only:
            print('POSTGRES_REHEARSAL=PASS; no READY receipt or production images emitted')
            return
        with tempfile.TemporaryDirectory(prefix='gpoint-docker-dependency-manifests-') as dependency_context:
            run(sys.executable, 'scripts/docker_dependency_manifests.py', dependency_context)
            for tag, dockerfile, context in (
                    ('gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.'),
                    ('gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata'),
                    ('gpoint-g-sales-runtime', 'services/gsales/Dockerfile', 'services/gsales'),
            ):
                with measured_stage(f'docker_build_{tag.replace("-", "_")}', timings):
                    command = docker_build_command(
                        tag, dockerfile, context, sha,
                        dependency_manifest_context=(dependency_context
                                                     if tag == 'gpoint-runtime' else None))
                    run(*command)
        with measured_stage('bot_production_image_startup', timings):
            run(sys.executable, 'scripts/smoke_bot_image.py', f'gpoint-runtime:{sha}')
        gsales_migration = ROOT / 'services/gsales/migrations/001_gsales.sql'
        gsales_migration_sha = hashlib.sha256(gsales_migration.read_bytes()).hexdigest()
        embedded_migration_sha = run('docker', 'run', '--rm', f'gpoint-g-sales-runtime:{sha}',
            'python', '-c', "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('/srv/g-sales/migrations/001_gsales.sql').read_bytes()).hexdigest())", capture=True)
        if embedded_migration_sha != gsales_migration_sha:
            raise RuntimeError('G Sales image migration checksum differs from exact candidate source')
        with measured_stage('gsales_migration_backup_restore', timings):
            migration_env = dict(os.environ, GSALES_MIGRATION_TEST_IMAGE=f'gpoint-g-sales-runtime:{sha}')
            run(sys.executable, 'tests/test_gsales_migration_docker.py', env=migration_env)
        with measured_stage('gsales_image_live_health', timings):
            smoke = f'gpoint-gsales-health-{uuid.uuid4().hex[:12]}'
            run('docker', 'run', '-d', '--name', smoke, '--network', 'none',
                '-e', 'GSALES_PRODUCTION_MODE=false', '-e', 'GSALES_OUTREACH_ENABLED=false',
                '-e', 'GSALES_AUTO_REPLY_ENABLED=false', '-e', 'GSALES_MANAGEMENT_ENABLED=false',
                '-e', 'GSALES_TELEGRAM_LISTENER_ENABLED=false', f'gpoint-g-sales-runtime:{sha}')
            try:
                for _ in range(45):
                    if subprocess.run(['docker', 'exec', smoke, 'python', '-c',
                            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health/live', timeout=2)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('G Sales immutable image live health failed')
            finally:
                subprocess.run(['docker', 'rm', '-f', smoke], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
        with measured_stage('agata_guarded_production_migration_rehearsal', timings):
            rehearsal_env = dict(env, GPOINT_MIGRATION_TEST_IMAGE=f'gpoint-agata-runtime:{sha}')
            run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_agata_production_migration_docker.py', env=rehearsal_env)
        with measured_stage('gsales_protected_provision_rehearsal', timings):
            provision_env = dict(os.environ, GPOINT_MIGRATION_TEST_IMAGE=f'gpoint-agata-runtime:{sha}')
            run(sys.executable, 'tests/test_gsales_provision_docker.py', env=provision_env)
        docker_dsn = f'postgresql://ci_owner:disposable-ci-password@{postgres}:5432/runtime_agata_test'
        with measured_stage('runtime_artifact_migration_check', timings):
            run('docker', 'run', '--rm', '--network', network, '-e', f'DATABASE_URL={docker_dsn}',
                f'gpoint-runtime:{sha}', 'pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'migrate', 'deploy')
            run('docker', 'run', '--rm', '--network', network, '-e', f'DATABASE_URL={docker_dsn}',
                f'gpoint-runtime:{sha}', 'pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'migrate', 'status')
        container_env = ['-e', f'AGATA_DATABASE_URL={docker_dsn.replace("postgresql:", "postgresql+asyncpg:")}']
        for name in env:
            if name.startswith('AGATA_') and name not in ('AGATA_DATABASE_URL', 'AGATA_TEST_DATABASE_URL', 'AGATA_RLS_TEST_DATABASE_URL'):
                container_env.extend(['-e', name])
        with measured_stage('agata_artifact_migration_and_health', timings):
            # The production candidate image creates the disposable AGATA schema
            # through agata0009. Then read the complete fixture inventory from
            # this exact DB as its isolated administrator before agata0010 runs.
            run('docker', 'run', '--rm', '--network', network, *container_env,
                f'gpoint-agata-runtime:{sha}', 'alembic', 'upgrade', 'agata0009', env=env)
            env['AGATA_MIGRATION_TENANT_IDS'] = disposable_migration_tenant_inventory(
                postgres, 'runtime_agata_test')
            container_env.extend(['-e', 'AGATA_MIGRATION_TENANT_IDS'])
            migration_container_env = list(container_env)
            migration_container_env[1] = f'AGATA_DATABASE_URL={docker_dsn.replace("ci_owner:", "postgres:").replace("postgresql:", "postgresql+asyncpg:")}'
            for _ in range(2):  # Actual artifact migrations must also be idempotent.
                run('docker', 'run', '--rm', '--network', network, *migration_container_env,
                    f'gpoint-agata-runtime:{sha}', 'alembic', 'upgrade', 'head', env=env)
            run('docker', 'run', '-d', '--name', api, '--network', network, *container_env,
                f'gpoint-agata-runtime:{sha}', env=env)
            for _ in range(60):
                health = subprocess.run(['docker', 'exec', api, 'python', '-c',
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if health.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('candidate AGATA container readiness failed')
        index = run('docker', 'run', '--rm', f'gpoint-runtime:{sha}', 'cat', '/app/apps/miniapp/dist/index.html', capture=True)
        if f'gpoint-build-sha" content="{sha}' not in index:
            raise RuntimeError('Mini App candidate SHA mismatch')
        static_artifact_sha256 = None
        if impact['classification'] == 'UI_ONLY':
            # Advisory-only asset for future V4 fast route. The canonical V3
            # receipt, three images, PostgreSQL gates and Deploy are unchanged.
            from release_ui_artifact_v4 import create_bundle
            with measured_stage('v4_immutable_static_artifact', timings):
                static_path = output / 'miniapp.zip'
                create_bundle(ROOT / 'apps/miniapp/dist', sha, static_path)
                with static_path.open('rb') as archive_file:
                    static_artifact_sha256 = hashlib.file_digest(archive_file, 'sha256').hexdigest()
        heads = run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'runtime_agata_test', '-At', '-c',
                    'SELECT version_num FROM agata.alembic_version;', capture=True)
        prisma = run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'runtime_agata_test', '-At', '-c',
                    'SELECT migration_name FROM public._prisma_migrations WHERE finished_at IS NOT NULL ORDER BY migration_name DESC LIMIT 1;', capture=True)
        expected_prisma = sorted(p.name for p in (ROOT / 'packages/db/prisma/migrations').iterdir() if (p / 'migration.sql').is_file())[-1]
        expected_agata = run('docker', 'run', '--rm', f'gpoint-agata-runtime:{sha}', 'alembic', 'heads', capture=True)
        if '\n' in expected_agata or not expected_agata or heads != expected_agata.split()[0] or prisma != expected_prisma:
            raise RuntimeError('runtime artifact migration heads do not match candidate source')
        pilot_image = build_stock_eros_image(sha, timings)
        if gsales_regression is not None:
            finish_background_stage(gsales_regression)
        images = {}
        for role, tag in [('app', 'gpoint-runtime'), ('agata', 'gpoint-agata-runtime'), ('gsales', 'gpoint-g-sales-runtime')]:
            image = json.loads(run('docker', 'image', 'inspect', f'{tag}:{sha}', capture=True))[0]
            if image['Config']['Labels'].get('org.opencontainers.image.revision') != sha or image['Architecture'] != 'amd64' or image['Os'] != 'linux':
                raise RuntimeError('candidate image parity failed')
            images[role] = {'id': image['Id'], 'revision': sha, 'platform': 'linux/amd64'}
        proof = {'schema': 1, 'candidateSha': sha, 'status': 'READY', 'runId': int(os.environ['GITHUB_RUN_ID']),
                 'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
                 'gates': {name: 'PASS' for name in ['unit', 'postgres', 'gsalesPostgres', 'gsalesImage', 'gsalesMigrationChecksum', 'docker', 'migration', 'miniapp', 'agataHealth', 'botImageStartup', 'erosPilotImage']},
                 'impactV3': impact,
                 'images': images, 'prismaHead': prisma, 'agataHead': heads,
                 'gsalesMigrationHead': gsales_migration.stem, 'gsalesMigrationSha256': gsales_migration_sha,
                 'pilotImage': pilot_image,
                 'prepareStages': timings,
                 'applicationSmoke': 'DISPOSABLE_DB_DETERMINISTIC_PROVIDER', 'productionUserSmoke': 'NOT_RUN'}
        if impact['classification'] == 'UI_ONLY':
            proof['gates']['postgres'] = 'COVERED_UNCHANGED'
            proof['gates']['gsalesPostgres'] = 'COVERED_UNCHANGED'
        if static_artifact_sha256 is not None:
            proof['staticArtifactSha256'] = static_artifact_sha256
            proof['staticArtifactName'] = 'miniapp.zip'
            proof['staticArtifactRoute'] = 'V4_ADVISORY_ONLY'
        receipt.write_text(json.dumps(proof, indent=2) + '\n')
    finally:
        if gsales_regression is not None:
            abort_background_stage(gsales_regression)
        for name in (api, postgres):
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    prepare(sys.argv[1], sys.argv[2], postgres_only='--postgres-only' in sys.argv[3:])
