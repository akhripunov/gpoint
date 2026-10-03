"""Production migration protocol against isolated PostgreSQL and the actual image."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

IMAGE = os.environ.get('GPOINT_MIGRATION_TEST_IMAGE')
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(IMAGE, 'run in Prepare after exact AGATA image build')
class DockerMigrationProtocol(unittest.TestCase):
    def test_full_inventory_guard_restricted_image_and_complete_backup(self):
        spec = importlib.util.spec_from_file_location('migration', ROOT / 'scripts/agata-production-migration.py')
        gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
        suffix = uuid4().hex[:12]
        network, postgres = 'gpoint-inventory-net-' + suffix, 'gpoint-inventory-pg-' + suffix
        tenant1, tenant2 = str(uuid4()), str(uuid4())
        def docker(*args, input=None, check=True):
            result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True, timeout=180)
            if check and result.returncode: self.fail('isolated Docker rehearsal command failed: ' + result.stderr[:1500])
            return result
        with tempfile.TemporaryDirectory(prefix='gpoint-inventory-') as scratch:
            env = Path(scratch) / 'test.env'
            env.write_text(f'AGATA_DATABASE_URL=postgresql+asyncpg://gpoint:disposable-ci-password@{postgres}:5432/gpoint\nAGATA_TENANT_ID={tenant1}\n')
            env.chmod(0o600)
            try:
                docker('network', 'create', network)
                docker('run', '-d', '--name', postgres, '--network', network,
                       '-e', 'POSTGRES_USER=postgres', '-e', 'POSTGRES_PASSWORD=disposable-ci-password', 'pgvector/pgvector:0.8.6-pg16')
                for _ in range(60):
                    ready = docker('exec', '-e', 'PGPASSWORD=disposable-ci-password', postgres,
                        'psql', '-h', '127.0.0.1', '-U', 'postgres', '-Atc', 'SELECT 1', check=False)
                    if ready.returncode == 0: break
                    time.sleep(1)
                else: self.fail('disposable PostgreSQL not ready')
                docker('exec', postgres, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c',
                       "CREATE ROLE gpoint LOGIN PASSWORD 'disposable-ci-password' NOSUPERUSER NOBYPASSRLS")
                docker('exec', postgres, 'createdb', '-U', 'postgres', '-O', 'gpoint', 'gpoint')
                sql = f"""
                CREATE SCHEMA agata AUTHORIZATION gpoint;
                CREATE TABLE agata.tenants(id uuid PRIMARY KEY);
                CREATE TABLE agata.conversations(id uuid PRIMARY KEY,tenant_id uuid,source_system text);
                CREATE TABLE agata.messages(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,character_id uuid,
                  conversation_id uuid,scope text,role text,scenario_id uuid,deleted_at timestamptz);
                CREATE TABLE agata.alembic_version(version_num varchar(32) PRIMARY KEY);
                INSERT INTO agata.alembic_version VALUES('agata0009');
                CREATE TABLE public.backup_fixture(id integer); INSERT INTO public.backup_fixture VALUES(42);
                INSERT INTO agata.tenants VALUES('{tenant1}'),('{tenant2}');
                INSERT INTO agata.conversations SELECT id,id,'gpoint' FROM agata.tenants;
                INSERT INTO agata.messages SELECT id,id,id,id,id,'real_world','assistant',NULL,NULL FROM agata.tenants;
                """
                for table in ('tenants', 'conversations', 'messages', 'alembic_version'):
                    sql += f'ALTER TABLE agata.{table} OWNER TO gpoint;'
                for table in ('tenants', 'conversations', 'messages'):
                    sql += f"ALTER TABLE agata.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE agata.{table} FORCE ROW LEVEL SECURITY; CREATE POLICY tenant_policy ON agata.{table} USING ({'id' if table == 'tenants' else 'tenant_id'}=nullif(current_setting('app.tenant_id',true),'')::uuid);"
                docker('exec', '-i', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', input=sql)
                gate.DB_CONTAINER, gate.DB_NETWORK, gate.ENV_FILE = postgres, network, str(env)
                proof = gate.effective_probe(IMAGE)
                self.assertFalse(proof['privileged']); self.assertTrue(proof['force_rls'])
                census = gate.authoritative_census()
                self.assertIn(proof['server_address'], census['cluster_addresses'], repr((proof['server_address'], census['cluster_addresses'], proof['database_oid'], census['database_oid'])))
                readiness = gate.validate_readiness(proof, census)
                self.assertEqual(readiness['tenant_count'], 2)
                refused = docker('run', '--rm', '--network', network, '--env-file', str(env), IMAGE,
                                 'alembic', 'upgrade', 'head', check=False)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn('complete AGATA_MIGRATION_TENANT_IDS', refused.stderr)
                self.assertEqual(gate.effective_probe(IMAGE)['head'], 'agata0009')
                backup = gate.complete_backup('a'*40, directory=Path(scratch) / 'backup')
                self.assertEqual(backup['status'], 'COMPLETE_PROTECTED')
                self.assertEqual(Path(backup['path']).stat().st_mode & 0o777, 0o600)
                docker('exec', postgres, 'createdb', '-U', 'postgres', 'gpoint_restore_test')
                with open(backup['path'], 'rb') as stream:
                    restored = subprocess.run(['docker','exec','-i',postgres,'pg_restore','-U','postgres',
                        '-d','gpoint_restore_test','--no-owner','--clean','--if-exists'], stdin=stream, capture_output=True, timeout=60)
                self.assertEqual(restored.returncode, 0, restored.stderr.decode()[:1000])
                counts = docker('exec',postgres,'psql','-U','postgres','-d','gpoint_restore_test','-Atc',
                    'SELECT (SELECT count(*) FROM agata.messages),(SELECT count(*) FROM agata.tenants),(SELECT id FROM public.backup_fixture)').stdout.strip()
                self.assertEqual(counts, '2|2|42')
                with gate.TenantGuard() as guard:
                    backend = guard.query("SELECT json_build_object('pid',pg_backend_pid());")['pid']
                    docker('exec',postgres,'psql','-U','postgres','-d','gpoint','-Atc',
                        f'SELECT pg_terminate_backend({backend})')
                    self.assertFalse(guard.alive(), 'a live psql process is not proof of a retained database lock')
                with gate.TenantGuard() as guard:
                    self.assertTrue(guard.alive())
                    pid = guard.query("ROLLBACK; SELECT json_build_object('pid',pg_backend_pid());")['pid']
                    self.assertEqual(pid, guard.backend_pid)
                    self.assertFalse(guard.alive(), 'a retained backend without its lock must fail closed')
                def migrate(inventory, guard):
                    # An attempted concurrent tenant insert is refused while the guard is held.
                    blocked = docker('exec', postgres, 'psql', '-U','postgres','-d','gpoint','-v','ON_ERROR_STOP=1','-c',
                        f"SET lock_timeout='100ms'; INSERT INTO agata.tenants VALUES('{uuid4()}')", check=False)
                    self.assertNotEqual(blocked.returncode, 0); self.assertIn('lock timeout', blocked.stderr)
                    gate.migrate_image(IMAGE, inventory, guard)
                facts = gate.guarded_migration(proof, gate.TenantGuard(), migrate)
                self.assertEqual(facts['tenant_count'], 2)
                self.assertNotIn('inventory', facts)
                self.assertEqual(gate.effective_probe(IMAGE)['head'], 'agata0010')
                marked = docker('exec',postgres,'psql','-U','postgres','-d','gpoint','-Atc',
                    'SELECT count(*) FROM agata.messages WHERE incoming_read_at IS NOT NULL').stdout.strip()
                self.assertEqual(marked, '2')
            finally:
                docker('rm', '-f', postgres, check=False)
                docker('network', 'rm', network, check=False)


if __name__ == '__main__': unittest.main()
