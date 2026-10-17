import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('migration', ROOT / 'scripts/agata-production-migration.py')


class ProductionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path(SPEC.origin).exists(), 'canonical production migration gate is missing')
        self.m = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.m)

    def proof(self):
        return dict(database='gpoint', role='gpoint', head='agata0009', privileged=False,
                    owner=True, schema_create=True, schema_usage=True, permissions=True,
                    force_rls=True, rls=True, server_address='10.2.0.4', database_oid=16385, total_bytes=114688, url_source='AGATA_DATABASE_URL')

    def census(self):
        return dict(database='gpoint', authoritative=True, cluster_addresses=['10.2.0.4'], database_oid=16385,
                    tenants=['22222222-2222-4222-8222-222222222222',
                             '11111111-1111-4111-8111-111111111111'],
                    messages=12, baseline=5)

    def test_agata0009_candidate_applies_through_agata0011_only_with_privileged_migrator(self):
        proof = self.proof(); proof['privileged'] = True
        result = self.m.validate_readiness(proof, self.census())
        self.assertEqual(result['tenant_count'], 2)
        self.assertEqual(result['inventory'], ','.join(sorted(self.census()['tenants'])))
        self.assertEqual(result['lock_risk'], 'BOUNDED_MIGRATION_LOCK_REQUIRED')
        self.assertEqual(result['target_head'], 'agata0011')
        with self.assertRaisesRegex(RuntimeError, 'privileged migration identity'):
            self.m.validate_readiness(self.proof(), self.census())

    def test_agata0010_to_agata0011_requires_verified_privileged_migrator(self):
        proof = self.proof(); proof.update(head='agata0010', privileged=True)
        result = self.m.validate_readiness(proof, self.census())
        self.assertEqual(result['target_head'], 'agata0011')
        proof['privileged'] = False
        with self.assertRaisesRegex(RuntimeError, 'privileged migration identity'):
            self.m.validate_readiness(proof, self.census())

    def test_unsupported_migration_baseline_fails_closed(self):
        proof = self.proof(); proof['head'] = 'agata0008'
        with self.assertRaisesRegex(RuntimeError, 'only supports'):
            self.m.validate_readiness(proof, self.census())

    def test_missing_inventory_fails_closed(self):
        census = self.census(); census['tenants'] = []
        proof = self.proof(); proof['privileged'] = True
        with self.assertRaisesRegex(RuntimeError, 'inventory'):
            self.m.validate_readiness(proof, census)

    def test_role_filtered_census_is_never_complete(self):
        census = self.census(); census['authoritative'] = False
        with self.assertRaisesRegex(RuntimeError, 'authoritative'):
            self.m.validate_readiness(self.proof(), census)

    def test_wrong_database_or_missing_permissions_fails_closed(self):
        for key, value in [('database', 'maxibot'), ('owner', False), ('permissions', False),
                           ('schema_create', False), ('schema_usage', False), ('rls', False), ('force_rls', False), ('head', 'agata0008')]:
            with self.subTest(key=key):
                proof = self.proof(); proof[key] = value
                with self.assertRaises(RuntimeError):
                    self.m.validate_readiness(proof, self.census())

    def test_inventory_is_not_leaked_in_public_facts(self):
        proof = self.proof(); proof['privileged'] = True
        result = self.m.public_facts(self.m.validate_readiness(proof, self.census()))
        self.assertNotIn('inventory', result)
        self.assertNotIn('tenants', result)
        self.assertEqual(result['tenant_count'], 2)
        self.assertEqual(len(result['inventory_sha256']), 64)

    def test_backup_purpose_is_bounded_and_recovery_backup_is_not_mislabeled_pre_release(self):
        with self.assertRaisesRegex(RuntimeError, 'purpose is invalid'):
            self.m.complete_backup('a' * 40, purpose='candidate-time')
        def docker_checked(args, *, input=None):
            return 'f' if 'information_schema.schemata' in input else ''
        def docker_run(args, **kwargs):
            if any('pg_dump' in str(value) for value in args):
                kwargs['stdout'].write(b'disposable verified backup bytes')
                return SimpleNamespace(returncode=0, stdout=b'')
            return SimpleNamespace(returncode=0, stdout=b'TABLE DATA agata messages')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.m, 'resolve_recovery_postgres_container', return_value='a' * 64), \
                patch.object(self.m, 'authoritative_census', side_effect=lambda container=None: self.census()), \
                patch.object(self.m, 'docker_checked', side_effect=docker_checked), \
                patch.object(self.m.subprocess, 'run', side_effect=docker_run):
            result = self.m.complete_backup('a' * 40, directory=Path(directory), purpose='recovery-current')
            self.assertTrue(Path(result['path']).name.startswith('gpoint-recovery-current-' + 'a' * 40 + '-'))
            self.assertEqual(result['status'], 'COMPLETE_PROTECTED')
            backup_path = Path(result['path'])
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result['sha256'], hashlib.sha256(backup_path.read_bytes()).hexdigest())
            self.assertEqual(result['postgres_container_name'], '/shared_postgres_1')

    def test_damaged_recovery_dump_is_removed_and_never_reported_complete(self):
        def docker_checked(args, *, input=None, timeout=30):
            return 'f' if 'information_schema.schemata' in (input or '') else ''
        def docker_run(args, **kwargs):
            if any('pg_dump' in str(value) for value in args):
                kwargs['stdout'].write(b'damaged archive')
                return SimpleNamespace(returncode=0, stdout=b'')
            return SimpleNamespace(returncode=1, stdout=b'')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.m, 'resolve_recovery_postgres_container', return_value='a' * 64), \
                patch.object(self.m, 'authoritative_census', side_effect=lambda container=None: self.census()), \
                patch.object(self.m, 'docker_checked', side_effect=docker_checked), \
                patch.object(self.m.subprocess, 'run', side_effect=docker_run):
            with self.assertRaisesRegex(RuntimeError, 'archive verification failed'):
                self.m.complete_backup('a' * 40, directory=Path(directory), purpose='recovery-current')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_recovery_postgres_identity_requires_one_running_compose_postgres(self):
        container = {'Id': 'a' * 64, 'Name': '/shared_postgres_1',
                     'Config': {'Labels': {'com.docker.compose.project': 'shared',
                                           'com.docker.compose.service': 'postgres'}},
                     'State': {'Running': True}}
        def docker(args, **kwargs):
            if args[:3] == ['docker', 'ps', '-aq']:
                return SimpleNamespace(returncode=0, stdout='a' * 64 + '\n')
            if args[:3] == ['docker', 'inspect', 'a' * 64]:
                import json
                return SimpleNamespace(returncode=0, stdout=json.dumps([container]))
            self.fail(f'unexpected docker invocation: {args}')
        with patch.object(self.m.subprocess, 'run', side_effect=docker):
            self.assertEqual(self.m.resolve_recovery_postgres_container(), 'a' * 64)

    def test_recovery_postgres_identity_fails_closed_when_missing_or_ambiguous(self):
        for ids in ('', 'one\ntwo\n'):
            with self.subTest(ids=ids), patch.object(self.m.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0, stdout=ids)):
                with self.assertRaisesRegex(RuntimeError, 'unique running PostgreSQL'):
                    self.m.resolve_recovery_postgres_container()

    def test_recovery_backup_stops_before_census_or_file_creation_when_identity_is_unknown(self):
        for message in ('container missing', 'ambiguous containers'):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory, \
                    patch.object(self.m, 'resolve_recovery_postgres_container', side_effect=RuntimeError(message)), \
                    patch.object(self.m, 'authoritative_census') as census:
                with self.assertRaisesRegex(RuntimeError, message):
                    self.m.complete_backup('a' * 40, directory=Path(directory), purpose='recovery-current')
                census.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_recovery_postgres_identity_rejects_wrong_name_labels_or_stopped_container(self):
        invalid = [
            {'Name': '/compose-postgres-1', 'Config': {'Labels': {'com.docker.compose.project': 'shared', 'com.docker.compose.service': 'postgres'}}, 'State': {'Running': True}},
            {'Name': '/shared_postgres_1', 'Config': {'Labels': {'com.docker.compose.project': 'other', 'com.docker.compose.service': 'postgres'}}, 'State': {'Running': True}},
            {'Name': '/shared_postgres_1', 'Config': {'Labels': {'com.docker.compose.project': 'shared', 'com.docker.compose.service': 'postgres'}}, 'State': {'Running': False}},
        ]
        for item in invalid:
            with self.subTest(item=item):
                import json
                def docker(args, **kwargs):
                    if args[:3] == ['docker', 'ps', '-aq']:
                        return SimpleNamespace(returncode=0, stdout='a' * 64 + '\n')
                    return SimpleNamespace(returncode=0, stdout=json.dumps([item]))
                with patch.object(self.m.subprocess, 'run', side_effect=docker):
                    with self.assertRaisesRegex(RuntimeError, 'identity|PostgreSQL'):
                        self.m.resolve_recovery_postgres_container()

    def test_recovery_backup_uses_resolved_container_for_census_dump_and_restore(self):
        calls = []
        def docker_checked(args, *, input=None, timeout=30):
            calls.append((args, input))
            return 'f' if 'information_schema.schemata' in (input or '') else ''
        def docker_run(args, **kwargs):
            calls.append((args, None))
            if any('pg_dump' in str(value) for value in args):
                kwargs['stdout'].write(b'disposable verified backup bytes')
                return SimpleNamespace(returncode=0, stdout=b'')
            return SimpleNamespace(returncode=0, stdout=b'TABLE DATA agata messages')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.m, 'resolve_recovery_postgres_container', return_value='a' * 64), \
                patch.object(self.m, 'authoritative_census', return_value=self.census()) as census, \
                patch.object(self.m, 'docker_checked', side_effect=docker_checked), \
                patch.object(self.m.subprocess, 'run', side_effect=docker_run):
            result = self.m.complete_backup('a' * 40, directory=Path(directory), purpose='recovery-current')
        census.assert_called_once_with(container='a' * 64)
        docker_invocations = [args for args, _ in calls if args and args[0] == 'exec']
        self.assertTrue(docker_invocations)
        self.assertTrue(all('a' * 64 in args for args in docker_invocations))
        self.assertEqual(result['postgres_container_id'], 'a' * 64)

    def test_guard_precedes_census_and_is_held_through_migration(self):
        events = []
        class Guard:
            def __enter__(self): events.append('lock'); return self
            def census(self): events.append('census'); return self_outer.census()
            def alive(self): return True
            def verify(self, target_head): events.append(('verify', target_head)); return True
            def __exit__(self, *args): events.append('unlock')
        self_outer = self
        def migrate(inventory, guard):
            events.append('migrate')
            self.assertTrue(guard.alive())
            self.assertEqual(len(inventory.split(',')), 2)
        proof = self.proof(); proof['privileged'] = True
        self.m.guarded_migration(proof, Guard(), migrate)
        self.assertEqual(events, ['lock', 'census', 'migrate', ('verify', 'agata0011'), 'unlock'])

    def test_guard_loss_or_incomplete_baseline_is_not_success(self):
        for alive, verified in [(False, True), (True, False)]:
            class Guard:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def census(self): return self_outer.census()
                def alive(self): return alive
                def verify(self, target_head): return verified
            self_outer = self
            calls = []
            proof = self.proof(); proof['privileged'] = True
            with self.assertRaises(RuntimeError):
                self.m.guarded_migration(proof, Guard(), lambda *args: calls.append('apply'))
            if not alive: self.assertEqual(calls, [])


    def test_different_database_cluster_cannot_supply_inventory(self):
        proof = self.proof(); proof.update(server_address='10.2.0.4', database_oid=16385)
        census = self.census(); census.update(cluster_addresses=['10.2.0.5'], database_oid=16385)
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.m.validate_readiness(proof, census)

    def test_unprotected_migration_credentials_block_probe_before_container_start(self):
        with tempfile.TemporaryDirectory() as directory:
            migration = Path(directory) / 'migration.env'
            migration.write_text('AGATA_MIGRATOR_DATABASE_URL=disposable-secret\n')
            migration.chmod(0o644)
            self.m.MIGRATION_ENV_FILE = str(migration)
            def forbidden_docker(*args, **kwargs):
                self.fail('insecure credentials reached a Docker command')
            self.m.docker_checked = forbidden_docker
            with self.assertRaisesRegex(RuntimeError, 'protected 0600'):
                self.m.effective_probe('disposable-image')


if __name__ == '__main__': unittest.main()
