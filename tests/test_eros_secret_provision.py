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
                                      image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            first = MODULE.read_env(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output, 'EROS_SECRET_STORE=READY')
            self.assertEqual(first['EROS_SOURCE_SHA'], 'a' * 40)
            self.assertTrue(first['EROS_JWT_SECRET'])
            self.assertTrue(first['EROS_EMBEDDING_API_KEY'])
            self.assertTrue(first['EROS_WORKER_SIGNING_SECRET'])
            self.assertEqual(first['EROS_RUNTIME_DATABASE_URL'].split('@')[1], 'gpoint-eros-db:5432/eros')
            self.assertEqual(first['EROS_MIGRATOR_DATABASE_URL'].split('@')[1], 'gpoint-eros-db:5432/eros')
            self.assertNotIn(first['EROS_JWT_SECRET'], output)
            self.assertEqual(MODULE.provision(path, source_sha='c' * 40,
                                             image='ghcr.io/akhripunov/eros@sha256:' + 'd' * 64), output)
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
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
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
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
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
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            self.assertEqual(list(target.iterdir()), [])

    def test_rejects_unknown_or_incomplete_existing_entries_without_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            path.write_text('EROS_JWT_SECRET=existing\nUNEXPECTED=value\n')
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                MODULE.provision(path, source_sha='a' * 40,
                                 image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            self.assertIn('EROS_JWT_SECRET=existing', path.read_text())

    def test_uses_independent_cryptographic_values_for_each_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = MODULE._new_values('a' * 40, 'ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            tokens = [values[key] for key in ('EROS_JWT_SECRET', 'EROS_EMBEDDING_API_KEY',
                                               'EROS_WORKER_SIGNING_SECRET', 'EROS_RUNTIME_PASSWORD',
                                               'EROS_MIGRATOR_PASSWORD', 'EROS_DATABASE_IDENTITY')]
            self.assertEqual(len(set(tokens)), len(tokens))
            self.assertTrue(all(len(token) >= 43 for token in tokens))

    def test_database_provisioning_uses_a_dedicated_db_and_restricted_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            results = iter(['false', '{"memberships": 0, "foreign_database_owners": 0}', '', '', ''])
            calls = []
            def run(args, **kwargs):
                calls.append((args, kwargs))
                return SimpleNamespace(returncode=0, stdout=next(results), stderr='')
            with patch.object(MODULE.subprocess, 'run', side_effect=run):
                self.assertEqual(MODULE.provision_database(path), 'EROS_DATABASE_ROLES=READY')
            self.assertEqual(len(calls), 5)
            sql = '\n'.join(call[1]['input'] for call in calls)
            self.assertIn('CREATE DATABASE eros OWNER eros_ddl', sql)
            self.assertIn('eros_deployment_identity', sql)
            self.assertIn('REVOKE CONNECT ON DATABASE gpoint FROM eros_runtime, eros_migrator', sql)
            self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', sql)
            self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', sql)
            for key in MODULE.SECRET_KEYS:
                self.assertNotIn(MODULE.read_env(path)[key], 'EROS_DATABASE_ROLES=READY')

    def test_existing_database_requires_owner_and_matching_identity_before_role_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            with patch.object(MODULE, '_psql', side_effect=['true', 'eros_ddl', 'wrong-identity']) as psql:
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 3)

    def test_unexpected_role_membership_blocks_before_grants_or_password_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            with patch.object(MODULE, '_psql', side_effect=['false', '{"memberships": 1, "foreign_database_owners": 0}']) as psql:
                with self.assertRaisesRegex(RuntimeError, 'role inventory is unsafe'):
                    MODULE.provision_database(path)
            self.assertEqual(psql.call_count, 2)

    def test_database_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'eros.env'
            MODULE.provision(path, source_sha='a' * 40,
                             image='ghcr.io/akhripunov/eros@sha256:' + 'b' * 64)
            secret = MODULE.read_env(path)['EROS_RUNTIME_PASSWORD']
            failed = SimpleNamespace(returncode=1, stdout='', stderr='bad SQL ' + secret)
            with patch.object(MODULE.subprocess, 'run', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'credential output') as error:
                    MODULE.provision_database(path)
            self.assertNotIn(secret, str(error.exception))


if __name__ == '__main__':
    unittest.main()
