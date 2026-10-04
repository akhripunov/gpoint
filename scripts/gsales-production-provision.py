"""Explicitly approved, baseline-guarded first GSales tenant/config provisioning.

Run on the VPS through release:gpoint under its shared production lock. Secrets
remain in protected files; elevated migration credentials never reach services.
"""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

TEST_TENANTS = {'c4d67889-877e-4d1b-a8d8-597c479d638f', '7508f36f-774a-48aa-8c6b-42f6f160415d'}


def validate_authorization(candidate, approved, expected, live):
    if not all(re.fullmatch(r'[0-9a-f]{40}', value or '') for value in (candidate, approved, expected, live)):
        raise RuntimeError('exact SHA authorization required')
    if candidate != approved or expected != live:
        raise RuntimeError('authorization or production baseline changed')


def role_bindings(tenants, gpoint, gsales):
    gp = str(UUID(gpoint))
    gs = str(UUID(gsales)) if gsales else None
    if gp in TEST_TENANTS or gs in TEST_TENANTS or gs == gp:
        raise RuntimeError('production requires distinct non-test tenants')
    if gp not in tenants or set(tenants) - {gp, gs}:
        raise RuntimeError('unmapped production tenants; explicit inventory review required')
    result = {'gpoint': gp}
    if gs:
        result.update(gsales_runtime=gs, gsales_agata_runtime=gs)
    return result


def checked(args, *, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('protected provisioning command failed; credentials suppressed')
    return result.stdout.strip()


def admin_sql(statement, *, container="shared_postgres_1"):
    return checked(['docker', 'exec', '-i', container, 'sh', '-ec',
                    'exec psql -U "$POSTGRES_USER" -d gpoint -X -At -v ON_ERROR_STOP=1'], data=statement)


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def read_env(file):
    if file.stat().st_mode & 0o077:
        raise RuntimeError('production credential file is not private')
    return dict(line.split('=', 1) for line in file.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)


def stage_env(file, values):
    lines = file.read_text().splitlines() if file.exists() else []
    output = [line for line in lines if line.split('=', 1)[0] not in values]
    output.extend(key + '=' + value for key, value in sorted(values.items()))
    fd, temporary = tempfile.mkstemp(prefix='.provision-pending-', dir=file.parent)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('\n'.join(output) + '\n'); stream.flush(); os.fsync(stream.fileno())
    return Path(temporary)


def provision(args, *, env_file=None, db_container="shared_postgres_1",
              db_network="shared_internal", probe_image=None, backup_directory=None):
    # Internal fixture dependencies have no CLI escape flags. Production uses
    # the same fixed GPoint paths/container and exact baseline image.
    env_file = Path(env_file) if env_file is not None else Path('/opt/gpoint/env/production.env')
    def query(statement):
        return admin_sql(statement, container=db_container)
    migration_file = env_file.with_name('migration.env')
    values = read_env(env_file)
    validate_authorization(args.candidate, args.owner_approved_sha, args.baseline, values.get('RELEASE_SHA'))
    source = urlsplit(values.get('AGATA_DATABASE_URL', '').replace('postgresql+asyncpg:', 'postgresql:'))
    if source.username != 'gpoint' or source.path != '/gpoint' or not source.hostname:
        raise RuntimeError('unexpected GPoint runtime database identity')
    if query("SELECT version_num FROM agata.alembic_version;") != 'agata0010':
        raise RuntimeError('first provisioning requires reviewed agata0010 baseline')
    tenants = query('SELECT id FROM agata.tenants ORDER BY id;').splitlines()
    gp = values['AGATA_TENANT_ID']
    gs = values.get('GSALES_TENANT_ID')
    role_bindings(tenants, gp, gs)
    if not gs:
        gs = str(uuid4())
    bindings = role_bindings(tenants, gp, gs)
    # Existing roles are reused only with a matching protected, nonprivileged
    # login. Never reset an existing password or overwrite a foreign binding.
    credentials = {}
    for role, key in [('gsales_runtime', 'GSALES_DATABASE_URL'), ('gsales_agata_runtime', 'GSALES_AGATA_DATABASE_URL')]:
        present = query('SELECT COUNT(*) FROM pg_roles WHERE rolname=' + literal(role) + ';') == '1'
        if present:
            safe = query('SELECT rolcanlogin AND NOT rolinherit AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles WHERE rolname=' + literal(role) + ';')
            old = urlsplit(values.get(key, '').replace('postgresql+asyncpg:', 'postgresql:'))
            if safe != 't' or old.username != role or not old.password or old.hostname != source.hostname or old.path != '/gpoint':
                raise RuntimeError('existing sales role cannot be safely reused')
            probe = "import psycopg\nwith psycopg.connect(" + repr(values[key].replace('postgresql+asyncpg:', 'postgresql:')) + ") as conn:\n    assert conn.execute('SELECT session_user, current_database()').fetchone() == " + repr((role, 'gpoint')) + "\n"
            checked(['docker', 'run', '--rm', '-i', '--network', db_network,
                     probe_image or 'gpoint-agata-runtime:' + args.baseline, 'python', '-'], data=probe)
            credentials[role] = unquote(old.password)
        else:
            credentials[role] = secrets.token_urlsafe(48)
    spec = importlib.util.spec_from_file_location('backup', Path(__file__).with_name('agata-production-migration.py'))
    backup_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup_module)
    backup_module.DB_CONTAINER = db_container
    backup_module.DB_NETWORK = db_network
    backup = backup_module.complete_backup(args.candidate, directory=backup_directory)
    inventory = json.loads(checked(['docker', 'inspect', db_container]))[0]
    pg = dict(entry.split('=', 1) for entry in inventory['Config']['Env'] if '=' in entry)
    if not pg.get('POSTGRES_USER') or not pg.get('POSTGRES_PASSWORD'):
        raise RuntimeError('private migration administrator credential unavailable')
    def dsn(role, password, asynchronous=False):
        netloc = quote(role, safe='') + ':' + quote(password, safe='') + '@' + source.hostname
        if source.port: netloc += ':' + str(source.port)
        return urlunsplit(('postgresql+asyncpg' if asynchronous else 'postgresql', netloc, '/gpoint', '', ''))
    managed = dict(GSALES_TENANT_ID=gs, GSALES_AGATA_TENANT_ID=gs,
                   GSALES_DATABASE_URL=dsn('gsales_runtime', credentials['gsales_runtime']),
                   GSALES_AGATA_DATABASE_URL=dsn('gsales_agata_runtime', credentials['gsales_agata_runtime'], True),
                   GSALES_PRODUCTION_MODE='true', GSALES_AGATA_LLM_PROVIDER='mac_worker',
                   AGATA_ROLE_TENANT_BINDINGS=json.dumps(bindings, separators=(',', ':')))
    for name in ('OUTREACH_ENABLED', 'AUTO_REPLY_ENABLED', 'MANAGEMENT_ENABLED', 'TELEGRAM_LISTENER_ENABLED'):
        managed['GSALES_' + name] = 'false'
    for name in ('GSALES_OWNER_API_TOKEN', 'GSALES_WORKER_TOKEN', 'GSALES_AGATA_INTERNAL_API_TOKEN'):
        managed[name] = values.get(name) or secrets.token_urlsafe(48)
    if len({managed[key] for key in ('GSALES_OWNER_API_TOKEN', 'GSALES_WORKER_TOKEN', 'GSALES_AGATA_INTERNAL_API_TOKEN')} | {values.get('GPOINT_AGATA_WORKER_TOKEN')}) != 4:
        raise RuntimeError('lane credentials must be distinct')
    for name in ('MESSAGE_AEAD_KEY', 'REQUEST_FINGERPRINT_HMAC_KEY', 'RETRIEVAL_QUERY_HMAC_KEY', 'RESPONSE_FINGERPRINT_HMAC_KEY', 'SHADOW_CANDIDATE_HMAC_KEY'):
        managed['GSALES_AGATA_' + name] = values.get('GSALES_AGATA_' + name) or base64.b64encode(secrets.token_bytes(32)).decode()
    migrator = dsn(pg['POSTGRES_USER'], pg['POSTGRES_PASSWORD'])
    pending = []
    committed = False
    sql_attempted = False
    try:
        staged_runtime = stage_env(env_file, managed)
        pending.append(staged_runtime)
        staged_migration = stage_env(migration_file, {'AGATA_MIGRATOR_DATABASE_URL': migrator.replace('postgresql:', 'postgresql+asyncpg:'), 'GSALES_MIGRATION_DATABASE_URL': migrator})
        pending.append(staged_migration)
        statements = ['BEGIN;', 'SELECT pg_advisory_xact_lock(723803);', 'LOCK TABLE agata.tenants IN ACCESS EXCLUSIVE MODE;']
        # Census is repeated while holding the tenant table lock; concurrent tenant
        # provisioning must fail the transaction instead of expanding its scope.
        expected = ','.join(sorted(tenants))
        statements.append("DO $$ BEGIN IF (SELECT string_agg(id::text, ',' ORDER BY id) FROM agata.tenants) <> " + literal(expected) + " THEN RAISE EXCEPTION 'tenant inventory changed'; END IF; END $$;")
        for role in credentials:
            statements.append('DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=' + literal(role) + ') THEN CREATE ROLE ' + role + ' LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD ' + literal(credentials[role]) + '; END IF; END $$;')
        statements.extend(['INSERT INTO agata.tenants(id) VALUES (' + literal(gs) + ') ON CONFLICT(id) DO NOTHING;',
                           "INSERT INTO agata.regional_policy_profiles(tenant_id,jurisdiction,market,version,status,effective_from,rules) VALUES (" + literal(gs) + ",'GLOBAL','GSALES',1,'active',now(),jsonb_build_object('ai_disclosure_required',true,'self_attestation_allowed',false,'adult_only_features','[]'::jsonb,'disclosure_repeat_hours',24)) ON CONFLICT(tenant_id,jurisdiction,market,version) DO NOTHING;",
                           'GRANT CONNECT ON DATABASE gpoint TO gsales_runtime, gsales_agata_runtime;',
                           'GRANT USAGE ON SCHEMA agata TO gsales_agata_runtime;',
                           'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agata TO gsales_agata_runtime;',
                           'REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON agata.alembic_version FROM gsales_agata_runtime;',
                           'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA agata TO gsales_agata_runtime;', 'COMMIT;'])
        sql_attempted = True
        query('\n'.join(statements))
        committed = True
        os.replace(staged_migration, migration_file)
        os.replace(staged_runtime, env_file)
    except BaseException:
        if not sql_attempted:
            for staged in pending:
                staged.unlink(missing_ok=True)
            raise
        # Never discard the only protected copy of committed role credentials.
        state = 'database committed' if committed else 'SQL attempted; commit outcome requires verification'
        raise RuntimeError('provisioning ' + state + '; protected pending environment files require recovery before release') from None
    print(json.dumps({'status': 'PROVISIONED', 'candidateSha': args.candidate, 'baselineSha': args.baseline,
                      'gpointTenant': gp, 'gsalesTenant': gs, 'backup': backup, 'sendingEnabled': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for key in ('candidate', 'owner-approved-sha', 'baseline'):
        parser.add_argument('--' + key, required=True)
    try:
        provision(parser.parse_args())
    except Exception as error:
        raise SystemExit('BLOCKED: ' + str(error)) from None
