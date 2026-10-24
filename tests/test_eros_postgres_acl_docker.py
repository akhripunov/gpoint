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
            def pg_psql(statement, database='postgres'):
                return docker('exec', '-i', container, 'psql', '-XqAt', '-U', 'postgres', '-d', database,
                              '-v', 'ON_ERROR_STOP=1', input=statement).stdout.strip()
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                state = ACL.snapshot()
            self.assertEqual(state['publicConnectElsewhereCount'], 0)
            self.assertFalse(state['erosDatabaseExists'])
            self.assertFalse(state['roles'])
            before_owner_acl = {db['name']: db['acl'] for db in state['databases']}
            maintenance = [{'role': 'maintenance_owner', 'database': 'postgres', 'application': 'psql',
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
            plan = {'schema': ACL.SCHEMA, 'candidateSha': 'a' * 40, 'productionSha': 'b' * 40,
                    'container': ACL.CONTAINER, 'clusterSystemIdentifier': state['clusterSystemIdentifier'],
                    'containerId': identity[0], 'containerImage': identity[1], 'containerImageId': identity[2],
                    'serverVersionNum': state['serverVersionNum'], 'maintenanceInventorySha256': 'e' * 64,
                    'maintenanceRoleState': ACL.maintenance_role_state(['maintenance_owner']),
                    'databases': [], 'maintenanceWindow': window,
                    'maintenanceInventory': {'ownerReviewed': True, 'knownClients': maintenance,
                        'unknownClients': [], 'maintenanceWindow': window}}
            for db in state['databases']:
                plan['databases'].append({**db, 'acl': ACL.normalized_acl(db['acl']),
                                          'afterAcl': ACL.remove_public_connect(db['acl'])})
            ACL.validate_plan(plan)
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
            with patch.object(ACL, '_psql', side_effect=pg_psql):
                ACL._psql(ACL.build_apply_sql(plan))
                after = ACL.snapshot()
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
