import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eros_secrets', ROOT / 'scripts/eros-production-secrets.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ErosProductionSecretTests(unittest.TestCase):
    def test_generates_persistent_credentials_without_emitting_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            output = MODULE.provision(path, source_sha='a' * 40,
                                      image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                      tenant_id='12345678-1234-4234-8234-123456789abc')
            first = MODULE.read_env(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output, 'EROS_SECRET_STORE=READY')
            self.assertEqual(first['EROS_SOURCE_SHA'], 'a' * 40)
            self.assertEqual(first['EROS_TENANT_ID'], '12345678-1234-4234-8234-123456789abc')
            self.assertEqual(first['EROS_TEST_USER_IDS'], '')
            self.assertEqual(first['EROS_TEST_CREATOR_IDS'], '')
            self.assertTrue(first['EROS_JWT_SECRET'])
            self.assertTrue(first['EROS_EMBEDDING_API_KEY'])
            self.assertTrue(first['EROS_WORKER_SIGNING_SECRET'])
            self.assertEqual(first['EROS_RUNTIME_DATABASE_URL'].split('@')[1], 'gpoint-eros-db:5432/eros')
            self.assertEqual(first['EROS_MIGRATOR_DATABASE_URL'].split('@')[1], 'gpoint-eros-db:5432/eros')
            self.assertNotIn(first['EROS_JWT_SECRET'], output)
            self.assertEqual(MODULE.provision(path, source_sha='c' * 40,
                                             image='ghcr.io/akhripunov/eros@sha256:' + 'd' * 64,
                                             tenant_id='12345678-1234-4234-8234-123456789abc'), output)
            second = MODULE.read_env(path)
            for key in ('EROS_JWT_SECRET', 'EROS_EMBEDDING_API_KEY', 'EROS_WORKER_SIGNING_SECRET',
                        'EROS_RUNTIME_PASSWORD', 'EROS_MIGRATOR_PASSWORD'):
                self.assertEqual(second[key], first[key])
            self.assertEqual(second['EROS_SOURCE_SHA'], 'c' * 40)

    def test_rejects_unexpected_permissions_and_malformed_identity_without_replacing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            path.write_text('EROS_JWT_SECRET=existing\n')
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'permissions'):
                MODULE.provision(path, source_sha='a' * 40,
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                 tenant_id='12345678-1234-4234-8234-123456789abc')
            self.assertEqual(path.read_text(), 'EROS_JWT_SECRET=existing\n')
            self.assertEqual(MODULE.read_env(path)['EROS_JWT_SECRET'], 'existing')

    def test_rejects_symlinked_secret_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'outside.env'
            target.write_text('preserve-me\n')
            path = root / 'eros.env'
            path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                MODULE.provision(path, source_sha='a' * 40,
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                 tenant_id='12345678-1234-4234-8234-123456789abc')
            self.assertEqual(target.read_text(), 'preserve-me\n')

    def test_rejects_symlinked_secret_directory_before_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'real'
            target.mkdir()
            linked = root / 'linked'
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'directory cannot be a symlink'):
                MODULE.provision(linked / 'eros.env', source_sha='a' * 40,
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                 tenant_id='12345678-1234-4234-8234-123456789abc')
            self.assertEqual(list(target.iterdir()), [])

    def test_rejects_unknown_or_incomplete_existing_entries_without_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            path.write_text('EROS_JWT_SECRET=existing\nUNEXPECTED=value\n')
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                MODULE.provision(path, source_sha='a' * 40,
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                 tenant_id='12345678-1234-4234-8234-123456789abc')
            self.assertIn('EROS_JWT_SECRET=existing', path.read_text())

    def test_uses_independent_cryptographic_values_for_each_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = MODULE._new_values('a' * 40, 'ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                                        '12345678-1234-4234-8234-123456789abc')
            tokens = [values[key] for key in ('EROS_JWT_SECRET', 'EROS_EMBEDDING_API_KEY',
                                               'EROS_WORKER_SIGNING_SECRET', 'EROS_RUNTIME_PASSWORD',
                                               'EROS_MIGRATOR_PASSWORD', 'EROS_DATABASE_IDENTITY')]
            self.assertEqual(len(set(tokens)), len(tokens))
            self.assertTrue(all(len(token) >= 43 for token in tokens))

    def test_provision_clears_route_allowlists_until_synthetic_isolation_is_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            original = path.read_text()
            path.write_text(original.replace('EROS_TEST_USER_IDS=\n', 'EROS_TEST_USER_IDS=real-user-should-not-survive\n')
                             .replace('EROS_TEST_CREATOR_IDS=\n', 'EROS_TEST_CREATOR_IDS=real-creator-should-not-survive\n'))
            path.chmod(0o600)
            MODULE.provision(path, source_sha='c' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'd' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            values = MODULE.read_env(path)
            self.assertEqual(values['EROS_TEST_USER_IDS'], '')
            self.assertEqual(values['EROS_TEST_CREATOR_IDS'], '')

    def test_database_provisioning_uses_a_dedicated_db_and_restricted_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            results = iter(['[]', 'false', '{"memberships": 0, "foreign_database_owners": 0, "foreign_database_connections": 0, "existing_eros_roles": 0}', '', '', '', '[]'])
            calls = []
            def run(args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(returncode=0, stdout=next(results), stderr='')
            with patch.object(MODULE.subprocess, 'run', side_effect=run):
                self.assertEqual(MODULE.provision_database(path), 'EROS_DATABASE_ROLES=READY')
            self.assertEqual(len(calls), 7)
            sql = '\n'.join(call[1]['input'] for call in calls)
            self.assertIn('CREATE DATABASE eros OWNER eros_ddl', sql)
            self.assertIn('eros_deployment_identity', sql)
            self.assertIn('REVOKE CONNECT ON DATABASE gpoint FROM eros_runtime, eros_migrator', sql)
            self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', sql)
            self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', sql)
            self.assertIn("has_database_privilege('eros_runtime', datname, 'CONNECT')", sql)
            self.assertIn("acl.grantee=0 AND acl.privilege_type='CONNECT'", sql)
            for key in MODULE.SECRET_KEYS:
                self.assertNotIn(MODULE.read_env(path)[key], 'EROS_DATABASE_ROLES=READY')

    def test_public_connect_acl_blocks_before_creating_database_or_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            with patch.object(MODULE, '_psql', return_value='["gpoint", "postgres"]') as psql:
                with self.assertRaisesRegex(RuntimeError, 'PUBLIC CONNECT'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 1)

    def test_existing_database_requires_owner_and_matching_identity_before_role_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            with patch.object(MODULE, '_psql', side_effect=['[]', 'true', 'eros_ddl', 'wrong-identity']) as psql:
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 4)

    def test_unexpected_role_membership_blocks_before_grants_or_password_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            with patch.object(MODULE, '_psql', side_effect=['[]', 'false', '{"memberships": 1, "foreign_database_owners": 0, "foreign_database_connections": 0, "existing_eros_roles": 0}']) as psql:
                with self.assertRaisesRegex(RuntimeError, 'role inventory is unsafe'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 3)

    def test_orphaned_eros_role_blocks_database_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            results = iter(['[]', 'false', '{"memberships": 0, "foreign_database_owners": 0, "foreign_database_connections": 0, "existing_eros_roles": 1}'])
            with patch.object(MODULE, '_psql', side_effect=lambda *args, **kwargs: next(results)) as psql:
                with self.assertRaisesRegex(RuntimeError, 'role inventory is unsafe'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 3)

    def test_database_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64,
                             tenant_id='12345678-1234-4234-8234-123456789abc')
            secret = MODULE.read_env(path)['EROS_RUNTIME_PASSWORD']
            failed = SimpleNamespace(returncode=1, stdout='', stderr='bad SQL ' + secret)
            with patch.object(MODULE.subprocess, 'run', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'credential output') as error:
                    MODULE.provision_database(path)
            self.assertNotIn(secret, str(error.exception))

    def test_existing_postgres_is_attached_once_to_private_eros_network_with_stable_alias(self):
        network = [{"Driver": "bridge", "Internal": True}]
        unattached = [{"NetworkSettings": {"Networks": {"shared_internal": {"Aliases": ["shared_postgres_1"]}}}}]
        attached = [{"NetworkSettings": {"Networks": {"shared_internal": {"Aliases": ["shared_postgres_1"]},
                                                       "gpoint_eros_data": {"Aliases": ["shared_postgres_1", "gpoint-eros-db"]}}}}]
        responses = [
            SimpleNamespace(returncode=1, stdout='', stderr='Error: network gpoint_eros_data not found'),
            SimpleNamespace(returncode=0, stdout='network-id', stderr=''),
            SimpleNamespace(returncode=0, stdout=__import__('json').dumps(network), stderr=''),
            SimpleNamespace(returncode=0, stdout=__import__('json').dumps(unattached), stderr=''),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=0, stdout=__import__('json').dumps(attached), stderr=''),
        ]
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return responses.pop(0)
        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            self.assertEqual(MODULE.ensure_postgres_network(), 'EROS_POSTGRES_NETWORK=READY')
        self.assertEqual(calls[1][-1], 'gpoint_eros_data')
        self.assertEqual(calls[4][-5:], ['connect', '--alias', 'gpoint-eros-db', 'gpoint_eros_data', 'shared_postgres_1'])
        self.assertEqual(len(responses), 0)


if __name__ == '__main__':
    unittest.main()
