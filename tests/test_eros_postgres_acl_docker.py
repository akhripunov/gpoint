"""ACL semantics and rollback on the same pinned PostgreSQL 16 image as production."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from uuid import uuid4
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'pgvector/pgvector:0.8.6-pg16-bookworm@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b'
SPEC = importlib.util.spec_from_file_location('eros_postgres_acl', ROOT / 'scripts/eros-postgres-acl.py')
ACL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACL)


@unittest.skipUnless(os.environ.get('GPOINT_RUN_EROS_ACL_PG16') == '1',
                     'set GPOINT_RUN_EROS_ACL_PG16=1 to run pinned disposable PostgreSQL 16')
class ErosPostgresAclDockerTests(unittest.TestCase):
    def test_hardening_access_preservation_and_preprovisioning_rollback(self):
        suffix = uuid4().hex[:12]
        container = 'gpoint-eros-acl-pg-' + suffix

        def docker(*args, input=None, check=True):
            result = subprocess.run(['docker', *args], input=input, text=True,
                                    capture_output=True, timeout=180)
            if check and result.returncode:
                self.fail('disposable PostgreSQL command failed: ' + result.stderr[-1200:])
            return result

        def sql(statement, *, db='postgres', check=True):
            return docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres',
                          '-d', db, '-v', 'ON_ERROR_STOP=1', input=statement, check=check)

        try:
            docker('run', '-d', '--name', container, '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', IMAGE)
            for _ in range(60):
                ready = docker('exec', container, 'pg_isready', '-U', 'postgres', check=False)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                self.fail('pinned disposable PostgreSQL 16 did not become ready')

            sql("CREATE ROLE maintenance_owner LOGIN; CREATE ROLE public_only LOGIN; CREATE ROLE eros_runtime LOGIN BYPASSRLS;")
            sql('CREATE DATABASE appdb;')
            sql('REVOKE CONNECT ON DATABASE appdb FROM PUBLIC; GRANT CONNECT ON DATABASE appdb TO maintenance_owner;')
            # Reproduce the defect for a future BYPASSRLS login before change.
            for database in ('postgres', 'template1'):
                result = docker('exec', container, 'psql', '-XqAt', '-U', 'eros_runtime', '-d', database,
                                '-v', 'ON_ERROR_STOP=1', '-c', 'SELECT 1', check=False)
                self.assertEqual(result.returncode, 0, database)
            result = docker('exec', container, 'psql', '-XqAt', '-U', 'eros_runtime', '-d', 'appdb',
                            '-v', 'ON_ERROR_STOP=1', '-c', 'SELECT 1', check=False)
            self.assertNotEqual(result.returncode, 0)
            sql('DROP ROLE eros_runtime;')

            sql('CREATE ROLE gsales_runtime LOGIN; GRANT CONNECT ON DATABASE appdb TO gsales_runtime;')
            before_app_acl = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-c',
                                    "SELECT coalesce(datacl::text,'NULL') FROM pg_database WHERE datname='appdb'").stdout.strip()
            def pg_psql(statement, database=ACL.CONTROL_DATABASE):
                if database == ACL.CONTROL_DATABASE:
                    database = 'appdb'  # disposable stand-in for production CONTROL_DATABASE='gpoint'
                return docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', database,
                              '-v', 'ON_ERROR_STOP=1', input=statement).stdout.strip()
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                state = ACL.snapshot()
            self.assertEqual(state['publicConnectElsewhereCount'], 0)
            self.assertFalse(state['erosDatabaseExists'])
            self.assertFalse(state['roles'])
            before_owner_acl = {db['name']: db['acl'] for db in state['databases']}
            maintenance = [{'role': 'maintenance_owner', 'database': 'postgres', 'application': 'acl-explicit-test',
                            'access': 'explicit-connect', 'ownerApproved': True}]
            # PostgreSQL owner has a separate direct path in the maintenance fixture.
            if not any(g['grantee'] == 'maintenance_owner' and g['privilege'] == 'CONNECT'
                       for g in before_owner_acl['postgres']):
                sql('GRANT CONNECT ON DATABASE postgres TO maintenance_owner;')
                with patch.object(ACL, '_psql', side_effect=pg_psql): state = ACL.snapshot()
                before_owner_acl = {db['name']: db['acl'] for db in state['databases']}
            now = datetime.now(timezone.utc)
            window = {'ownerConfirmed': True, 'barrierId': 'test-barrier-01',
                      'evidence': 'https://linear.app/bestuzhev/issue/ME-36',
                      'scope': ACL.WINDOW_SCOPE,
                      'startsAt': (now - timedelta(minutes=1)).isoformat(),
                      'expiresAt': (now + timedelta(minutes=10)).isoformat()}
            identity = docker('inspect', '--format', '{{.Id}}|{{.Config.Image}}|{{.Image}}', container).stdout.strip().split('|')
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                maintenance_state = ACL.maintenance_role_state(['maintenance_owner'])
            plan = {'schema': ACL.SCHEMA, 'candidateSha': 'a' * 40, 'productionSha': 'b' * 40,
                    'container': ACL.CONTAINER, 'clusterSystemIdentifier': state['clusterSystemIdentifier'],
                    'containerId': identity[0], 'containerImage': identity[1], 'containerImageId': identity[2],
                    'serverVersionNum': state['serverVersionNum'], 'maintenanceInventorySha256': 'e' * 64,
                    'maintenanceRoleState': maintenance_state,
                    'databases': [], 'maintenanceWindow': window,
                    'maintenanceInventory': {'ownerReviewed': True, 'knownClients': maintenance,
                        'unknownClients': [], 'maintenanceWindow': window}}
            for db in state['databases']:
                normalized = ACL.normalized_acl(db['acl'])
                plan['databases'].append({**db, 'acl': normalized,
                                          'afterAcl': ACL.remove_public_connect(normalized)})
            ACL.validate_plan(plan)

            # The live catalog relation locks serialize ACL, database and role
            # administration. A concurrent GRANT must hit its bounded timeout.
            sql('CREATE ROLE catalog_waiter LOGIN;')
            lock_sql = "BEGIN; LOCK TABLE pg_catalog.pg_database IN SHARE MODE; LOCK TABLE pg_catalog.pg_authid IN SHARE MODE; LOCK TABLE pg_catalog.pg_auth_members IN SHARE MODE; SELECT 'CATALOG_LOCKS_HELD'; SELECT pg_sleep(2); COMMIT;"
            lock_holder = subprocess.Popen(['docker', 'exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres',
                '-d', 'appdb', '-v', 'ON_ERROR_STOP=1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            lock_holder.stdin.write(lock_sql); lock_holder.stdin.close()
            self.assertEqual(lock_holder.stdout.readline().strip(), 'CATALOG_LOCKS_HELD')
            racing_grant = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', '-c', "SET lock_timeout='200ms'; GRANT CONNECT ON DATABASE appdb TO catalog_waiter;",
                check=False)
            self.assertNotEqual(racing_grant.returncode, 0)
            self.assertIn('lock timeout', racing_grant.stderr.lower())
            racing_role = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', '-c', "SET lock_timeout='200ms'; ALTER ROLE catalog_waiter NOLOGIN;",
                check=False)
            self.assertNotEqual(racing_role.returncode, 0)
            self.assertIn('lock timeout', racing_role.stderr.lower())
            racing_membership = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', '-c', "SET lock_timeout='200ms'; GRANT catalog_waiter TO maintenance_owner;",
                check=False)
            self.assertNotEqual(racing_membership.returncode, 0)
            self.assertIn('lock timeout', racing_membership.stderr.lower())
            self.assertEqual(lock_holder.wait(timeout=10), 0, lock_holder.stderr.read())
            sql('DROP ROLE catalog_waiter;')

            # Runtime revalidation catches a maintenance-role change after the
            # saved plan; the transaction must not close connections or revoke ACLs.
            sql('ALTER ROLE maintenance_owner NOINHERIT;')
            role_drift = docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', input=ACL.build_apply_sql(plan), check=False)
            self.assertNotEqual(role_drift.returncode, 0)
            self.assertIn('maintenance role attributes or memberships drifted', role_drift.stderr)
            still_public = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-c',
                "SELECT EXISTS (SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) x WHERE d.datname='postgres' AND x.grantee=0 AND x.privilege_type='CONNECT')").stdout.strip()
            self.assertEqual(still_public, 't')
            sql('ALTER ROLE maintenance_owner INHERIT;')

            client = subprocess.Popen(['docker', 'exec', '-e', 'PGAPPNAME=acl-public-only-test', container,
                'psql', '-U', 'public_only', '-d', 'postgres', '-c', 'SELECT pg_sleep(60)'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(1)
            self.assertIsNone(client.poll(), 'public-only dependency session did not stay connected')
            blocked = docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'postgres',
                             '-v', 'ON_ERROR_STOP=1', input=ACL.build_apply_sql(plan), check=False)
            self.assertNotEqual(blocked.returncode, 0, 'Apply must block while a public-only client is connected')
            still_public = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-c',
                "SELECT EXISTS (SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) x WHERE d.datname='postgres' AND x.grantee=0 AND x.privilege_type='CONNECT')").stdout.strip()
            self.assertEqual(still_public, 't')
            sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='acl-public-only-test';")
            client.wait(timeout=10)
            sql('DROP ROLE public_only;')
            approved_client = subprocess.Popen(['docker', 'exec', '-e', 'PGAPPNAME=acl-explicit-test', container,
                'psql', '-U', 'maintenance_owner', '-d', 'postgres', '-c', 'SELECT pg_sleep(60)'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(1)
            self.assertIsNone(approved_client.poll(), 'approved explicit-connect client did not stay connected')
            close_phase, reopen_phase = ACL.build_apply_phases(plan)
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                ACL._psql(close_phase, ACL.CONTROL_DATABASE)
            for database in ('postgres', 'template1'):
                denied = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', database,
                                '-c', 'SELECT 1', check=False)
                self.assertNotEqual(denied.returncode, 0, 'new connection passed the transaction-fenced admission gate')
            self.assertIsNone(approved_client.poll(), 'an established approved session was terminated by the fence')
            self.assertEqual('1', docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                                         '-c', 'SELECT 1').stdout.strip())
            app_allow = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                               '-c', "SELECT datallowconn FROM pg_database WHERE datname='appdb'").stdout.strip()
            self.assertEqual(app_allow, 't')
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                fenced_snapshot = ACL.snapshot()
            fenced = {db['name']: db for db in fenced_snapshot['databases']}
            for database in ('postgres', 'template1'):
                self.assertFalse(fenced[database]['allowConnect'], database)
                self.assertEqual(ACL.normalized_acl(fenced[database]['acl']),
                                 ACL.normalized_acl(next(db['afterAcl'] for db in plan['databases']
                                                         if db['name'] == database)), database)
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                ACL._psql(reopen_phase, ACL.CONTROL_DATABASE)
                after = ACL.snapshot()
            approved_client.terminate(); approved_client.wait(timeout=10)
            by_name = {db['name']: db for db in after['databases']}
            for database in ('postgres', 'template1'):
                self.assertFalse(any(g['grantee'] == 'PUBLIC' and g['privilege'] == 'CONNECT'
                                     for g in by_name[database]['acl']))
                self.assertTrue(docker('exec', container, 'psql', '-XqAt', '-U', 'postgres',
                                       '-d', database, '-c', 'SELECT 1').stdout.strip() == '1')
            sql('CREATE ROLE eros_runtime LOGIN BYPASSRLS;')
            self.assertTrue(docker('exec', container, 'psql', '-XqAt', '-U', 'maintenance_owner',
                                   '-d', 'postgres', '-c', 'SELECT 1').stdout.strip() == '1')
            for database in ('postgres', 'template1'):
                denied = docker('exec', container, 'psql', '-XqAt', '-U', 'eros_runtime', '-d', database,
                                '-c', 'SELECT 1', check=False)
                self.assertNotEqual(denied.returncode, 0, database)
            # A fenced recovery refuses an Eros role with effective CONNECT;
            # once that grant is removed, the exact ACL can reopen both targets.
            sql('ALTER DATABASE postgres WITH ALLOW_CONNECTIONS false; ALTER DATABASE template1 WITH ALLOW_CONNECTIONS false;', db='appdb')
            sql('GRANT maintenance_owner TO eros_runtime;', db='appdb')
            unsafe_recovery = docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', input=ACL.build_admission_recovery_sql(plan), check=False)
            self.assertNotEqual(unsafe_recovery.returncode, 0)
            self.assertIn('effective CONNECT', unsafe_recovery.stderr)
            closed = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                            '-c', "SELECT bool_and(NOT datallowconn) FROM pg_database WHERE datname IN ('postgres','template1')").stdout.strip()
            self.assertEqual(closed, 't')
            sql('REVOKE maintenance_owner FROM eros_runtime;', db='appdb')
            sql('ALTER DATABASE template1 WITH ALLOW_CONNECTIONS true;', db='appdb')
            asymmetric_recovery = docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-v', 'ON_ERROR_STOP=1', input=ACL.build_admission_recovery_sql(plan), check=False)
            self.assertNotEqual(asymmetric_recovery.returncode, 0)
            self.assertIn('both planned targets to remain fenced', asymmetric_recovery.stderr)
            still_fenced = docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-d', 'appdb',
                '-c', "SELECT NOT (SELECT datallowconn FROM pg_database WHERE datname='postgres') AND (SELECT datallowconn FROM pg_database WHERE datname='template1')").stdout.strip()
            self.assertEqual(still_fenced, 't')
            sql('ALTER DATABASE template1 WITH ALLOW_CONNECTIONS false;', db='appdb')
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                ACL._psql(ACL.build_admission_recovery_sql(plan), ACL.CONTROL_DATABASE)
                recovered = ACL.snapshot()
            for database in recovered['databases']:
                self.assertTrue(database['allowConnect'], database['name'])
                self.assertFalse(any(g['grantee'] == 'PUBLIC' and g['privilege'] == 'CONNECT'
                                     for g in database['acl']), database['name'])
            self.assertEqual(before_app_acl, docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-c',
                "SELECT coalesce(datacl::text,'NULL') FROM pg_database WHERE datname='appdb'").stdout.strip())

            # Once BYPASSRLS role state exists, the same rollback SQL is refused.
            denied_rollback = docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d',
                                     'postgres', '-v', 'ON_ERROR_STOP=1', input=ACL.build_rollback_sql(plan), check=False)
            self.assertNotEqual(denied_rollback.returncode, 0)
            sql('DROP ROLE eros_runtime;')
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                ACL._psql(ACL.build_rollback_sql(plan))
                restored = ACL.snapshot()
            restored_by_name = {db['name']: db for db in restored['databases']}
            for database in ('postgres', 'template1'):
                self.assertEqual(ACL.normalized_acl(restored_by_name[database]['acl']),
                                 ACL.normalized_acl(before_owner_acl[database]))
            self.assertEqual(before_app_acl, docker('exec', container, 'psql', '-XqAt', '-U', 'postgres', '-c',
                "SELECT coalesce(datacl::text,'NULL') FROM pg_database WHERE datname='appdb'").stdout.strip())
        finally:
            docker('rm', '-f', container, check=False)


if __name__ == '__main__':
    unittest.main()
