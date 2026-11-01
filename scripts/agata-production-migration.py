#!/usr/bin/env python3
"""GPoint-only guarded migrations through agata0012.

The administrator channel proves census/backup completeness. Alembic continues
to use its existing effective production identity and FORCE RLS remains enabled.
No database URLs, credentials or tenant IDs are emitted.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import time
from urllib.parse import urlsplit
from uuid import UUID

DB_CONTAINER = 'shared_postgres_1'
ENV_FILE = '/opt/gpoint/env/production.env'
MIGRATION_ENV_FILE = '/opt/gpoint/env/migration.env'
DB_NETWORK = 'shared_internal'
RELEASE_LOG_ROOT = Path('/opt/gpoint/release-logs')
PREDICATE = "c.source_system='gpoint' AND m.role='assistant' AND m.scope='real_world' AND m.scenario_id IS NULL"
TENANT_GUARD_LOCK = (1196441417, 1314150988)
# JSON access also supports older baselines before incoming_read_at existed.
READ_STATE_SQL = "encode(sha256(convert_to(COALESCE(jsonb_agg(jsonb_build_array(m.id,to_jsonb(m)->'incoming_read_at') ORDER BY m.id),'[]'::jsonb)::text,'UTF8')),'hex')"
CENSUS_SQL = f"""SELECT json_build_object(
 'database',current_database(),
 'database_oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
 'authoritative',(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user),
 'tenants',(SELECT coalesce(json_agg(id ORDER BY id),'[]') FROM agata.tenants),
 'messages',(SELECT count(*) FROM agata.messages),
 'baseline',(SELECT count(*) FROM agata.messages m JOIN agata.conversations c
 ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id WHERE {PREDICATE}),
 'baseline_read_state_sha256',(SELECT {READ_STATE_SQL} FROM agata.messages m JOIN agata.conversations c ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id WHERE {PREDICATE}),
 'baseline_ids',(SELECT coalesce(json_agg(id),'[]') FROM
 (SELECT m.id FROM agata.messages m JOIN agata.conversations c
 ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id WHERE {PREDICATE} LIMIT 10001) s));"""
PROBE = """
import json, os, psycopg
from sqlalchemy.engine import make_url
source = 'AGATA_MIGRATOR_DATABASE_URL' if os.environ.get('AGATA_MIGRATOR_DATABASE_URL') else 'AGATA_DATABASE_URL'
url = make_url(os.environ[source])
url = url.set(drivername='postgresql', query={k:v for k,v in url.query.items() if k in {'sslmode','sslrootcert','sslcert','sslkey','connect_timeout','application_name'}})
with psycopg.connect(url.render_as_string(hide_password=False)) as conn:
    conn.read_only = True
    conn.execute("SET LOCAL statement_timeout='15s'")
    row=conn.execute('''SELECT current_database(),current_user,
    (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user),
    pg_has_role(current_user,relowner,'USAGE'),relrowsecurity,relforcerowsecurity,
    has_schema_privilege('agata','CREATE'),has_schema_privilege('agata','USAGE'),
    has_table_privilege('agata.messages','SELECT') AND has_table_privilege('agata.messages','UPDATE') AND
    has_table_privilege('agata.tenants','SELECT') AND
    has_table_privilege('agata.conversations','SELECT') AND
    has_table_privilege('agata.alembic_version','SELECT') AND has_table_privilege('agata.alembic_version','INSERT') AND has_table_privilege('agata.alembic_version','UPDATE') AND has_table_privilege('agata.alembic_version','DELETE'),
    pg_total_relation_size(oid) FROM pg_class WHERE oid='agata.messages'::regclass''').fetchone()
    heads=conn.execute('SELECT version_num FROM agata.alembic_version').fetchall()
    if len(heads)!=1: raise RuntimeError('migration head is not singular')
    data=dict(zip(('database','role','privileged','owner','rls','force_rls','schema_create','schema_usage','permissions','total_bytes'),row))
    identity=conn.execute('SELECT host(inet_server_addr()),(SELECT oid::bigint FROM pg_database WHERE datname=current_database())').fetchone()
    data.update(head=heads[0][0],url_source=source,server_address=identity[0],database_oid=identity[1])
    print(json.dumps(data))
"""


def validate_readiness(proof, census):
    if proof.get('database') != 'gpoint' or census.get('database') != 'gpoint':
        raise RuntimeError('GPoint database identity mismatch')
    if proof.get('server_address') not in census.get('cluster_addresses', []) or not proof.get('database_oid') or proof['database_oid'] != census.get('database_oid'):
        raise RuntimeError('authoritative database instance identity mismatch')
    if proof.get('head') not in ('agata0009', 'agata0010', 'agata0011'):
        raise RuntimeError('guard only supports production baselines agata0009, agata0010, or agata0011')
    if not all(proof.get(key) is True for key in ('owner', 'schema_create', 'schema_usage', 'permissions', 'rls', 'force_rls')):
        raise RuntimeError('effective migrator lacks required ownership/privileges')
    if census.get('authoritative') is not True:
        raise RuntimeError('tenant census source is not authoritative')
    try:
        tenants = sorted({str(UUID(value)) for value in census['tenants']})
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('invalid complete tenant inventory') from exc
    if not tenants:
        raise RuntimeError('complete tenant inventory is absent or empty')
    if census.get('baseline', 10001) > 10000 or proof.get('total_bytes', 67108865) > 64 * 1024 * 1024:
        raise RuntimeError('baseline volume exceeds bounded migration rehearsal envelope')
    inventory = ','.join(tenants)
    # This candidate also applies agata0012, which enumerates every tenant to
    # preserve disclosure receipts. Require the verified privileged migrator.
    target_head = 'agata0012'
    if proof.get('privileged') is not True:
        raise RuntimeError('agata0012 requires a verified privileged migration identity')
    if proof.get('head') == 'agata0011' and not re.fullmatch(r'[0-9a-f]{64}', str(census.get('baseline_read_state_sha256', ''))):
        raise RuntimeError('agata0012 requires a verified baseline read-state snapshot')
    return dict(proof, target_head=target_head, tenant_count=len(tenants), inventory=inventory,
                inventory_sha256=hashlib.sha256(inventory.encode()).hexdigest(),
                messages=census['messages'], baseline=census['baseline'],
                lock_risk='BOUNDED_MIGRATION_LOCK_REQUIRED', lock_timeout_seconds=15,
                statement_timeout_seconds=120, readiness='PASS')


def public_facts(proof):
    return {key: value for key, value in proof.items() if key not in ('inventory', 'tenants', 'baseline_ids')}


def guarded_migration(proof, guard, migrate):
    # 0012 changes disclosure versions only. A baseline already at 0011 must
    # retain every message receipt, including intentionally unread messages.
    guard.preserve_read_state = proof.get('head') == 'agata0011'
    with guard:
        readiness = validate_readiness(proof, guard.census())
        if not guard.alive():
            raise RuntimeError('tenant inventory guard lost before migration')
        migrate(readiness['inventory'], guard)
        if not guard.alive():
            raise RuntimeError('tenant guard was lost during migration')
        if not guard.verify(readiness['target_head']):
            facts = getattr(guard, 'verification', {})
            raise RuntimeError(f"migration verification failed (actual_head={facts.get('head', 'unknown')}, expected_head={readiness['target_head']}, baseline_missing={facts.get('missing', 'unknown')})")
        return public_facts(readiness)


def docker_checked(args, *, input=None, timeout=30):
    result = subprocess.run(['docker', *args], input=input, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        # Docker/psycopg stderr can include environment details. Keep diagnostics bounded.
        raise RuntimeError('GPoint database command failed (details withheld to protect credentials)')
    return result.stdout.strip()


def psql_args(container=None):
    container = DB_CONTAINER if container is None else container
    return ['exec', '-i', container, 'sh', '-ec',
            'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint']


def authoritative_census(container=None):
    container = DB_CONTAINER if container is None else container
    sql = "BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; " + CENSUS_SQL + ' ROLLBACK;'
    return add_cluster_identity(json.loads(docker_checked(psql_args(container), input=sql)), container)


def resolve_recovery_postgres_container():
    """Resolve exactly one running GPoint shared PostgreSQL by Compose identity."""
    result = subprocess.run(['docker', 'ps', '-aq',
        '--filter', 'label=com.docker.compose.project=shared',
        '--filter', 'label=com.docker.compose.service=postgres'],
        capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('recovery PostgreSQL container identity could not be read')
    container_ids = result.stdout.split()
    if len(container_ids) != 1 or not re.fullmatch(r'[0-9a-f]{12,64}', container_ids[0]):
        raise RuntimeError('recovery requires one unique running PostgreSQL container')
    try:
        inspected = subprocess.run(['docker', 'inspect', container_ids[0]],
            capture_output=True, text=True, timeout=15)
        item = json.loads(inspected.stdout)[0] if inspected.returncode == 0 else None
        labels = (item or {}).get('Config', {}).get('Labels') or {}
    except (IndexError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        item = None
        labels = {}
    if (not item or item.get('Name') != '/shared_postgres_1'
            or labels.get('com.docker.compose.project') != 'shared'
            or labels.get('com.docker.compose.service') != 'postgres'
            or item.get('State', {}).get('Running') is not True):
        raise RuntimeError('recovery PostgreSQL container identity is invalid or not running')
    return container_ids[0]


def migration_env_files():
    files = [ENV_FILE]
    migration = Path(MIGRATION_ENV_FILE)
    if migration.exists():
        if not migration.is_file() or migration.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError('migration environment must be a protected 0600 file')
        files.append(str(migration))
    return files


def migration_env_args():
    return [arg for path in migration_env_files() for arg in ('--env-file', path)]


def effective_probe(image):
    return json.loads(docker_checked(['run', '--rm', '--network', DB_NETWORK,
        *migration_env_args(), '--entrypoint', 'python', image, '-c', PROBE]))


def add_cluster_identity(census, container=None):
    container = DB_CONTAINER if container is None else container
    networks = json.loads(docker_checked(['inspect', '--format', '{{json .NetworkSettings.Networks}}', container]))
    census['cluster_addresses'] = [network['IPAddress'] for network in networks.values() if network.get('IPAddress')]
    return census


class TenantGuard:
    def __enter__(self):
        self.process = subprocess.Popen(['docker', *psql_args()], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        try:
            acquired = self.query("SELECT json_build_object('pid',pg_backend_pid(),'locked',"
                f"pg_try_advisory_lock({TENANT_GUARD_LOCK[0]},{TENANT_GUARD_LOCK[1]}));")
            if acquired.get('locked') is not True:
                raise RuntimeError('tenant migration guard is already held')
            self.backend_pid = acquired['pid']
            # Freeze tenant writes while the census is captured. Release the
            # relation lock before Alembic: agata0011 needs ACCESS EXCLUSIVE to
            # install RLS on agata.tenants. The advisory lock remains session-held.
            locked_snapshot = self.query("BEGIN; SET LOCAL lock_timeout='15s'; "
                "SET LOCAL statement_timeout='120s'; SET LOCAL idle_in_transaction_session_timeout='180s'; "
                "LOCK TABLE agata.tenants IN SHARE MODE; SELECT json_build_object('pid',pg_backend_pid(),'tenants',coalesce(json_agg(id ORDER BY id),'[]')) FROM agata.tenants; COMMIT;")
            if locked_snapshot['pid'] != self.backend_pid:
                raise RuntimeError('tenant inventory session identity changed')
            self.snapshot = authoritative_census()
            if sorted(locked_snapshot['tenants']) != sorted(self.snapshot['tenants']):
                raise RuntimeError('authoritative census does not match locked inventory')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def query(self, sql, timeout=20):
        self.process.stdin.write(sql + '\n'); self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise RuntimeError('authoritative inventory transaction timed out')
        line = self.process.stdout.readline()
        if not line: raise RuntimeError('authoritative inventory transaction failed')
        return json.loads(line)

    def census(self): return self.snapshot

    def alive(self):
        if self.process.poll() is not None:
            return False
        try:
            result = self.query("SELECT json_build_object('pid',pg_backend_pid(),'locked',"
                "EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' "
                f"AND classid={TENANT_GUARD_LOCK[0]} AND objid={TENANT_GUARD_LOCK[1]} "
                "AND objsubid=2 AND granted));", timeout=2)
            return result['pid'] == self.backend_pid and result['locked'] is True
        except (OSError, RuntimeError, ValueError, KeyError):
            return False

    def verify(self, target_head):
        ids = [str(UUID(value)) for value in self.snapshot['baseline_ids']]
        # Only pre-existing IDs: incoming messages after migration remain unread.
        literals = ','.join("'" + value + "'::uuid" for value in ids)
        predicate = f'm.id IN ({literals})' if ids else 'false'
        result = self.query("SELECT json_build_object('head',(SELECT version_num FROM agata.alembic_version),"
            f"'missing',(SELECT count(*) FROM agata.messages m WHERE {predicate} AND incoming_read_at IS NULL),"
            f"'retained',(SELECT count(*) FROM agata.messages m WHERE {predicate}),"
            f"'read_state_sha256',(SELECT {READ_STATE_SQL} FROM agata.messages m WHERE {predicate}));")
        self.verification = result
        if getattr(self, 'preserve_read_state', False):
            return (result['head'] == target_head and result['retained'] == self.snapshot['baseline']
                    and result['read_state_sha256'] == self.snapshot['baseline_read_state_sha256'])
        return result['head'] == target_head and result['missing'] == 0

    def __exit__(self, *args):
        if getattr(self, 'process', None) is None: return
        try:
            self.process.communicate('ROLLBACK;\n\\q\n', timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            self.process.kill(); self.process.wait(timeout=5)


def migrate_image(image, inventory, guard):
    name = 'gpoint-agata-migration-' + image.rsplit(':', 1)[1][:12]
    args = ['docker', 'run', '--rm', '--name', name, '--network', DB_NETWORK,
            *migration_env_args(), '-e', 'AGATA_MIGRATION_TENANT_IDS=' + inventory,
            '-e', 'PGOPTIONS=-c lock_timeout=15000 -c statement_timeout=120000',
            image, 'alembic', 'upgrade', 'head']
    # Drain stderr without exposing SQL/credentials. Outer process has a hard deadline.
    with open(os.devnull, 'w') as sink:
        process = subprocess.Popen(args, stdout=sink, stderr=sink)
        deadline = time.monotonic() + 150
        try:
            while process.poll() is None:
                if not guard.alive() or time.monotonic() >= deadline:
                    raise RuntimeError('migration guard lost or bounded deadline exceeded')
                time.sleep(0.1)
            if process.returncode:
                raise RuntimeError('restricted AGATA migration failed; runtime was not switched')
        finally:
            if process.poll() is None:
                subprocess.run(['docker', 'stop', '--time', '2', name], stdout=sink, stderr=sink, timeout=10)
                process.wait(timeout=10)



def verify_migration_image(image, sha, expected_checksum=None):
    if not re.fullmatch(r'ghcr\.io/akhripunov/gpoint-agata-runtime@sha256:[0-9a-f]{64}', image):
        raise RuntimeError('an immutable GPoint AGATA image is required')
    info = json.loads(docker_checked(['image', 'inspect', image, '--format', '{{json .}}']))
    if (info.get('Os') != 'linux' or info.get('Architecture') != 'amd64'
            or info.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha
            or image not in (info.get('RepoDigests') or [])):
        raise RuntimeError('migration image source, platform or digest does not match candidate')
    value = docker_checked(['run', '--rm', '--network', 'none', '--entrypoint', 'sha256sum', image,
                            '/srv/agata/migrations/versions/0012_eros_memory_disclosure.py']).split()
    if (len(value) != 2 or not re.fullmatch(r'[0-9a-f]{64}', value[0])
            or value[1] != '/srv/agata/migrations/versions/0012_eros_memory_disclosure.py'
            or (expected_checksum is not None and value[0] != expected_checksum)):
        raise RuntimeError('migration image checksum does not match approved source')
    return value[0]


def migrate_candidate(image, sha, *, record_receipt=False, expected_migration_sha256=None):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise RuntimeError('exact migration source SHA required')
    receipt_path = None
    if record_receipt:
        if not re.fullmatch(r'[0-9a-f]{64}', expected_migration_sha256 or ''):
            raise RuntimeError('approved migration checksum required before Apply')
        receipt_path = RELEASE_LOG_ROOT / sha / 'agata-applied-migration.json'
        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = receipt_path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise RuntimeError('migration receipt directory is not private')
        if receipt_path.exists() or receipt_path.is_symlink():
            raise RuntimeError('migration receipt already exists; reconcile before another Apply')
    checksum = verify_migration_image(image, sha, expected_migration_sha256)
    proof = effective_probe(image)
    if record_receipt and proof.get('head') != 'agata0011':
        raise RuntimeError('migration receipt requires the approved agata0011 baseline')
    result = guarded_migration(proof, TenantGuard(), lambda ids, guard: migrate_image(image, ids, guard))
    if receipt_path is not None:
        if result.get('target_head') != 'agata0012':
            raise RuntimeError('verified migration target is not agata0012')
        receipt = dict(schema='gpoint-agata-applied-migration/v1', status='PASS', source_sha=sha,
                       head_before=proof['head'], head_after=result['target_head'],
                       migration_sha256=checksum, agata_image=image,
                       verified_at=datetime.now(timezone.utc).isoformat())
        payload = (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode()
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload); output.flush(); os.fsync(output.fileno())
        directory_fd = os.open(receipt_path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
        result = dict(result, receipt_path=str(receipt_path), receipt_sha256=hashlib.sha256(payload).hexdigest())
    return result


def complete_backup(sha, *, directory=None, purpose='pre-release'):
    # This container administrator channel creates only the GPoint logical
    # backup; application migration credentials use a separate protected file.
    if purpose not in ('pre-release', 'recovery-current'):
        raise RuntimeError('complete backup purpose is invalid')
    postgres_container = resolve_recovery_postgres_container() if purpose == 'recovery-current' else DB_CONTAINER
    census = authoritative_census(container=postgres_container)
    if census.get('database') != 'gpoint' or census.get('authoritative') is not True:
        raise RuntimeError('complete backup identity could not be established')
    directory = directory if directory is not None else Path('/opt/gpoint/backups'); directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / f'gpoint-{purpose}-{sha}-{time.time_ns()}.dump'
    try:
        schemas = ['public', 'agata']
        for schema in ('gsales', 'gsales_meta', 'agata_identity'):
            exists = docker_checked(psql_args(postgres_container), input=f"BEGIN READ ONLY; SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name='{schema}'); ROLLBACK;")
            if exists == 't': schemas.append(schema)
        schema_literals = ','.join("'" + schema + "'" for schema in schemas)
        extensions = docker_checked(psql_args(postgres_container), input="BEGIN READ ONLY; SELECT e.extname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE n.nspname IN (" + schema_literals + ") AND e.extname<>'plpgsql' ORDER BY e.extname; ROLLBACK;").splitlines()
        if any(not re.fullmatch('[a-z][a-z0-9_-]*', extension) for extension in extensions):
            raise RuntimeError('GPoint backup extension identity is invalid')
        with target.open('xb') as stream:
            target.chmod(0o600)
            dump_script = 'set -eu; set --; for schema in $GPOINT_BACKUP_SCHEMAS; do set -- "$@" "--schema=$schema"; done; for extension in $GPOINT_BACKUP_EXTENSIONS; do set -- "$@" "--extension=$extension"; done; exec pg_dump -U "$POSTGRES_USER" -d gpoint --format=custom --no-owner "$@"'
            result = subprocess.run(['docker', 'exec', '-e', 'GPOINT_BACKUP_SCHEMAS=' + ' '.join(schemas), '-e', 'GPOINT_BACKUP_EXTENSIONS=' + ' '.join(extensions), postgres_container, 'sh', '-ec', dump_script],
                stdout=stream, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode or not target.stat().st_size:
            raise RuntimeError('complete GPoint backup failed')
        with target.open('rb') as stream:
            result = subprocess.run(['docker', 'exec', '-i', postgres_container, 'pg_restore', '--list'],
                stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode or b'TABLE DATA agata messages' not in result.stdout:
            raise RuntimeError('GPoint backup archive verification failed')
        for extension in extensions:
            if not re.search(rb'\bEXTENSION - ' + re.escape(extension.encode()) + rb'(?:\s|$)', result.stdout):
                raise RuntimeError('GPoint backup extension archive verification failed')
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
        result = dict(path=str(target), sha256=digest.hexdigest(), database='gpoint',
                      schemas=schemas, extensions=extensions, status='COMPLETE_PROTECTED')
        if purpose == 'recovery-current':
            result.update(postgres_container_id=postgres_container,
                          postgres_container_name='/shared_postgres_1')
        return result
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['preflight', 'backup', 'migrate'])
    parser.add_argument('--sha', required=True)
    parser.add_argument('--image', help='immutable AGATA image for the selected source SHA')
    parser.add_argument('--record-receipt', action='store_true')
    parser.add_argument('--expected-migration-sha256')
    parser.add_argument('--backup-purpose', choices=['pre-release', 'recovery-current'], default='pre-release')
    parser.add_argument('--backup-directory', type=Path,
                        help='write the temporary plaintext archive in a caller-provided protected directory')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.sha): raise RuntimeError('exact SHA required')
    if args.record_receipt and args.mode != 'migrate':
        raise RuntimeError('migration receipt can only record an actual migrate operation')
    # Host configuration is consulted for source identity only; never echo values.
    env = {}
    for file in migration_env_files():
        env.update(line.split('=', 1) for line in Path(file).read_text().splitlines()
                   if line and not line.startswith('#') and '=' in line)
    for key in ['AGATA_MIGRATOR_DATABASE_URL' if env.get('AGATA_MIGRATOR_DATABASE_URL') else 'AGATA_DATABASE_URL', 'DATABASE_URL']:
        if urlsplit(env.get(key, '').replace('postgresql+asyncpg:', 'postgresql:').replace('postgresql+psycopg:', 'postgresql:')).path != '/gpoint':
            raise RuntimeError('production database scope could not be verified')
    if args.mode == 'preflight':
        if args.image:
            verify_migration_image(args.image, args.sha, args.expected_migration_sha256)
            image = args.image
        else:
            live_sha = env.get('RELEASE_SHA', '')
            if not re.fullmatch('[0-9a-f]{40}', live_sha): raise RuntimeError('current image identity unavailable')
            image = 'gpoint-agata-runtime:' + live_sha
        proof = validate_readiness(effective_probe(image), authoritative_census())
        print('AGATA_MIGRATION_READINESS=' + json.dumps(dict(public_facts(proof), candidate_sha=args.sha, checked_at=datetime.now(timezone.utc).isoformat(), inventory_source='verified_postgres_administrator_readonly'), sort_keys=True))
    elif args.mode == 'backup':
        print('GPOINT_COMPLETE_BACKUP=' + json.dumps(dict(complete_backup(
            args.sha, directory=args.backup_directory, purpose=args.backup_purpose), candidate_sha=args.sha), sort_keys=True))
    else:
        if args.image or args.record_receipt:
            result = migrate_candidate(args.image or '', args.sha, record_receipt=args.record_receipt,
                                       expected_migration_sha256=args.expected_migration_sha256)
        else:
            image = 'gpoint-agata-runtime:' + args.sha
            proof = effective_probe(image)
            result = guarded_migration(proof, TenantGuard(), lambda ids, guard: migrate_image(image, ids, guard))
        print('AGATA_MIGRATION_VERIFIED=' + json.dumps(dict(result, candidate_sha=args.sha, head_after=result['target_head'], checked_at=datetime.now(timezone.utc).isoformat(), inventory_source='administrator_share_locked_tenants'), sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Deliberately do not expose chained psycopg/process exceptions or URLs.
        print('AGATA_MIGRATION_BLOCKED=' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
