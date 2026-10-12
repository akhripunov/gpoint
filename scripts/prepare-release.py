#!/usr/bin/env python3
"""Build and rehearse Linux artifacts; only explicitly disposable Docker DBs."""
import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
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


def verify_saved_images(archive, images, sha, temp_root):
    """Rehearse production import/capture/parity/reuse in a private containerd store."""
    started = time.monotonic()
    dockerd, docker, ctr, containerd = (shutil.which(name) for name in ('dockerd', 'docker', 'ctr', 'containerd'))
    unshare = shutil.which('unshare')
    if not all((dockerd, docker, ctr, containerd, unshare, shutil.which('sudo'))):
        raise RuntimeError('isolated Docker daemon prerequisites are unavailable')
    root = Path(tempfile.mkdtemp(prefix='gpoint-image-roundtrip-', dir=temp_root))
    socket = root / 'docker.sock'
    pidfile = root / 'dockerd.pid'
    log_path = root / 'dockerd.log'
    host = f'unix://{socket}'
    log = log_path.open('wb')
    daemon = None
    daemon_pid = None
    content_daemon = None
    content_pidfile = root / 'containerd.pid'
    content_socket = root / 'containerd.sock'
    try:
        config = root / 'daemon.json'
        config.write_text(json.dumps({'features': {'containerd-snapshotter': True}}))
        # Own both daemons explicitly; never attach to system containerd.
        content_daemon = subprocess.Popen(
            ['sudo', '-n', 'env', f'PATH={os.environ["PATH"]}', 'sh', '-c',
             'echo $$ > "$1"; shift; exec "$@"', 'gpoint-containerd', str(content_pidfile),
             containerd, '--root', str(root / 'content'), '--state', str(root / 'content-state'),
             '--address', str(content_socket)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(30):
            if content_daemon.poll() is not None:
                break
            probe = subprocess.run(['sudo', '-n', ctr, '--address', str(content_socket), 'version'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('private containerd did not become ready')
        if content_daemon.poll() is not None:
            raise RuntimeError('private containerd exited before rehearsal')
        daemon = subprocess.Popen(
            ['sudo', '-n', 'env', f'PATH={os.environ["PATH"]}', unshare, '--net', dockerd,
             '--config-file', str(config), '--host', host, '--data-root', str(root / 'data'),
             '--exec-root', str(root / 'exec'), '--pidfile', str(pidfile), '--storage-driver=overlayfs',
             '--containerd', str(content_socket), '--containerd-namespace=moby',
             '--bridge=none', '--iptables=false', '--ip-masq=false',
             '--ip-forward=false'],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # Wrappers always address the private daemon, even if setup-docker-action
        # exports a Docker context. ctr must address its private managed containerd.
        wrappers = root / 'bin'
        wrappers.mkdir()
        for name, command in {
                'docker': ['sudo', '-n', docker, '--host', host],
                'ctr': ['sudo', '-n', ctr, '--address', str(content_socket)],
        }.items():
            wrapper = wrappers / name
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
            wrapper.chmod(0o700)
        client_env = dict(os.environ, PATH=f'{wrappers}:{os.environ["PATH"]}', DOCKER_HOST=host)
        for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION'):
            client_env.pop(key, None)
        ready = False
        for _ in range(90):
            if daemon.poll() is not None:
                break
            probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], env=client_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            log.flush()
            detail = log_path.read_text(errors='replace')[-4000:]
            raise RuntimeError(f'isolated Docker daemon did not become ready: {detail}')

        info = json.loads(run('docker', 'info', '--format', '{{json .}}', env=client_env, capture=True))
        if info['ServerVersion'] != '29.1.3' or info['Driver'] != 'overlayfs' or \
                ['driver-type', 'io.containerd.snapshotter.v1'] not in info.get('DriverStatus', []):
            raise RuntimeError('rehearsal must use production Docker 29.1.3/containerd/overlayfs')
        if info.get('Containerd', {}).get('Address') != str(content_socket):
            raise RuntimeError('rehearsal Docker is not attached to its private containerd')
        daemon_pid = int(pidfile.read_text().strip())
        network_inode = int(run('sudo', '-n', 'stat', '-Lc', '%i',
                                f'/proc/{daemon_pid}/ns/net', capture=True))
        if network_inode == os.stat('/proc/self/ns/net').st_ino:
            raise RuntimeError('rehearsal Docker must not share the build daemon network namespace')
        copied = root / f'gpoint-images-{sha}.tar.gz'
        shutil.copyfile(archive, copied)
        with copied.open('rb') as source:
            checksum = hashlib.file_digest(source, 'sha256').hexdigest()
        proof = root / 'proof.json'
        proof.write_text(json.dumps({'candidateSha': sha, 'images': images, 'archiveSha256': checksum}))
        run('node', 'scripts/rehearse-release-bundle.mjs', str(proof), sha, str(root), env=client_env)
        print('PREPARE_IMAGE_STORE=containerd DOCKER_VERSION=29.1.3')
        print('PREPARE_IMAGE_ROUNDTRIP=PASS')
        elapsed = round(time.monotonic() - started, 1)
        print(f'PREPARE_DEPLOY_REHEARSAL_SECONDS={elapsed}')
        return {'dockerVersion': '29.1.3', 'store': 'containerd', 'snapshotter': 'overlayfs',
                'identityRegressionRejected': ['app', 'agata'], 'seconds': elapsed}
    finally:
        if pidfile.exists():
            try:
                daemon_pid = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                pass
        if daemon is not None and daemon.poll() is None:
            if daemon_pid:
                subprocess.run(['sudo', '-n', 'kill', '-TERM', str(daemon_pid)], check=False, timeout=10)
            try:
                daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if daemon_pid:
                    subprocess.run(['sudo', '-n', 'kill', '-KILL', str(daemon_pid)], check=False, timeout=10)
                daemon.kill()
                daemon.wait(timeout=10)
        if content_daemon is not None and content_daemon.poll() is None:
            if content_pidfile.exists():
                content_pid = int(content_pidfile.read_text().strip())
                subprocess.run(['sudo', '-n', 'kill', '-TERM', str(content_pid)], check=False, timeout=10)
            try:
                content_daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if content_pidfile.exists():
                    subprocess.run(['sudo', '-n', 'kill', '-KILL', str(content_pid)], check=False, timeout=10)
                content_daemon.kill()
                content_daemon.wait(timeout=10)
        log.close()
        # dockerd runs as root on hosted runners, so its isolated scratch tree
        # needs narrowly scoped privileged cleanup.
        subprocess.run(['sudo', '-n', 'rm', '-rf', '--', str(root)], check=False, timeout=30)


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
    gsales_regression = None
    if not postgres_only:
        commands = (
            ('pnpm_install', ('pnpm', 'install', '--frozen-lockfile')),
            ('prisma_generate', ('pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'generate')),
            ('workspace_build', ('pnpm', 'build')),
            ('workspace_typecheck', ('pnpm', 'typecheck')),
            ('workspace_tests', ('pnpm', 'test')),
            ('release_tests', ('pnpm', 'test:release')),
            ('linear_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*linear*.py')),
            ('production_migration_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_agata_production_migration.py')),
            ('gsales_provision_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_gsales_provision.py')),
            ('gsales_mac_lane_gate_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_configure_gsales_mac_lane.py')),
            ('prepare_inventory_tests', (sys.executable, '-m', 'pytest', '-q', 'tests/test_prepare_release_migration_inventory.py')),
            ('release_asset_tests', (sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_release_assets.py')),
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
        for tag, dockerfile, context in (
                ('gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.'),
                ('gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata'),
                ('gpoint-g-sales-runtime', 'services/gsales/Dockerfile', 'services/gsales'),
        ):
            with measured_stage(f'docker_build_{tag.replace("-", "_")}', timings):
                cache_scope = f'gpoint-{tag}'
                run('docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load',
                    '--build-arg', f'GPOINT_BUILD_SHA={sha}',
                    '--label', f'org.opencontainers.image.revision={sha}',
                    '--cache-from', f'type=gha,scope={cache_scope}',
                    '--cache-to', f'type=gha,scope={cache_scope},mode=max,ignore-error=true',
                    '-f', dockerfile, '-t', f'{tag}:{sha}', context)
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
        heads = run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'runtime_agata_test', '-At', '-c',
                    'SELECT version_num FROM agata.alembic_version;', capture=True)
        prisma = run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'runtime_agata_test', '-At', '-c',
                    'SELECT migration_name FROM public._prisma_migrations WHERE finished_at IS NOT NULL ORDER BY migration_name DESC LIMIT 1;', capture=True)
        expected_prisma = sorted(p.name for p in (ROOT / 'packages/db/prisma/migrations').iterdir() if (p / 'migration.sql').is_file())[-1]
        expected_agata = run('docker', 'run', '--rm', f'gpoint-agata-runtime:{sha}', 'alembic', 'heads', capture=True)
        if '\n' in expected_agata or not expected_agata or heads != expected_agata.split()[0] or prisma != expected_prisma:
            raise RuntimeError('runtime artifact migration heads do not match candidate source')
        if gsales_regression is not None:
            finish_background_stage(gsales_regression)
        images = {}
        for role, tag in [('app', 'gpoint-runtime'), ('agata', 'gpoint-agata-runtime'), ('gsales', 'gpoint-g-sales-runtime')]:
            image = json.loads(run('docker', 'image', 'inspect', f'{tag}:{sha}', capture=True))[0]
            if image['Config']['Labels'].get('org.opencontainers.image.revision') != sha or image['Architecture'] != 'amd64' or image['Os'] != 'linux':
                raise RuntimeError('candidate image parity failed')
            images[role] = {'id': image['Id'], 'revision': sha, 'platform': 'linux/amd64'}
        archive = output / 'images.tar.gz'
        with measured_stage('image_archive_export', timings):
            with gzip.open(archive, 'wb', compresslevel=1) as dest:
                proc = subprocess.Popen(['docker', 'save', f'gpoint-runtime:{sha}', f'gpoint-agata-runtime:{sha}', f'gpoint-g-sales-runtime:{sha}'], stdout=subprocess.PIPE)
                assert proc.stdout is not None
                while chunk := proc.stdout.read(1024 * 1024):
                    dest.write(chunk)
                if proc.wait(timeout=600):
                    raise RuntimeError('image export failed')
        with measured_stage('deploy_artifact_roundtrip', timings):
            rehearsal = verify_saved_images(archive, images, sha, output.parent)
        with archive.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        proof = {'schema': 1, 'candidateSha': sha, 'status': 'READY', 'runId': int(os.environ['GITHUB_RUN_ID']),
                 'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
                 'gates': {name: 'PASS' for name in ['unit', 'postgres', 'gsalesPostgres', 'gsalesImage', 'gsalesMigrationChecksum', 'docker', 'migration', 'miniapp', 'agataHealth', 'deployRehearsal']},
                 'impactV3': impact,
                 'images': images, 'archiveSha256': digest, 'prismaHead': prisma, 'agataHead': heads,
                 'gsalesMigrationHead': gsales_migration.stem, 'gsalesMigrationSha256': gsales_migration_sha,
                 'prepareStages': timings,
                 'deployRehearsal': rehearsal,
                 'applicationSmoke': 'DISPOSABLE_DB_DETERMINISTIC_PROVIDER', 'productionUserSmoke': 'NOT_RUN'}
        if impact['classification'] == 'UI_ONLY':
            proof['gates']['postgres'] = 'COVERED_UNCHANGED'
            proof['gates']['gsalesPostgres'] = 'COVERED_UNCHANGED'
        receipt.write_text(json.dumps(proof, indent=2) + '\n')
    finally:
        if gsales_regression is not None:
            abort_background_stage(gsales_regression)
        for name in (api, postgres):
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    prepare(sys.argv[1], sys.argv[2], postgres_only='--postgres-only' in sys.argv[3:])
