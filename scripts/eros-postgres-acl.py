#!/usr/bin/env python3
"""Narrow, fail-closed ACL hardening for the shared production PostgreSQL.

This program is intended to run only from the trusted GPoint Mac Deploy path.
It never accepts database names from the caller: the only mutation scope is
PUBLIC CONNECT on postgres and template1.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

SCHEMA = 'gpoint-eros-postgres-acl/v1'
TARGETS = ('postgres', 'template1')
PLAN_DIR = Path('/opt/gpoint/backups/eros-acl')
CONTAINER = 'shared_postgres_1'
POSTGRES_IMAGE = 'pgvector/pgvector:0.8.6-pg16-bookworm@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b'
WINDOW_SCOPE = 'ME-36 shared_postgres_1:REVOKE PUBLIC CONNECT on postgres,template1'
SHA = re.compile(r'^[a-f0-9]{40}$')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def normalized_acl(rows):
    """Normalize only database ACL grants; ACL NULL and default ACL compare semantically."""
    result = sorted(({'grantee': row['grantee'], 'privilege': row['privilege'],
                      'grantor': row['grantor'], 'grantable': bool(row['grantable'])}
                     for row in rows), key=lambda row: (row['grantee'], row['privilege'],
                                                        row['grantor'], row['grantable']))
    return result


def validate_targets(databases):
    if not isinstance(databases, list) or {item.get('name') for item in databases} != set(TARGETS) or len(databases) != 2:
        raise ValueError('database scope must contain postgres and template1 exactly once')
    for item in databases:
        if not isinstance(item.get('oid'), int) or item['oid'] <= 0 or not item.get('owner'):
            raise ValueError('target database identity is incomplete')
        if not isinstance(item.get('acl'), list):
            raise ValueError('target database ACL is invalid')


def remove_public_connect(acl):
    return [grant for grant in acl if not (grant['grantee'] == 'PUBLIC' and grant['privilege'] == 'CONNECT')]


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('schema') != SCHEMA:
        raise ValueError('unsupported ACL plan schema')
    if not SHA.fullmatch(plan.get('candidateSha', '')) or not SHA.fullmatch(plan.get('productionSha', '')):
        raise ValueError('exact candidate and production SHAs are required')
    if not isinstance(plan.get('clusterSystemIdentifier'), str) or not plan['clusterSystemIdentifier'].isdigit():
        raise ValueError('PostgreSQL cluster identity is missing')
    validate_targets(plan.get('databases'))
    if plan.get('container') != CONTAINER:
        raise ValueError('unexpected PostgreSQL container identity')
    if not re.fullmatch(r'[a-f0-9]{64}', plan.get('containerId', '')) or not re.fullmatch(r'sha256:[a-f0-9]{64}', plan.get('containerImageId', '')):
        raise ValueError('PostgreSQL runtime container identity is incomplete')
    if plan.get('containerImage') != POSTGRES_IMAGE or plan.get('serverVersionNum', 0) < 160000 or plan.get('serverVersionNum', 0) >= 170000:
        raise ValueError('PostgreSQL runtime image or major version differs from the pinned production image')
    if plan.get('maintenanceInventory', {}).get('ownerReviewed') is not True:
        raise ValueError('owner-reviewed maintenance-client inventory is required')
    window = plan.get('maintenanceWindow', {})
    if (set(window) != {'ownerConfirmed', 'barrierId', 'evidence', 'startsAt', 'expiresAt', 'scope'}
            or window.get('ownerConfirmed') is not True
            or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', window.get('barrierId', ''))
            or window.get('scope') != WINDOW_SCOPE):
        raise ValueError('owner-confirmed exclusive ACL/DDL window evidence is required')
    if not isinstance(window.get('evidence'), str) or not window['evidence'].startswith('https://'):
        raise ValueError('maintenance window must link to its owner-reviewed coordination evidence')
    if window != plan['maintenanceInventory'].get('maintenanceWindow'):
        raise ValueError('maintenance window inventory and plan evidence disagree')
    if not re.fullmatch(r'[a-f0-9]{64}', plan.get('maintenanceInventorySha256', '')):
        raise ValueError('maintenance inventory identity is missing')
    if plan['maintenanceInventory'].get('unknownClients') != []:
        raise ValueError('unknown maintenance clients block ACL hardening')
    if not isinstance(plan['maintenanceInventory'].get('knownClients'), list):
        raise ValueError('maintenance client inventory is invalid')
    acl_by_db = {database['name']: database['acl'] for database in plan['databases']}
    owner_by_db = {database['name']: database['owner'] for database in plan['databases']}
    if not isinstance(plan.get('maintenanceRoleState'), list):
        raise ValueError('maintenance role state is missing')
    role_names = {client['role'] for client in plan['maintenanceInventory']['knownClients']}
    if not role_names.issubset({role.get('name') for role in plan['maintenanceRoleState']}):
        raise ValueError('maintenance role attributes and memberships are incomplete')
    for client in plan['maintenanceInventory']['knownClients']:
        if (not isinstance(client, dict) or client.get('database') not in TARGETS
                or client.get('access') not in ('owner', 'explicit-connect')
                or client.get('ownerApproved') is not True or not client.get('role')
                or (client.get('access') == 'explicit-connect' and not isinstance(client.get('application'), str))):
            raise ValueError('every maintenance client must have an owner-approved surviving access path')
        if client['access'] == 'owner' and client['role'] != owner_by_db[client['database']]:
            raise ValueError('owner client identity does not match database owner')
        if client['access'] == 'explicit-connect' and not any(
                grant['grantee'] == client['role'] and grant['privilege'] == 'CONNECT'
                for grant in acl_by_db[client['database']]):
            raise ValueError('explicit maintenance CONNECT grant is absent from the saved ACL')
    for database in plan['databases']:
        expected = remove_public_connect(database['acl'])
        if normalized_acl(database['afterAcl']) != normalized_acl(expected):
            raise ValueError('planned ACL delta exceeds PUBLIC CONNECT removal')
        if not any(g['grantee'] == 'PUBLIC' and g['privilege'] == 'CONNECT' for g in database['acl']):
            raise ValueError('expected PUBLIC CONNECT is absent from baseline')


def plan_digest(plan):
    body = {key: value for key, value in plan.items() if key != 'planSha256'}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def _assert_window_active(window, minimum_remaining_seconds=0):
    try:
        starts = datetime.fromisoformat(window['startsAt'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(window['expiresAt'].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('exclusive maintenance window timestamps are invalid') from None
    if starts.tzinfo is None or expires.tzinfo is None or starts >= expires:
        raise RuntimeError('exclusive maintenance window timestamps are invalid')
    if now < starts or (expires - now).total_seconds() < minimum_remaining_seconds:
        raise RuntimeError('exclusive maintenance window is not active for the required interval')


def _verify_inventory(plan, minimum_remaining_seconds=120):
    path = Path('/opt/gpoint/backups/eros-acl/maintenance-clients.json')
    try:
        info = path.stat()
        raw = path.read_bytes()
    except OSError:
        raise RuntimeError('owner-reviewed maintenance inventory is unavailable') from None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError('owner-reviewed maintenance inventory permissions are unsafe')
    if hashlib.sha256(raw).hexdigest() != plan['maintenanceInventorySha256']:
        raise RuntimeError('owner-reviewed maintenance inventory changed after planning')
    inventory = json.loads(raw)
    _assert_window_active(inventory.get('maintenanceWindow', {}), minimum_remaining_seconds=minimum_remaining_seconds)
    return inventory


def _psql(sql, database='postgres'):
    # SQL and identifiers are constants in this file. No output from psql errors
    # is forwarded because drivers can echo SQL or environment details.
    result = subprocess.run(
        ['docker', 'exec', '-i', '-e', 'PGCONNECT_TIMEOUT=5', CONTAINER, 'sh', '-ec',
         'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d ' + database],
        input=sql, text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError('PostgreSQL ACL operation failed; inspect exact state read-only before retry')
    return result.stdout.strip()


SNAPSHOT_SQL = r"""SELECT json_build_object(
 'clusterSystemIdentifier', (SELECT system_identifier::text FROM pg_control_system()),
 'serverVersionNum', current_setting('server_version_num')::integer,
 'publicConnectElsewhereCount', (SELECT count(*) FROM pg_database d WHERE d.datallowconn
    AND d.datname NOT IN ('postgres','template1') AND EXISTS
    (SELECT 1 FROM aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) x WHERE x.grantee=0 AND x.privilege_type='CONNECT')),
 'databases', (SELECT json_agg(json_build_object(
   'name', d.datname, 'oid', d.oid, 'owner', pg_get_userbyid(d.datdba),
   'acl', (SELECT coalesce(json_agg(json_build_object('grantee', CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
      'privilege', x.privilege_type, 'grantor', pg_get_userbyid(x.grantor), 'grantable', x.is_grantable)
      ORDER BY x.grantee,x.privilege_type,x.grantor,x.is_grantable), '[]'::json)
      FROM aclexplode(coalesce(d.datacl, acldefault('d',d.datdba))) x),
   'allowConnect', d.datallowconn) ORDER BY d.datname)
   FROM pg_database d WHERE d.datname IN ('postgres','template1')),
 'roles', (SELECT json_agg(json_build_object('name',rolname,'login',rolcanlogin,'superuser',rolsuper,
   'createDatabase',rolcreatedb,'createRole',rolcreaterole,'bypassRls',rolbypassrls) ORDER BY rolname)
   FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')),
 'erosDatabaseExists', EXISTS(SELECT 1 FROM pg_database WHERE datname='eros'),
 'connections', (SELECT json_agg(json_build_object('database',datname,'role',usename,'application',coalesce(application_name,''),'count',n)
   ORDER BY datname,usename,application_name) FROM (SELECT datname,usename,application_name,count(*) n
   FROM pg_stat_activity WHERE backend_type='client backend' AND datname IN ('postgres','template1')
   GROUP BY datname,usename,application_name) s)
)::text"""


def snapshot():
    raw = _psql(SNAPSHOT_SQL)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError('PostgreSQL ACL snapshot is malformed') from None
    validate_targets(value.get('databases'))
    return value


def maintenance_role_state(role_names):
    names = sorted(set(role_names))
    if not names:
        return []
    if any(not isinstance(name, str) or not name or len(name) > 63 for name in names):
        raise ValueError('maintenance role name is invalid')
    names_sql = ','.join(_sql_literal(name) for name in names)
    query = f"""SELECT coalesce(json_agg(json_build_object(
      'name',r.rolname,'login',r.rolcanlogin,'superuser',r.rolsuper,
      'createDatabase',r.rolcreatedb,'createRole',r.rolcreaterole,'inherit',r.rolinherit,
      'bypassRls',r.rolbypassrls,'memberships',(SELECT coalesce(json_agg(pg_get_userbyid(m.roleid) ORDER BY pg_get_userbyid(m.roleid)),'[]'::json)
        FROM pg_auth_members m WHERE m.member=r.oid)) ORDER BY r.rolname),'[]'::json)::text
      FROM pg_roles r WHERE r.rolname IN ({names_sql})"""
    try:
        result = json.loads(_psql(query))
    except (TypeError, ValueError):
        raise RuntimeError('maintenance role state is unrecognized') from None
    if {role.get('name') for role in result} != set(names):
        raise RuntimeError('a maintenance client role is missing')
    return result


def _verify_live_connections(connections, plan):
    owners = {database['name']: database['owner'] for database in plan['databases']}
    clients = plan['maintenanceInventory']['knownClients']
    for connection in connections or []:
        if connection['role'] == owners[connection['database']]:
            continue
        if not any(client['database'] == connection['database'] and client['role'] == connection['role']
                   and client['application'] == connection['application']
                   and client['access'] == 'explicit-connect' for client in clients):
            raise RuntimeError('an active maintenance connection is not covered by owner-reviewed inventory')


def container_identity():
    try:
        result = subprocess.run(['docker', 'inspect', '--format',
            '{{.Id}}|{{.Config.Image}}|{{.Image}}', CONTAINER], text=True,
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('PostgreSQL container identity could not be read') from None
    if result.returncode:
        raise RuntimeError('PostgreSQL container identity could not be read')
    values = result.stdout.strip().split('|')
    if (len(values) != 3 or not re.fullmatch(r'[a-f0-9]{64}', values[0])
            or values[1] != POSTGRES_IMAGE or not re.fullmatch(r'sha256:[a-f0-9]{64}', values[2])):
        raise RuntimeError('PostgreSQL container image differs from the approved pinned production image')
    return {'containerId': values[0], 'containerImage': values[1], 'containerImageId': values[2]}


def secure_write(path, data):
    path = Path(path)
    if path.parent != PLAN_DIR or path.parent.is_symlink() or path.name != 'me36-plan.json':
        raise RuntimeError('ACL evidence directory cannot be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != 0 or path.parent.stat().st_mode & 0o077:
        raise RuntimeError('ACL evidence directory must be root-owned and private')
    if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.stat().st_mode)
                          or path.stat().st_uid != 0 or path.stat().st_mode & 0o077):
        raise RuntimeError('existing ACL evidence file is unsafe')
    if path.exists():
        raise RuntimeError('an ACL plan already exists; inspect it read-only instead of replacing its preimage')
    fd, staged = tempfile.mkstemp(prefix='.acl.', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(canonical(data) + '\n')
            output.flush()
            os.fsync(output.fileno())
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


def load_plan(path):
    path = Path(path)
    if path.parent != PLAN_DIR or path.name != 'me36-plan.json' or path.parent.is_symlink():
        raise RuntimeError('ACL plan path is outside the protected evidence location')
    info = path.stat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError('ACL plan must be a root-owned mode-0600 regular file')
    plan = json.loads(path.read_text())
    validate_plan(plan)
    if plan.get('planSha256') != plan_digest(plan):
        raise RuntimeError('ACL plan checksum mismatch')
    return plan


def write_plan(candidate, production, inventory_path, output):
    if not SHA.fullmatch(candidate or '') or not SHA.fullmatch(production or ''):
        raise ValueError('exact candidate and production SHAs are required')
    inventory_file = Path(inventory_path)
    inventory_info = inventory_file.stat()
    if inventory_file.is_symlink() or not stat.S_ISREG(inventory_info.st_mode) or inventory_info.st_uid != 0 or inventory_info.st_mode & 0o077:
        raise RuntimeError('maintenance inventory must be a private regular file')
    inventory_bytes = inventory_file.read_bytes()
    inventory = json.loads(inventory_bytes)
    if set(inventory) != {'ownerReviewed', 'knownClients', 'unknownClients', 'maintenanceWindow'}:
        raise ValueError('maintenance inventory schema is invalid')
    if not isinstance(inventory['knownClients'], list) or not isinstance(inventory['unknownClients'], list):
        raise ValueError('maintenance client inventory is invalid')
    if inventory['unknownClients'] or inventory['ownerReviewed'] is not True:
        raise ValueError('unknown or unreviewed maintenance clients block ACL planning')
    if inventory.get('maintenanceWindow', {}).get('ownerConfirmed') is not True:
        raise ValueError('owner-confirmed exclusive ACL/DDL window evidence is required')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', inventory['maintenanceWindow'].get('barrierId', '')):
        raise ValueError('maintenance window barrier identity is invalid')
    if not inventory['maintenanceWindow'].get('evidence'):
        raise ValueError('maintenance window evidence reference is required')
    _assert_window_active(inventory['maintenanceWindow'], minimum_remaining_seconds=120)
    for client in inventory['knownClients']:
        if (not isinstance(client, dict) or client.get('database') not in TARGETS
                or client.get('access') not in ('owner', 'explicit-connect')
                or client.get('ownerApproved') is not True):
            raise ValueError('every maintenance client must have an owner-approved surviving access path')
    state = snapshot()
    runtime = container_identity()
    if state.get('serverVersionNum', 0) < 160000 or state.get('serverVersionNum', 0) >= 170000:
        raise RuntimeError('PostgreSQL server major version differs from the pinned PostgreSQL 16 rehearsal')
    if state.get('publicConnectElsewhereCount') != 0:
        raise RuntimeError('PUBLIC CONNECT exists on an out-of-scope database')
    if state['erosDatabaseExists'] or state['roles']:
        raise RuntimeError('Eros state already exists; initial ACL plan is inapplicable')
    by_name = {item['name']: item for item in state['databases']}
    for connection in state['connections'] or []:
        if connection['role'] == by_name[connection['database']]['owner']:
            continue
        if not any(client['database'] == connection['database'] and client['role'] == connection['role']
                   and client.get('application') == connection['application']
                   for client in inventory['knownClients']):
            raise RuntimeError('an active maintenance client is not in the owner-reviewed inventory')
    for name in TARGETS:
        if not by_name[name]['allowConnect']:
            raise RuntimeError('target maintenance database rejects connections unexpectedly')
    dbs = []
    for name in TARGETS:
        item = by_name[name]
        acl = normalized_acl(item['acl'])
        dbs.append({**item, 'acl': acl, 'afterAcl': remove_public_connect(acl)})
    plan = {'schema': SCHEMA, 'candidateSha': candidate, 'productionSha': production,
            'container': CONTAINER, **runtime, 'serverVersionNum': state['serverVersionNum'],
            'clusterSystemIdentifier': state['clusterSystemIdentifier'], 'databases': dbs,
            'maintenanceRoleState': maintenance_role_state(client['role'] for client in inventory['knownClients']),
            'maintenanceInventorySha256': hashlib.sha256(inventory_bytes).hexdigest(),
            'maintenanceInventory': inventory,
            'maintenanceWindow': inventory['maintenanceWindow'],
            'connectionsAtPlan': state['connections'] or [],
            'capturedAt': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    validate_plan(plan)
    _verify_live_connections(state['connections'], plan)
    plan['planSha256'] = plan_digest(plan)
    secure_write(output, plan)
    print(json.dumps({'status': 'PLAN_READY', 'planSha256': plan['planSha256'],
                      'candidateSha': candidate, 'targetDatabases': list(TARGETS),
                      'connectionRows': len(plan['connectionsAtPlan'])}))


def verify_live(plan, after=False, minimum_window_seconds=120):
    _verify_inventory(plan, minimum_remaining_seconds=minimum_window_seconds)
    current = snapshot()
    if current['serverVersionNum'] != plan['serverVersionNum'] or container_identity() != {
            'containerId': plan['containerId'], 'containerImage': plan['containerImage'],
            'containerImageId': plan['containerImageId']}:
        raise RuntimeError('PostgreSQL runtime container identity changed')
    if current['clusterSystemIdentifier'] != plan['clusterSystemIdentifier']:
        raise RuntimeError('PostgreSQL cluster identity changed')
    if current['erosDatabaseExists'] or current['roles']:
        raise RuntimeError('Eros role or database state changed')
    if current.get('publicConnectElsewhereCount') != 0:
        raise RuntimeError('PUBLIC CONNECT appeared on an out-of-scope database')
    _verify_live_connections(current['connections'], plan)
    current_roles = maintenance_role_state(client['role'] for client in plan['maintenanceInventory']['knownClients'])
    if current_roles != plan['maintenanceRoleState']:
        raise RuntimeError('maintenance role attributes or memberships changed after planning')
    expected = {db['name']: db['afterAcl' if after else 'acl'] for db in plan['databases']}
    live = {db['name']: db for db in current['databases']}
    for name in TARGETS:
        baseline = next(db for db in plan['databases'] if db['name'] == name)
        item = live[name]
        if item['oid'] != baseline['oid'] or item['owner'] != baseline['owner']:
            raise RuntimeError('target database OID or owner changed')
        if normalized_acl(item['acl']) != normalized_acl(expected[name]):
            raise RuntimeError('target database ACL drifted from reviewed plan')
    return current


def authorize_apply(candidate, owner_sha):
    if not SHA.fullmatch(candidate or '') or candidate != owner_sha:
        raise RuntimeError('Apply requires matching exact candidate and OWNER-approved SHA')
    if os.environ.get('EROS_ACL_APPLY_APPROVED') != 'true':
        raise RuntimeError('explicit Eros ACL Apply approval is required')
    owner = os.environ.get('GITHUB_REPOSITORY_OWNER', '')
    if not owner or os.environ.get('GITHUB_ACTOR') != owner or os.environ.get('GITHUB_TRIGGERING_ACTOR') != owner:
        raise RuntimeError('Eros ACL Apply requires a repository-owner dispatch')
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('Eros ACL Apply must run from the trusted main Deploy workflow')


def _acl_guard_sql(plan, expected_key):
    expected = {db['name']: db for db in plan['databases']}
    assertions = []
    for name in TARGETS:
        db = expected[name]
        owner = db['owner'].replace("'", "''")
        acl_json = canonical(db[expected_key]).replace("'", "''")
        assertions.append(f"""IF (SELECT oid FROM pg_database WHERE datname='{name}') IS DISTINCT FROM {db['oid']}
 OR (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{name}') IS DISTINCT FROM '{owner}'
 OR NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='{name}' AND datallowconn)
 OR (SELECT coalesce(jsonb_agg(jsonb_build_object(
      'grantee',CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
      'privilege',x.privilege_type,'grantor',pg_get_userbyid(x.grantor),'grantable',x.is_grantable)
      ORDER BY CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
       x.privilege_type,pg_get_userbyid(x.grantor),x.is_grantable),'[]'::jsonb)
     FROM aclexplode(coalesce((SELECT datacl FROM pg_database WHERE datname='{name}'),
       acldefault('d',(SELECT datdba FROM pg_database WHERE datname='{name}')))) x)
    IS DISTINCT FROM '{acl_json}'::jsonb THEN
 RAISE EXCEPTION 'ACL baseline drift on {name}'; END IF;""")
    return '\n'.join(assertions)


def _sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _connection_guard_sql(plan):
    allowed = []
    for client in plan['maintenanceInventory']['knownClients']:
        if client['access'] != 'explicit-connect':
            continue
        allowed.append('(d.datname=' + _sql_literal(client['database'])
                       + ' AND a.usename=' + _sql_literal(client['role'])
                       + ' AND coalesce(a.application_name,\'\')=' + _sql_literal(client['application']) + ')')
    allowed_sql = ' OR '.join(allowed) if allowed else 'FALSE'
    return f"""DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_database d ON d.oid=a.datid
   WHERE d.datname IN ('postgres','template1') AND a.usesysid <> d.datdba
   AND NOT ({allowed_sql})) THEN
   RAISE EXCEPTION 'unreviewed maintenance connection is active';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_database d ON d.oid=a.datid
   WHERE d.datname IN ('postgres','template1') AND a.usesysid <> d.datdba
   AND NOT has_database_privilege(a.usesysid,d.oid,'CONNECT')) THEN
   RAISE EXCEPTION 'active maintenance connection relies on PUBLIC CONNECT';
 END IF;
END $$;"""


def build_apply_sql(plan):
    return f"""BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '10s';
DO $$ BEGIN
 IF (SELECT system_identifier::text FROM pg_control_system()) IS DISTINCT FROM '{plan['clusterSystemIdentifier']}' THEN
   RAISE EXCEPTION 'PostgreSQL cluster identity changed';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_database WHERE datname='eros') OR
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')) THEN
   RAISE EXCEPTION 'Eros state appeared after planning';
 END IF;
 {_acl_guard_sql(plan, 'acl')}
END $$;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC RESTRICT;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC RESTRICT;
{_connection_guard_sql(plan)}
COMMIT;"""


def build_rollback_sql(plan):
    grants = '\n'.join(f'GRANT CONNECT ON DATABASE {name} TO PUBLIC;' for name in TARGETS)
    return f"""BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '10s';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_database WHERE datname='eros') OR
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator')) THEN
   RAISE EXCEPTION 'Eros state exists; PUBLIC CONNECT rollback is forbidden';
 END IF;
 {_acl_guard_sql(plan, 'afterAcl')}
END $$;
{grants}
COMMIT;"""


def apply(plan_path, candidate, owner_sha):
    authorize_apply(candidate, owner_sha)
    plan = load_plan(plan_path)
    if plan['candidateSha'] != candidate:
        raise RuntimeError('ACL plan candidate SHA changed')
    verify_live(plan)
    # One bounded transaction; never retry an ambiguous transport result.
    sql = build_apply_sql(plan)
    _psql(sql)
    verify_live(plan, after=True, minimum_window_seconds=0)
    _verify_inventory(plan, minimum_remaining_seconds=0)
    print(json.dumps({'status': 'ACL_APPLIED', 'planSha256': plan['planSha256'],
                      'candidateSha': candidate, 'databases': list(TARGETS)}))


def rollback(plan_path, candidate, owner_sha):
    authorize_apply(candidate, owner_sha)
    plan = load_plan(plan_path)
    if plan['candidateSha'] != candidate:
        raise RuntimeError('ACL plan candidate SHA changed')
    verify_live(plan, after=True)
    _psql(build_rollback_sql(plan))
    verify_live(plan, minimum_window_seconds=0)
    _verify_inventory(plan, minimum_remaining_seconds=0)
    print(json.dumps({'status': 'ACL_ROLLED_BACK', 'planSha256': plan['planSha256'],
                      'candidateSha': candidate, 'databases': list(TARGETS)}))


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--candidate', required=True); p.add_argument('--production', required=True)
    p.add_argument('--maintenance-inventory', required=True)
    p.add_argument('--output', default=str(PLAN_DIR / 'me36-plan.json'))
    i = sub.add_parser('inspect'); i.add_argument('--plan', required=True)
    a = sub.add_parser('apply')
    a.add_argument('--plan', required=True); a.add_argument('--candidate', required=True); a.add_argument('--owner-approved-sha', required=True)
    r = sub.add_parser('rollback')
    r.add_argument('--plan', required=True); r.add_argument('--candidate', required=True); r.add_argument('--owner-approved-sha', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan': write_plan(args.candidate, args.production, args.maintenance_inventory, args.output)
        elif args.command == 'inspect':
            plan = load_plan(args.plan); live = verify_live(plan)
            print(json.dumps({'status': 'BASELINE_MATCH', 'planSha256': plan['planSha256'],
                              'candidateSha': plan['candidateSha'], 'connectionRows': len(live['connections'] or [])}))
        elif args.command == 'apply': apply(args.plan, args.candidate, args.owner_approved_sha)
        else: rollback(args.plan, args.candidate, args.owner_approved_sha)
    except Exception as exc:
        print(f'BLOCKED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
