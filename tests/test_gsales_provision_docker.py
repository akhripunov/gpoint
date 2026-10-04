"""First provisioning/retry against an isolated real agata0010 database."""
from argparse import Namespace
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get('GPOINT_MIGRATION_TEST_IMAGE')


@unittest.skipUnless(IMAGE, 'Prepare must supply the exact AGATA candidate image')
class ProvisionDockerTests(unittest.TestCase):
    def setUp(self):
        def interrupt(_signum, _frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.signal(signum, interrupt)
            self.addCleanup(signal.signal, signum, previous)

    def test_authorized_first_provision_backup_private_credentials_and_idempotent_retry(self):
        spec = importlib.util.spec_from_file_location('provision', ROOT / 'scripts/gsales-production-provision.py')
        provision = importlib.util.module_from_spec(spec); spec.loader.exec_module(provision)
        suffix = uuid4().hex[:12]
        network, postgres = 'gpoint-provision-net-' + suffix, 'gpoint-provision-pg-' + suffix
        password, tenant = uuid4().hex, str(uuid4())
        candidate, baseline = 'a'*40, 'b'*40

        def docker(*args, input=None, check=True):
            result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True, timeout=180)
            if check and result.returncode:
                self.fail('disposable provisioning Docker command failed; credential details withheld')
            return result

        def query(statement, database='gpoint'):
            return docker('exec', '-i', postgres, 'psql', '-XqAt', '-U', 'postgres', '-d', database,
                          '-v', 'ON_ERROR_STOP=1', input=statement).stdout.strip()

        with tempfile.TemporaryDirectory(prefix='gpoint-provision-') as scratch:
            directory = Path(scratch)
            env_file, bootstrap = directory / 'production.env', directory / 'bootstrap.env'
            bootstrap.write_text(f'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD={password}\nPGPASSWORD={password}\n'
                                 f'AGATA_DATABASE_URL=postgresql+asyncpg://postgres:{password}@{postgres}:5432/gpoint\n'
                                 f'AGATA_TENANT_ID={tenant}\n')
            bootstrap.chmod(0o600)
            runtime = {'RELEASE_SHA': baseline,
                       'DATABASE_URL': f'postgresql://gpoint:{password}@{postgres}:5432/gpoint',
                       'AGATA_DATABASE_URL': f'postgresql+asyncpg://gpoint:{password}@{postgres}:5432/gpoint',
                       'AGATA_TENANT_ID': tenant, 'GPOINT_AGATA_WORKER_TOKEN': 'disposable-core-lane-token'}
            for key in ('MESSAGE_AEAD_KEY','REQUEST_FINGERPRINT_HMAC_KEY','RETRIEVAL_QUERY_HMAC_KEY','RESPONSE_FINGERPRINT_HMAC_KEY','SHADOW_CANDIDATE_HMAC_KEY'):
                with bootstrap.open('a') as stream:
                    stream.write('AGATA_' + key + '=' + base64.b64encode(b'a'*32).decode() + '\n')

            def write_runtime(values):
                env_file.write_text(''.join(key + '=' + value + '\n' for key, value in values.items()))
                env_file.chmod(0o600)

            def run(approved=candidate, expected=baseline):
                args = Namespace(candidate=candidate, owner_approved_sha=approved, baseline=expected)
                with redirect_stdout(io.StringIO()) as output:
                    provision.provision(args, env_file=env_file, db_container=postgres,
                                        db_network=network, probe_image=IMAGE, backup_directory=directory / 'backups')
                return json.loads(output.getvalue())

            write_runtime(runtime)
            try:
                docker('network', 'create', network)
                docker('run', '-d', '--name', postgres, '--network', network, '--env-file', str(bootstrap), 'pgvector/pgvector:0.8.6-pg16')
                for _ in range(60):
                    if not docker('exec', postgres, 'psql', '-h', '127.0.0.1', '-U', 'postgres', '-Atc', 'SELECT 1', check=False).returncode: break
                    time.sleep(0.5)
                else: self.fail('disposable provisioning PostgreSQL not ready')
                docker('exec', postgres, 'createdb', '-U', 'postgres', 'gpoint')
                query(f"CREATE ROLE gpoint LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;")
                docker('run', '--rm', '--network', network, '--env-file', str(bootstrap), IMAGE, 'alembic', 'upgrade', 'agata0010')
                query('CREATE TABLE public.provision_fixture(id int PRIMARY KEY); INSERT INTO public.provision_fixture VALUES(42);')
                before = query('SELECT id::text FROM agata.tenants ORDER BY id')
                self.assertEqual(before, tenant)
                for approved, expected in [('c'*40, baseline), (candidate, 'c'*40)]:
                    with self.assertRaisesRegex(RuntimeError, 'authorization|baseline'):
                        run(approved, expected)
                query("UPDATE agata.alembic_version SET version_num='agata0011'")
                with self.assertRaisesRegex(RuntimeError, 'agata0010'):
                    run()
                query("UPDATE agata.alembic_version SET version_num='agata0010'")
                extra = str(uuid4()); query(f"INSERT INTO agata.tenants(id) VALUES('{extra}')")
                with self.assertRaisesRegex(RuntimeError, 'unmapped'):
                    run()
                query(f"DELETE FROM agata.tenants WHERE id='{extra}'")
                self.assertFalse((directory / 'backups').exists(), 'failed preflight created a backup or mutation')
                self.assertEqual(query('SELECT id::text FROM agata.tenants ORDER BY id'), before)
                self.assertTrue(provision.read_env(env_file) == runtime, 'preflight changed protected runtime credentials')
                real_stage = provision.stage_env
                stages = []
                def fail_second_stage(file, values):
                    stages.append(file)
                    if len(stages) == 2:
                        raise OSError('disposable staging failure')
                    return real_stage(file, values)
                with patch.object(provision, 'stage_env', side_effect=fail_second_stage):
                    with self.assertRaisesRegex(OSError, 'staging failure'):
                        run()
                self.assertFalse(list(directory.glob('.provision-pending-*')), 'pre-SQL staging failure left secrets on disk')
                query("CREATE FUNCTION agata.reject_provision_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture rejected'; END $$; "
                      "CREATE TRIGGER reject_provision_fixture BEFORE INSERT ON agata.tenants FOR EACH ROW EXECUTE FUNCTION agata.reject_provision_fixture();")
                with self.assertRaisesRegex(RuntimeError, 'SQL attempted.*recovery'):
                    run()
                pending = list(directory.glob('.provision-pending-*'))
                self.assertEqual(len(pending), 2, 'uncertain SQL outcome discarded the only protected role credentials')
                for staged in pending:
                    self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
                self.assertEqual(query('SELECT id::text FROM agata.tenants ORDER BY id'), before)
                self.assertEqual(query("SELECT count(*) FROM pg_roles WHERE rolname IN ('gsales_runtime','gsales_agata_runtime')"), '0')
                self.assertTrue(provision.read_env(env_file) == runtime, 'failed provisioning changed runtime credentials')
                # Fixture-only cleanup follows authoritative rollback proof.
                for staged in pending:
                    staged.unlink()
                query('DROP TRIGGER reject_provision_fixture ON agata.tenants; DROP FUNCTION agata.reject_provision_fixture();')
                result = run()
                self.assertEqual(result['status'], 'PROVISIONED')
                self.assertFalse(result['sendingEnabled'])
                self.assertEqual(result['backup']['status'], 'COMPLETE_PROTECTED')
                backup_file = Path(result['backup']['path'])
                self.assertEqual(backup_file.stat().st_mode & 0o777, 0o600)
                migration_file = directory / 'migration.env'
                self.assertEqual(migration_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                final_runtime = provision.read_env(env_file)
                migration = provision.read_env(migration_file)
                self.assertNotIn('AGATA_MIGRATOR_DATABASE_URL', final_runtime)
                self.assertNotIn('GSALES_MIGRATION_DATABASE_URL', final_runtime)
                self.assertEqual(set(migration), {'AGATA_MIGRATOR_DATABASE_URL', 'GSALES_MIGRATION_DATABASE_URL'})
                self.assertEqual(urlsplit(migration['GSALES_MIGRATION_DATABASE_URL']).username, 'postgres')
                self.assertEqual(urlsplit(final_runtime['GSALES_DATABASE_URL']).username, 'gsales_runtime')
                self.assertEqual(final_runtime['AGATA_DATABASE_URL'], runtime['AGATA_DATABASE_URL'])
                self.assertTrue('GSALES_OWNER_API_TOKEN' in final_runtime, 'provisioning omitted the Settings-required owner token')
                sales_tenant = final_runtime['GSALES_TENANT_ID']
                self.assertNotEqual(sales_tenant, tenant)
                for flag in ('OUTREACH_ENABLED', 'AUTO_REPLY_ENABLED', 'MANAGEMENT_ENABLED', 'TELEGRAM_LISTENER_ENABLED'):
                    self.assertEqual(final_runtime['GSALES_' + flag], 'false')
                self.assertEqual(query("SELECT count(*) FROM pg_roles WHERE rolname IN ('gsales_runtime','gsales_agata_runtime') AND rolcanlogin AND NOT rolinherit AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole"), '2')
                self.assertEqual(query("SELECT has_table_privilege('gsales_agata_runtime','agata.alembic_version','INSERT'),has_table_privilege('gsales_agata_runtime','agata.alembic_version','UPDATE'),has_table_privilege('gsales_agata_runtime','agata.alembic_version','DELETE')"), 'f|f|f')
                docker('exec', postgres, 'createdb', '-U', 'postgres', 'gpoint_provision_backup')
                with backup_file.open('rb') as stream:
                    restore = subprocess.run(['docker','exec','-i',postgres,'pg_restore','-U','postgres','-d','gpoint_provision_backup','--clean','--if-exists','--no-owner'], stdin=stream, capture_output=True, timeout=120)
                self.assertEqual(restore.returncode, 0, 'pre-provision backup restore failed: ' + '\n'.join(line for line in restore.stderr.decode().splitlines() if 'ERROR:' in line)[:1200])
                self.assertEqual(query('SELECT id::text FROM agata.tenants ORDER BY id', 'gpoint_provision_backup'), before,
                                 'backup must precede Sales tenant creation')
                self.assertEqual(query('SELECT id FROM public.provision_fixture', 'gpoint_provision_backup'), '42')
                retry = run()
                self.assertEqual(retry['gsalesTenant'], sales_tenant)
                self.assertTrue(provision.read_env(env_file) == final_runtime, 'retry rotated protected runtime credentials')
                self.assertTrue(provision.read_env(migration_file) == migration, 'retry changed protected migration credentials')
                self.assertEqual(query('SELECT count(*) FROM agata.tenants'), '2')
                self.assertEqual(query(f"SELECT count(*) FROM agata.regional_policy_profiles WHERE tenant_id='{sales_tenant}'"), '1')
                self.assertFalse(list(directory.glob('.provision-pending-*')))
                # An escaped existing password must authenticate and remain
                # singly encoded on retry, without rotating the actual role.
                escaped_password = 'disposable:@%/ password'
                query("ALTER ROLE gsales_runtime PASSWORD '" + escaped_password + "'")
                final_runtime['GSALES_DATABASE_URL'] = f'postgresql://gsales_runtime:{quote(escaped_password, safe="")}@{postgres}:5432/gpoint'
                write_runtime(final_runtime)
                run()
                self.assertTrue(provision.read_env(env_file) == final_runtime, 'retry double-encoded an existing role password')
                query('ALTER ROLE gsales_runtime INHERIT')
                with self.assertRaisesRegex(RuntimeError, 'safely reused'):
                    run()
                query('ALTER ROLE gsales_runtime NOINHERIT')
                query('ALTER ROLE gsales_runtime NOLOGIN')
                with self.assertRaisesRegex(RuntimeError, 'safely reused'):
                    run()
                query('ALTER ROLE gsales_runtime LOGIN')
                # Disk publication can fail after the transaction committed.
                # Retain its protected staged credentials and block release.
                real_replace = provision.os.replace
                replacements = []
                def fail_runtime_publication(source, target):
                    replacements.append(target)
                    if len(replacements) == 2:
                        raise OSError('disposable publication failure')
                    return real_replace(source, target)
                with patch.object(provision.os, 'replace', side_effect=fail_runtime_publication):
                    with self.assertRaisesRegex(RuntimeError, 'database committed.*recovery'):
                        run()
                pending = list(directory.glob('.provision-pending-*'))
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].stat().st_mode & 0o777, 0o600)
                real_replace(pending[0], env_file)  # Explicit fixture-only recovery.
                run()
                self.assertTrue(provision.read_env(env_file) == final_runtime, 'postcommit recovery changed protected role credentials')
                self.assertFalse(list(directory.glob('.provision-pending-*')))
                # Rehearse the protected overlay through the real next AGATA
                # migration, then restore a second complete backup at head.
                inventory = query('SELECT id::text FROM agata.tenants ORDER BY id').replace('\n', ',')
                docker('run', '--rm', '--network', network, '--env-file', str(env_file),
                       '--env-file', str(migration_file), '-e', 'AGATA_MIGRATION_TENANT_IDS=' + inventory,
                       IMAGE, 'alembic', 'upgrade', 'head')
                self.assertEqual(query('SELECT version_num FROM agata.alembic_version'), 'agata0011')
                backup_spec = importlib.util.spec_from_file_location('head_backup', ROOT / 'scripts/agata-production-migration.py')
                backup_gate = importlib.util.module_from_spec(backup_spec); backup_spec.loader.exec_module(backup_gate)
                backup_gate.DB_CONTAINER = postgres
                head_backup = backup_gate.complete_backup(candidate, directory=directory / 'backups')
                self.assertIn('vector', head_backup['extensions'])
                docker('exec', postgres, 'createdb', '-U', 'postgres', 'gpoint_provision_head_backup')
                with Path(head_backup['path']).open('rb') as stream:
                    restored_head = subprocess.run(['docker','exec','-i',postgres,'pg_restore','-U','postgres',
                        '-d','gpoint_provision_head_backup','--clean','--if-exists','--no-owner'], stdin=stream, capture_output=True, timeout=120)
                self.assertEqual(restored_head.returncode, 0, 'complete AGATA head backup restore failed; details withheld')
                self.assertEqual(query('SELECT version_num FROM agata.alembic_version', 'gpoint_provision_head_backup'), 'agata0011')
                self.assertEqual(query('SELECT count(*) FROM agata.tenants', 'gpoint_provision_head_backup'), '2')
                self.assertEqual(query('SELECT count(*) FROM agata.memories', 'gpoint_provision_head_backup'), '0')
            finally:
                docker('rm', '-f', postgres, check=False)
                docker('network', 'rm', network, check=False)


if __name__ == '__main__': unittest.main()
