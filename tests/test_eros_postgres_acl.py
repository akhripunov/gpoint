import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eros_postgres_acl', ROOT / 'scripts/eros-postgres-acl.py')
ACL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACL)


def grant(grantee, privilege, grantor='postgres', grantable=False):
    return {'grantee': grantee, 'privilege': privilege, 'grantor': grantor, 'grantable': grantable}


def plan():
    databases = []
    for name, oid in [('postgres', 5), ('template1', 1)]:
        before = [grant('PUBLIC', 'CONNECT'), grant('postgres', 'CONNECT'),
                  grant('postgres', 'CREATE'), grant('postgres', 'TEMPORARY')]
        databases.append({'name': name, 'oid': oid, 'owner': 'postgres', 'allowConnect': True,
                          'acl': ACL.normalized_acl(before),
                          'afterAcl': ACL.remove_public_connect(before)})
    now = datetime.now(timezone.utc)
    window = {'ownerConfirmed': True, 'barrierId': 'barrier:2026-10',
              'evidence': 'https://linear.app/bestuzhev/issue/ME-36',
              'scope': ACL.WINDOW_SCOPE,
              'startsAt': (now - timedelta(minutes=1)).isoformat(),
              'expiresAt': (now + timedelta(minutes=10)).isoformat()}
    return {'schema': ACL.SCHEMA, 'candidateSha': 'a' * 40, 'productionSha': 'b' * 40,
            'container': ACL.CONTAINER, 'containerId': 'c' * 64,
            'containerImage': ACL.POSTGRES_IMAGE, 'containerImageId': 'sha256:' + 'd' * 64,
            'serverVersionNum': 160001, 'clusterSystemIdentifier': '742001', 'databases': databases,
            'maintenanceRoleState': [],
            'maintenanceInventorySha256': 'e' * 64,
            'maintenanceWindow': window,
            'maintenanceInventory': {'ownerReviewed': True, 'knownClients': [],
                                     'unknownClients': [], 'maintenanceWindow': {
                                         **window}}}


class ErosPostgresAclTests(unittest.TestCase):
    def test_normalized_acl_treats_null_default_as_effective_acl(self):
        rows = [grant('postgres', 'CONNECT'), grant('PUBLIC', 'CONNECT')]
        self.assertEqual(ACL.normalized_acl(rows), ACL.normalized_acl(list(reversed(rows))))

    def test_psql_can_pin_docker_exec_to_verified_container_identity(self):
        identity = 'a' * 64
        with patch.object(ACL.subprocess, 'run', return_value=type('Result', (), {
                'returncode': 0, 'stdout': 'ok\n'})()) as run:
            self.assertEqual(ACL._psql('SELECT 1', database='gpoint', container=identity), 'ok')
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('exec') + 4], identity)

    def test_plan_allows_only_public_connect_removal_on_exact_targets(self):
        value = plan()
        ACL.validate_plan(value)
        value['databases'][0]['afterAcl'].append(grant('maintenance', 'CONNECT'))
        with self.assertRaisesRegex(ValueError, 'exceeds PUBLIC CONNECT'):
            ACL.validate_plan(value)

    def test_unknown_or_unreviewed_maintenance_client_blocks(self):
        value = plan()
        value['maintenanceInventory']['unknownClients'] = ['backup job']
        with self.assertRaisesRegex(ValueError, 'unknown maintenance clients'):
            ACL.validate_plan(value)
        value['maintenanceInventory'] = {'ownerReviewed': True, 'knownClients': [
            {'role': 'maintenance', 'database': 'postgres', 'access': 'public-connect', 'ownerApproved': True}],
            'unknownClients': [], 'maintenanceWindow': value['maintenanceWindow']}
        value['maintenanceRoleState'] = [{'name': 'maintenance'}]
        with self.assertRaisesRegex(ValueError, 'surviving access path'):
            ACL.validate_plan(value)

    def test_expired_or_too_short_exclusive_window_blocks(self):
        value = plan()
        expired = dict(value['maintenanceWindow'], startsAt='2026-10-23T00:00:00+00:00',
                       expiresAt='2026-10-23T00:01:00+00:00')
        value['maintenanceWindow'] = expired
        value['maintenanceInventory']['maintenanceWindow'] = expired
        with self.assertRaisesRegex(RuntimeError, 'not active'):
            ACL._assert_window_active(expired, minimum_remaining_seconds=30)

    def test_extra_database_and_existing_eros_state_stop_plan(self):
        value = plan()
        value['databases'].append(dict(value['databases'][0], name='gpoint'))
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            ACL.validate_plan(value)

    def test_apply_requires_exact_owner_and_explicit_approval(self):
        for key in ('EROS_ACL_APPLY_APPROVED', 'GITHUB_REPOSITORY_OWNER', 'GITHUB_ACTOR',
                    'GITHUB_TRIGGERING_ACTOR', 'GITHUB_EVENT_NAME', 'GITHUB_REF'):
            os.environ.pop(key, None)
        with self.assertRaisesRegex(RuntimeError, 'explicit Eros ACL Apply approval'):
            ACL.authorize_apply('a' * 40, 'a' * 40)
        env = {'EROS_ACL_APPLY_APPROVED': 'true', 'GITHUB_REPOSITORY_OWNER': 'owner',
               'GITHUB_ACTOR': 'owner', 'GITHUB_TRIGGERING_ACTOR': 'owner',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main'}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, 'matching exact candidate'):
                ACL.authorize_apply('a' * 40, 'b' * 40)
            ACL.authorize_apply('a' * 40, 'a' * 40)
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'collaborator'}):
                with self.assertRaisesRegex(RuntimeError, 'repository-owner'):
                    ACL.authorize_apply('a' * 40, 'a' * 40)

    def test_apply_uses_only_two_restrict_revokes_and_rolls_back_are_guarded(self):
        value = plan(); value['planSha256'] = ACL.plan_digest(value)
        calls = []
        with patch.object(ACL, 'load_plan', return_value=value), patch.object(ACL, 'verify_live'), \
             patch.object(ACL, '_verify_inventory'), \
             patch.object(ACL, '_psql', side_effect=lambda sql, database='postgres': calls.append((sql, database))), \
             patch.dict(os.environ, {'EROS_ACL_APPLY_APPROVED': 'true', 'GITHUB_REPOSITORY_OWNER': 'owner',
                 'GITHUB_ACTOR': 'owner', 'GITHUB_TRIGGERING_ACTOR': 'owner',
                 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main'}, clear=False):
            ACL.apply('/protected/plan', 'a' * 40, 'a' * 40)
        sql, database = calls[0]
        self.assertEqual(database, ACL.CONTROL_DATABASE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sql.count('REVOKE CONNECT ON DATABASE'), 2)
        self.assertIn('FROM PUBLIC RESTRICT', sql)
        self.assertNotIn('CASCADE', sql)
        self.assertIn("datname='eros'", sql)
        self.assertIn("rolname IN ('eros_ddl','eros_runtime','eros_migrator')", sql)
        self.assertIn('pg_stat_activity', sql)
        self.assertIn('has_database_privilege', sql)
        self.assertIn('LOCK TABLE pg_catalog.pg_database IN SHARE MODE', sql)
        self.assertIn('LOCK TABLE pg_catalog.pg_authid IN SHARE MODE', sql)
        self.assertIn('LOCK TABLE pg_catalog.pg_auth_members IN SHARE MODE', sql)
        self.assertIn('ALTER DATABASE postgres WITH ALLOW_CONNECTIONS false', sql)
        self.assertIn('ALTER DATABASE template1 WITH ALLOW_CONNECTIONS false', sql)
        self.assertIn('ALTER DATABASE postgres WITH ALLOW_CONNECTIONS true', sql)
        self.assertIn('ALTER DATABASE template1 WITH ALLOW_CONNECTIONS true', sql)
        self.assertEqual(sql.count('LOCK TABLE pg_catalog.pg_database IN SHARE MODE'), 2)
        self.assertIn('maintenance role attributes or memberships drifted', sql)
        self.assertIn('FROM pg_auth_members m WHERE m.member=r.oid', sql)

    def test_admission_recovery_is_exact_acl_guarded_and_owner_approved(self):
        value = plan()
        recovery = ACL.build_admission_recovery_sql(value)
        self.assertEqual(recovery.count('ALTER DATABASE'), 2)
        self.assertIn('IS DISTINCT FROM', recovery)
        self.assertIn("has_database_privilege(r.oid,d.oid,'CONNECT')", recovery)
        self.assertIn('admission recovery requires both planned targets to remain fenced', recovery)
        self.assertIn("rolname IN ('eros_ddl','eros_runtime','eros_migrator')", recovery)
        self.assertIn('LOCK TABLE pg_catalog.pg_database IN SHARE MODE', recovery)
        self.assertIn('LOCK TABLE pg_catalog.pg_authid IN SHARE MODE', recovery)
        self.assertIn('LOCK TABLE pg_catalog.pg_auth_members IN SHARE MODE', recovery)
        self.assertNotIn('REVOKE ', recovery)
        self.assertNotIn('GRANT ', recovery)
        self.assertNotIn('ALTER DATABASE gpoint', recovery)

    def test_rollback_restores_only_before_eros_state(self):
        value = plan(); value['planSha256'] = ACL.plan_digest(value)
        calls = []
        with patch.object(ACL, 'load_plan', return_value=value), patch.object(ACL, 'verify_live'), \
             patch.object(ACL, '_verify_inventory'), \
             patch.object(ACL, '_psql', side_effect=lambda sql, database='postgres': calls.append((sql, database))), \
             patch.dict(os.environ, {'EROS_ACL_APPLY_APPROVED': 'true', 'GITHUB_REPOSITORY_OWNER': 'owner',
                 'GITHUB_ACTOR': 'owner', 'GITHUB_TRIGGERING_ACTOR': 'owner',
                 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main'}, clear=False):
            ACL.rollback('/protected/plan', 'a' * 40, 'a' * 40)
        sql, database = calls[0]
        self.assertEqual(database, ACL.CONTROL_DATABASE)
        self.assertIn('GRANT CONNECT ON DATABASE postgres TO PUBLIC', sql)
        self.assertIn('GRANT CONNECT ON DATABASE template1 TO PUBLIC', sql)
        self.assertIn("datname='eros'", sql)
        self.assertIn('RAISE EXCEPTION', sql)


if __name__ == '__main__':
    unittest.main()
