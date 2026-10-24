#!/usr/bin/env python3
"""Create/reconcile the persistent Eros runtime secret file on the VPS.

Secret material is generated on the trusted VPS itself and is never emitted to
stdout/stderr. The separate file is mode 0600 and atomically replaced.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from urllib.parse import quote, unquote, urlsplit
import uuid


ENV_PATH = Path('/opt/gpoint/env/eros.env')
SOURCE_ENV_PATH = Path('/opt/gpoint/env/production.env')
SECRET_KEYS = (
    'EROS_JWT_SECRET',
    'EROS_EMBEDDING_API_KEY',
    'EROS_BRIDGE_API_KEY',
    'EROS_WORKER_SIGNING_SECRET',
    'EROS_RUNTIME_PASSWORD',
    'EROS_MIGRATOR_PASSWORD',
    'EROS_DATABASE_IDENTITY',
)
MANAGED_KEYS = frozenset((*SECRET_KEYS, 'EROS_RUNTIME_DATABASE_URL',
                          'EROS_MIGRATOR_DATABASE_URL', 'EROS_SOURCE_SHA', 'EROS_IMAGE', 'EROS_TENANT_ID',
                          'EROS_TEST_USER_IDS', 'EROS_TEST_CREATOR_IDS'))


def read_env(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith('#'):
            continue
        if '=' not in line:
            raise RuntimeError('Eros protected environment has a malformed entry')
        key, value = line.split('=', 1)
        if key not in MANAGED_KEYS:
            raise RuntimeError('Eros protected environment contains an unexpected entry')
        if key in values:
            raise RuntimeError('Eros protected environment contains duplicate entries')
        values[key] = value
    return values


def _new_values(source_sha, image, tenant_id):
    if not re.fullmatch(r'[a-f0-9]{40}', source_sha or ''):
        raise ValueError('exact Eros source SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image or ''):
        raise ValueError('immutable Eros GHCR image digest is required')
    try:
        parsed_tenant_id = uuid.UUID(tenant_id)
    except (AttributeError, ValueError, TypeError):
        raise ValueError('existing GPoint tenant identity is required') from None
    if parsed_tenant_id.int == 0 or str(parsed_tenant_id) != tenant_id:
        raise ValueError('existing GPoint tenant identity is invalid')
    values = {key: secrets.token_urlsafe(48) for key in SECRET_KEYS}
    def dsn(role, password):
        return f'postgresql://{role}:{quote(password, safe="")}@gpoint-eros-db:5432/eros'
    values.update({
        'EROS_RUNTIME_DATABASE_URL': dsn('eros_runtime', values['EROS_RUNTIME_PASSWORD']),
        'EROS_MIGRATOR_DATABASE_URL': dsn('eros_migrator', values['EROS_MIGRATOR_PASSWORD']),
        'EROS_SOURCE_SHA': source_sha,
        'EROS_IMAGE': image,
        'EROS_TENANT_ID': tenant_id,
        'EROS_TEST_USER_IDS': '',
        'EROS_TEST_CREATOR_IDS': '',
    })
    return values


def _validate_release_identity(source_sha, image):
    if not re.fullmatch(r'[a-f0-9]{40}', source_sha or ''):
        raise ValueError('exact Eros source SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image or ''):
        raise ValueError('immutable Eros GHCR image digest is required')


def _source_tenant(path):
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        raise RuntimeError('GPoint tenant identity source is missing') from None
    for line in lines:
        if line.startswith('AGATA_TENANT_ID=') or line.startswith('GPOINT_TENANT_ID='):
            key, value = line.split('=', 1)
            if key in values and values[key] != value:
                raise RuntimeError('GPoint tenant identity source is ambiguous')
            values[key] = value
    tenant = values.get('GPOINT_TENANT_ID') or values.get('AGATA_TENANT_ID')
    try:
        parsed = uuid.UUID(tenant or '')
    except (ValueError, TypeError):
        raise RuntimeError('GPoint tenant identity source is invalid') from None
    if parsed.int == 0 or str(parsed) != tenant:
        raise RuntimeError('GPoint tenant identity source is invalid')
    return tenant


def provision(path=ENV_PATH, *, source_sha, image, tenant_id=None, source_env=SOURCE_ENV_PATH):
    _validate_release_identity(source_sha, image)
    path = Path(path)
    if path.parent.is_symlink():
        raise RuntimeError('Eros protected environment directory cannot be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError('Eros protected environment path cannot be a symlink')
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError('Eros protected environment must be a regular file')
    if path.parent.stat().st_mode & 0o077:
        raise RuntimeError('Eros protected environment directory has unsafe permissions')
    if path == ENV_PATH and path.parent.stat().st_uid != 0:
        raise RuntimeError('Eros protected environment directory must be root-owned')
    if path.exists():
        metadata = path.stat()
        if metadata.st_mode & 0o077 or metadata.st_nlink != 1:
            raise RuntimeError('Eros protected environment file has unsafe permissions')
        if path == ENV_PATH and metadata.st_uid != 0:
            raise RuntimeError('Eros protected environment file must be root-owned')
        values = read_env(path)
        missing = set(MANAGED_KEYS) - set(values) - {'EROS_TENANT_ID', 'EROS_TEST_USER_IDS', 'EROS_TEST_CREATOR_IDS'}
        if missing and values:
            raise RuntimeError('Eros protected environment is incomplete; credential recovery is required')
    else:
        values = {}

    if not values:
        values = _new_values(source_sha, image, tenant_id or _source_tenant(source_env))
    else:
        if any(not values.get(key) for key in SECRET_KEYS):
            raise RuntimeError('Eros protected environment has an empty credential')
        values['EROS_SOURCE_SHA'] = source_sha
        values['EROS_IMAGE'] = image
        values['EROS_TEST_USER_IDS'] = ''
        values['EROS_TEST_CREATOR_IDS'] = ''
        existing_tenant = values.get('EROS_TENANT_ID')
        if not existing_tenant:
            values['EROS_TENANT_ID'] = tenant_id or _source_tenant(source_env)
        elif tenant_id and tenant_id != existing_tenant:
            raise RuntimeError('GPoint tenant identity changed; explicit migration is required')
        try:
            parsed_tenant = uuid.UUID(values['EROS_TENANT_ID'])
        except (ValueError, TypeError):
            raise RuntimeError('protected GPoint tenant identity is invalid') from None
        if parsed_tenant.int == 0 or str(parsed_tenant) != values['EROS_TENANT_ID']:
            raise RuntimeError('protected GPoint tenant identity is invalid')
        # Existing URLs must still match the protected passwords; refuse to
        # silently repair an inconsistent credential pair or rotate a secret.
        for key, username, password_key in (
                ('EROS_RUNTIME_DATABASE_URL', 'eros_runtime', 'EROS_RUNTIME_PASSWORD'),
                ('EROS_MIGRATOR_DATABASE_URL', 'eros_migrator', 'EROS_MIGRATOR_PASSWORD')):
            dsn = urlsplit(values[key])
            if (dsn.scheme != 'postgresql' or dsn.hostname != 'gpoint-eros-db' or dsn.port != 5432
                    or dsn.path != '/eros' or dsn.username != username
                    or unquote(dsn.password or '') != values[password_key]):
                raise RuntimeError('Eros database URL and protected credential disagree')

    fd, staged = tempfile.mkstemp(prefix='.eros.env.', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            for key in sorted(values):
                stream.write(f'{key}={values[key]}\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
    return 'EROS_SECRET_STORE=READY'


def _psql(sql, database='postgres'):
    result = subprocess.run(
        ['docker', 'exec', '-i', 'shared_postgres_1', 'sh', '-ec',
         'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d ' + database],
        input=sql, text=True, capture_output=True, timeout=45)
    if result.returncode:
        # Do not relay psql output: a database error may echo SQL credentials.
        raise RuntimeError('Eros PostgreSQL provisioning failed; no credential output was retained')
    return result.stdout.strip()


def assert_shared_database_acl_isolatable():
    public_connect = _psql("SELECT coalesce(json_agg(datname ORDER BY datname), '[]')::text FROM pg_database WHERE datallowconn AND datname <> 'eros' AND EXISTS (SELECT 1 FROM aclexplode(COALESCE(datacl, acldefault('d', datdba))) acl WHERE acl.grantee=0 AND acl.privilege_type='CONNECT')")
    try:
        public_databases = json.loads(public_connect)
    except (TypeError, ValueError):
        raise RuntimeError('shared PostgreSQL database ACL inventory is unrecognized') from None
    if public_databases:
        raise RuntimeError('shared PostgreSQL grants PUBLIC CONNECT outside eros; isolate database access before Eros provisioning')


def provision_database(path=ENV_PATH):
    """Provision one database and roles on the existing PostgreSQL cluster."""
    values = read_env(Path(path))
    if set(SECRET_KEYS) - set(values):
        raise RuntimeError('Eros protected environment is incomplete')

    assert_shared_database_acl_isolatable()

    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    database_exists = _psql("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname='eros')::text")
    if database_exists not in ('true', 'false'):
        raise RuntimeError('Eros database inventory is unrecognized')
    if database_exists == 'true':
        owner = _psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='eros'")
        if owner != 'eros_ddl':
            raise RuntimeError('existing Eros database has an unexpected owner')
        marker = _psql("SELECT identity FROM public.eros_deployment_identity WHERE singleton=true", 'eros')
        if marker != values['EROS_DATABASE_IDENTITY']:
            raise RuntimeError('existing Eros database has no matching deployment identity')
    shared_role_state = _psql("""SELECT json_build_object(
      'memberships', (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.roleid,m.member)
        WHERE r.rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
      'foreign_database_owners', (SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
        WHERE r.rolname IN ('eros_ddl','eros_runtime','eros_migrator') AND d.datname <> 'eros'),
      'foreign_database_connections', (SELECT count(*) FROM pg_database d CROSS JOIN pg_roles r
        WHERE r.rolname IN ('eros_runtime','eros_migrator') AND d.datname <> 'eros' AND d.datallowconn
          AND has_database_privilege(r.oid,d.oid,'CONNECT')),
      'existing_eros_roles', (SELECT count(*) FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator'))
    )::text""")
    try:
        role_state = json.loads(shared_role_state)
    except (TypeError, ValueError):
        raise RuntimeError('Eros role inventory is unrecognized') from None
    expected_role_state = {'memberships': 0, 'foreign_database_owners': 0,
                           'foreign_database_connections': 0,
                           'existing_eros_roles': 3 if database_exists == 'true' else 0}
    if role_state != expected_role_state:
        raise RuntimeError('Eros role inventory is unsafe')

    statements = [
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='eros_ddl') THEN CREATE ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT; END IF; END $$;",
        'ALTER ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;',
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=\'eros_migrator\') THEN CREATE ROLE eros_migrator LOGIN; END IF; END $$;',
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=\'eros_runtime\') THEN CREATE ROLE eros_runtime LOGIN; END IF; END $$;',
        'ALTER ROLE eros_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD ' + literal(values['EROS_MIGRATOR_PASSWORD']) + ';',
        'ALTER ROLE eros_runtime WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS NOINHERIT PASSWORD ' + literal(values['EROS_RUNTIME_PASSWORD']) + ';',
        'REVOKE CONNECT ON DATABASE gpoint FROM eros_runtime, eros_migrator;',
        "SELECT format('REVOKE CONNECT ON DATABASE %I FROM eros_runtime, eros_migrator', datname) FROM pg_database WHERE datname <> 'eros' \\gexec",
    ]
    _psql('\n'.join(statements))
    if database_exists == 'false':
        _psql('CREATE DATABASE eros OWNER eros_ddl;')

    database_setup = '''
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE TABLE IF NOT EXISTS public.eros_deployment_identity (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  identity text NOT NULL
);
INSERT INTO public.eros_deployment_identity(singleton, identity)
VALUES (true, ''' + literal(values['EROS_DATABASE_IDENTITY']) + ''')
ON CONFLICT (singleton) DO NOTHING;
DO $$ BEGIN
  IF (SELECT identity FROM public.eros_deployment_identity WHERE singleton=true) <> ''' + literal(values['EROS_DATABASE_IDENTITY']) + ''' THEN
    RAISE EXCEPTION 'Eros database identity mismatch';
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='engine') THEN
    CREATE SCHEMA engine AUTHORIZATION eros_ddl;
  ELSIF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='engine') <> 'eros_ddl' THEN
    RAISE EXCEPTION 'unexpected Eros schema owner';
  END IF;
END $$;
REVOKE CONNECT ON DATABASE eros FROM PUBLIC;
GRANT CONNECT ON DATABASE eros TO eros_runtime, eros_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO eros_migrator;
GRANT USAGE, CREATE ON SCHEMA engine TO eros_migrator;
GRANT USAGE ON SCHEMA public, engine TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT EXECUTE ON FUNCTIONS TO eros_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA engine TO eros_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA engine TO eros_runtime;
'''
    _psql(database_setup, 'eros')
    foreign_connect = _psql("SELECT coalesce(json_agg(datname ORDER BY datname), '[]')::text FROM pg_database WHERE datallowconn AND datname <> 'eros' AND (has_database_privilege('eros_runtime', datname, 'CONNECT') OR has_database_privilege('eros_migrator', datname, 'CONNECT'))")
    try:
        accessible = json.loads(foreign_connect)
    except (TypeError, ValueError):
        raise RuntimeError('Eros database access isolation read-back is unrecognized') from None
    if accessible:
        raise RuntimeError('Eros roles can CONNECT to databases outside eros; database ACL isolation is required')
    return 'EROS_DATABASE_ROLES=READY'


def ensure_postgres_network():
    """Attach the existing PostgreSQL container to a private Eros-only network."""
    def docker(args, *, allow_missing=False):
        result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=30)
        if result.returncode:
            detail = (result.stderr + result.stdout).lower()
            if allow_missing and ('no such network' in detail or 'network gpoint_eros_data not found' in detail):
                return None
            raise RuntimeError('Eros PostgreSQL network provisioning failed')
        return result.stdout.strip()

    inspected = docker(['network', 'inspect', 'gpoint_eros_data'], allow_missing=True)
    network = json.loads(inspected)[0] if inspected else None
    if network is None:
        docker(['network', 'create', '--driver', 'bridge', '--internal',
                '--label', 'com.gpoint.eros.managed=true', 'gpoint_eros_data'])
        network = json.loads(docker(['network', 'inspect', 'gpoint_eros_data']))[0]
    if network.get('Driver') != 'bridge' or network.get('Internal') is not True:
        raise RuntimeError('existing Eros data network is not private bridge network')
    container = json.loads(docker(['inspect', 'shared_postgres_1']))[0]
    networks = container.get('NetworkSettings', {}).get('Networks', {})
    attached = networks.get('gpoint_eros_data')
    if attached and 'gpoint-eros-db' in attached.get('Aliases', []):
        return 'EROS_POSTGRES_NETWORK=READY'
    if attached:
        raise RuntimeError('PostgreSQL is attached to Eros network without the required alias')
    docker(['network', 'connect', '--alias', 'gpoint-eros-db', 'gpoint_eros_data', 'shared_postgres_1'])
    verify = json.loads(docker(['inspect', 'shared_postgres_1']))[0]
    aliases = verify.get('NetworkSettings', {}).get('Networks', {}).get('gpoint_eros_data', {}).get('Aliases', [])
    if 'gpoint-eros-db' not in aliases:
        raise RuntimeError('PostgreSQL Eros network alias read-back failed')
    return 'EROS_POSTGRES_NETWORK=READY'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', default=str(ENV_PATH))
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--tenant-id')
    parser.add_argument('--source-env', default=str(SOURCE_ENV_PATH))
    parser.add_argument('--ensure-postgres-network', action='store_true')
    parser.add_argument('--provision-database', action='store_true')
    args = parser.parse_args()
    try:
        if args.provision_database:
            assert_shared_database_acl_isolatable()
        print(provision(args.path, source_sha=args.source_sha, image=args.image,
                        tenant_id=args.tenant_id, source_env=args.source_env))
        if args.ensure_postgres_network:
            print(ensure_postgres_network())
        if args.provision_database:
            print(provision_database(args.path))
    except Exception as error:
        # Error text is intentionally limited to classifications, never values.
        raise SystemExit('BLOCKED: ' + str(error)) from None


if __name__ == '__main__':
    main()
