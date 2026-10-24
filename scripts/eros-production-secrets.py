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


ENV_PATH = Path('/opt/gpoint/env/eros.env')
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
                          'EROS_MIGRATOR_DATABASE_URL', 'EROS_SOURCE_SHA', 'EROS_IMAGE'))


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


def _new_values(source_sha, image):
    if not re.fullmatch(r'[a-f0-9]{40}', source_sha or ''):
        raise ValueError('exact Eros source SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image or ''):
        raise ValueError('immutable Eros GHCR image digest is required')
    values = {key: secrets.token_urlsafe(48) for key in SECRET_KEYS}
    def dsn(role, password):
        return f'postgresql://{role}:{quote(password, safe="")}@gpoint-eros-db:5432/eros'
    values.update({
        'EROS_RUNTIME_DATABASE_URL': dsn('eros_runtime', values['EROS_RUNTIME_PASSWORD']),
        'EROS_MIGRATOR_DATABASE_URL': dsn('eros_migrator', values['EROS_MIGRATOR_PASSWORD']),
        'EROS_SOURCE_SHA': source_sha,
        'EROS_IMAGE': image,
    })
    return values


def _validate_release_identity(source_sha, image):
    if not re.fullmatch(r'[a-f0-9]{40}', source_sha or ''):
        raise ValueError('exact Eros source SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image or ''):
        raise ValueError('immutable Eros GHCR image digest is required')


def provision(path=ENV_PATH, *, source_sha, image):
    _validate_release_identity(source_sha, image)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
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
        missing = set(MANAGED_KEYS) - set(values)
        if missing and values:
            raise RuntimeError('Eros protected environment is incomplete; credential recovery is required')
    else:
        values = {}

    if not values:
        values = _new_values(source_sha, image)
    else:
        if any(not values.get(key) for key in SECRET_KEYS):
            raise RuntimeError('Eros protected environment has an empty credential')
        values['EROS_SOURCE_SHA'] = source_sha
        values['EROS_IMAGE'] = image
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


def provision_database(path=ENV_PATH):
    """Provision one database and roles on the existing PostgreSQL cluster."""
    values = read_env(Path(path))
    if set(SECRET_KEYS) - set(values):
        raise RuntimeError('Eros protected environment is incomplete')

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
        WHERE r.rolname IN ('eros_ddl','eros_runtime','eros_migrator') AND d.datname <> 'eros')
    )::text""")
    try:
        role_state = json.loads(shared_role_state)
    except (TypeError, ValueError):
        raise RuntimeError('Eros role inventory is unrecognized') from None
    if role_state != {'memberships': 0, 'foreign_database_owners': 0}:
        raise RuntimeError('Eros role inventory is unsafe')

    statements = [
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='eros_ddl') THEN CREATE ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT; END IF; END $$;",
        'ALTER ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;',
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=\'eros_migrator\') THEN CREATE ROLE eros_migrator LOGIN; END IF; END $$;',
        'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=\'eros_runtime\') THEN CREATE ROLE eros_runtime LOGIN; END IF; END $$;',
        'ALTER ROLE eros_migrator WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD ' + literal(values['EROS_MIGRATOR_PASSWORD']) + ';',
        'ALTER ROLE eros_runtime WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS NOINHERIT PASSWORD ' + literal(values['EROS_RUNTIME_PASSWORD']) + ';',
        'REVOKE CONNECT ON DATABASE gpoint FROM eros_runtime, eros_migrator;',
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
    return 'EROS_DATABASE_ROLES=READY'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', default=str(ENV_PATH))
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--provision-database', action='store_true')
    args = parser.parse_args()
    try:
        print(provision(args.path, source_sha=args.source_sha, image=args.image))
        if args.provision_database:
            print(provision_database(args.path))
    except Exception as error:
        # Error text is intentionally limited to classifications, never values.
        raise SystemExit('BLOCKED: ' + str(error)) from None


if __name__ == '__main__':
    main()
