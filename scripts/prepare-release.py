#!/usr/bin/env python3
"""Build and rehearse Linux artifacts; only explicitly disposable Docker DBs."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT, env=None, capture=False):
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=1800)
    return result.stdout.strip() if capture else None


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
    if not postgres_only:
        run('pnpm', 'install', '--frozen-lockfile')
        run('pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'generate')
        run('pnpm', 'build')
        run('pnpm', 'typecheck')
        run('pnpm', 'test')
        run('pnpm', 'test:release')
        run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*linear*.py')

    suffix = uuid.uuid4().hex[:12]
    network, postgres, api = (f'gpoint-ci-{suffix}-{x}' for x in ('net', 'pg', 'agata'))
    env = dict(os.environ)
    key = base64.b64encode(b'a' * 32).decode()
    env.update(AGATA_TENANT_ID='7508f36f-774a-48aa-8c6b-42f6f160415d',
               AGATA_INTERNAL_API_TOKEN='disposable-test-token', AGATA_LLM_PROVIDER='disabled')
    for name in ('MESSAGE_AEAD_KEY', 'REQUEST_FINGERPRINT_HMAC_KEY', 'RETRIEVAL_QUERY_HMAC_KEY',
                 'RESPONSE_FINGERPRINT_HMAC_KEY', 'SHADOW_CANDIDATE_HMAC_KEY'):
        env[f'AGATA_{name}'] = key
    try:
        run('docker', 'network', 'create', network)
        run('docker', 'run', '-d', '--name', postgres, '--network', network,
            '-p', '127.0.0.1::5432', '-e', 'POSTGRES_PASSWORD=disposable-ci-password', 'pgvector/pgvector:0.8.6-pg16')
        for _ in range(60):
            if subprocess.run(['docker', 'exec', postgres, 'pg_isready', '-U', 'postgres'],
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
        run(sys.executable, '-m', 'pytest', '-q', 'tests/test_postgres_baseline.py', cwd=agata, env=migration_env)
        run('docker', 'exec', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint_agata_test', '-v', 'ON_ERROR_STOP=1', '-c',
            'GRANT USAGE ON SCHEMA agata TO ci_owner; GRANT ALL ON ALL TABLES IN SCHEMA agata TO ci_owner; GRANT ALL ON ALL SEQUENCES IN SCHEMA agata TO ci_owner;')
        # Existing integration fixtures deliberately create tenants and roles.
        # Keep those administrative setup operations on the test administrator;
        # the explicit RLS migration test uses its isolated non-bypass role.
        migration_env['AGATA_RLS_TEST_DATABASE_URL'] = dsn.replace('/gpoint_agata_test', '/rls_agata_test')
        run(sys.executable, '-m', 'pytest', '-q', '--ignore=tests/test_postgres_baseline.py', cwd=agata, env=migration_env)
        # Repeat the actual HTTP dialogue under the application's non-bypass
        # role, independently of privileged fixture creation above.
        run(sys.executable, '-m', 'pytest', '-q', 'tests/test_internal_api_e2e.py', cwd=agata, env=env)
        if postgres_only:
            print('POSTGRES_REHEARSAL=PASS; no READY receipt or production images emitted')
            return
        for tag, dockerfile, context in (
            ('gpoint-runtime', 'infra/production/Gpoint.Dockerfile', '.'),
            ('gpoint-agata-runtime', 'services/agata/Dockerfile', 'services/agata'),
        ):
            run('docker', 'build', '--platform', 'linux/amd64', '--build-arg', f'GPOINT_BUILD_SHA={sha}',
                '--label', f'org.opencontainers.image.revision={sha}', '-f', dockerfile, '-t', f'{tag}:{sha}', context)
        docker_dsn = f'postgresql://ci_owner:disposable-ci-password@{postgres}:5432/runtime_agata_test'
        run('docker', 'run', '--rm', '--network', network, '-e', f'DATABASE_URL={docker_dsn}',
            f'gpoint-runtime:{sha}', 'pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'migrate', 'deploy')
        run('docker', 'run', '--rm', '--network', network, '-e', f'DATABASE_URL={docker_dsn}',
            f'gpoint-runtime:{sha}', 'pnpm', '--filter', '@gpoint/db', 'exec', 'prisma', 'migrate', 'status')
        container_env = ['-e', f'AGATA_DATABASE_URL={docker_dsn.replace("postgresql:", "postgresql+asyncpg:")}']
        for name in env:
            if name.startswith('AGATA_') and name not in ('AGATA_DATABASE_URL', 'AGATA_TEST_DATABASE_URL', 'AGATA_RLS_TEST_DATABASE_URL'):
                container_env.extend(['-e', name])
        for _ in range(2):  # Actual artifact migrations must also be idempotent.
            run('docker', 'run', '--rm', '--network', network, *container_env,
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
        images = {}
        for role, tag in [('app', 'gpoint-runtime'), ('agata', 'gpoint-agata-runtime')]:
            image = json.loads(run('docker', 'image', 'inspect', f'{tag}:{sha}', capture=True))[0]
            if image['Config']['Labels'].get('org.opencontainers.image.revision') != sha or image['Architecture'] != 'amd64' or image['Os'] != 'linux':
                raise RuntimeError('candidate image parity failed')
            images[role] = {'id': image['Id'], 'revision': sha, 'platform': 'linux/amd64'}
        archive = output / 'images.tar.gz'
        with gzip.open(archive, 'wb', compresslevel=1) as dest:
            proc = subprocess.Popen(['docker', 'save', f'gpoint-runtime:{sha}', f'gpoint-agata-runtime:{sha}'], stdout=subprocess.PIPE)
            assert proc.stdout is not None
            while chunk := proc.stdout.read(1024 * 1024):
                dest.write(chunk)
            if proc.wait(timeout=600):
                raise RuntimeError('image export failed')
        with archive.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        proof = {'schema': 1, 'candidateSha': sha, 'status': 'READY', 'runId': int(os.environ['GITHUB_RUN_ID']),
                 'gates': {name: 'PASS' for name in ['unit', 'postgres', 'docker', 'migration', 'miniapp', 'agataHealth']},
                 'images': images, 'archiveSha256': digest, 'prismaHead': prisma, 'agataHead': heads,
                 'applicationSmoke': 'DISPOSABLE_DB_DETERMINISTIC_PROVIDER', 'productionUserSmoke': 'NOT_RUN'}
        receipt.write_text(json.dumps(proof, indent=2) + '\n')
    finally:
        for name in (api, postgres):
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    prepare(sys.argv[1], sys.argv[2], postgres_only='--postgres-only' in sys.argv[3:])
