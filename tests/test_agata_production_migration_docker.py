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
GPOINT_TENANT = '81a4ba28-6de5-4b03-8332-0661ba4d46f4'


@unittest.skipUnless(IMAGE, 'run in Prepare after exact AGATA image build')
class DockerMigrationProtocol(unittest.TestCase):
    def test_full_inventory_guard_restricted_image_and_complete_backup(self):
        spec = importlib.util.spec_from_file_location('migration', ROOT / 'scripts/agata-production-migration.py')
        gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
        suffix = uuid4().hex[:12]
        network, postgres = 'gpoint-inventory-net-' + suffix, 'gpoint-inventory-pg-' + suffix
        tenant1, tenant2 = GPOINT_TENANT, str(uuid4())
        def docker(*args, input=None, check=True):
            result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True, timeout=180)
            if check and result.returncode: self.fail('isolated Docker rehearsal command failed: ' + result.stderr[:1500])
            return result
        with tempfile.TemporaryDirectory(prefix='gpoint-inventory-') as scratch:
            env = Path(scratch) / 'test.env'
            migration_env = Path(scratch) / 'migration.env'
            # Migration uses only this disposable cluster administrator. Runtime
            # roles remain separate, non-bypass, and tenant-bound below.
            bindings = json.dumps({'gpoint': tenant1, 'gpoint_runtime': tenant1, 'gsales_runtime': tenant2})
            env.write_text(
                f'AGATA_DATABASE_URL=postgresql+asyncpg://gpoint:disposable-ci-password@{postgres}:5432/gpoint\n'
                f'AGATA_TENANT_ID={tenant1}\nAGATA_ROLE_TENANT_BINDINGS={bindings}\n'
            )
            env.chmod(0o600)
            migration_env.write_text(f'AGATA_MIGRATOR_DATABASE_URL=postgresql+psycopg://postgres:disposable-ci-password@{postgres}:5432/gpoint\n')
            migration_env.chmod(0o600)
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
                       "CREATE ROLE gpoint LOGIN PASSWORD 'disposable-ci-password' NOSUPERUSER NOBYPASSRLS; "
                       "CREATE ROLE gpoint_runtime LOGIN PASSWORD 'disposable-ci-password' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB; "
                       "CREATE ROLE gsales_runtime LOGIN PASSWORD 'disposable-ci-password' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB")
                docker('exec', postgres, 'createdb', '-U', 'postgres', '-O', 'gpoint', 'gpoint')
                sql = f"""
                CREATE SCHEMA agata AUTHORIZATION gpoint;
                CREATE TABLE agata.tenants(id uuid PRIMARY KEY);
                CREATE TABLE agata.conversations(id uuid PRIMARY KEY,tenant_id uuid,source_system text);
                CREATE TABLE agata.messages(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,character_id uuid,
                  conversation_id uuid,scope text,role text,scenario_id uuid,deleted_at timestamptz);
                CREATE TABLE agata.outbox_jobs(id uuid PRIMARY KEY,tenant_id uuid NOT NULL,job_type text NOT NULL,
                  CONSTRAINT ck_outbox_job_type_allowlist CHECK (job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','memory_graph_sync','response_retry')));
                CREATE TABLE agata.regional_policy_profiles(
                  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,
                  jurisdiction text NOT NULL,market text NOT NULL,
                  version integer NOT NULL,status text NOT NULL,effective_from timestamptz NOT NULL,
                  effective_to timestamptz,rules jsonb NOT NULL,
                  UNIQUE(tenant_id,jurisdiction,market,version));
                CREATE TABLE agata.alembic_version(version_num varchar(32) PRIMARY KEY);
                INSERT INTO agata.alembic_version VALUES('agata0009');
                CREATE TABLE public.backup_fixture(id integer); INSERT INTO public.backup_fixture VALUES(42);
                INSERT INTO agata.tenants VALUES('{tenant1}'),('{tenant2}');
                INSERT INTO agata.conversations SELECT id,id,'gpoint' FROM agata.tenants;
                INSERT INTO agata.messages SELECT id,id,id,id,id,'real_world','assistant',NULL,NULL FROM agata.tenants;
                INSERT INTO agata.regional_policy_profiles
                  (id,tenant_id,jurisdiction,market,version,status,effective_from,rules)
                VALUES('{uuid4()}','{tenant1}','GLOBAL','GPOINT',1,'active',now(),
                  '{{"ai_disclosure_required":true,"policy_marker":"existing GPOINT policy"}}');
                """
                for table in ('tenants', 'conversations', 'messages', 'outbox_jobs', 'regional_policy_profiles', 'alembic_version'):
                    sql += f'ALTER TABLE agata.{table} OWNER TO gpoint;'
                for table in ('tenants', 'conversations', 'messages', 'outbox_jobs', 'regional_policy_profiles'):
                    sql += f"ALTER TABLE agata.{table} ENABLE ROW LEVEL SECURITY; ALTER TABLE agata.{table} FORCE ROW LEVEL SECURITY; CREATE POLICY tenant_policy ON agata.{table} USING ({'id' if table == 'tenants' else 'tenant_id'}=nullif(current_setting('app.tenant_id',true),'')::uuid);"
                docker('exec', '-i', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', input=sql)
                gate.DB_CONTAINER, gate.DB_NETWORK, gate.ENV_FILE = postgres, network, str(env)
                gate.MIGRATION_ENV_FILE = str(migration_env)
                self.assertEqual(gate.psql_args()[2], postgres,
                                 'default PostgreSQL helpers must use the active disposable container')
                proof = gate.effective_probe(IMAGE)
                self.assertTrue(proof['privileged']); self.assertTrue(proof['force_rls'])
                census = gate.authoritative_census()
                self.assertIn(proof['server_address'], census['cluster_addresses'], repr((proof['server_address'], census['cluster_addresses'], proof['database_oid'], census['database_oid'])))
                readiness = gate.validate_readiness(proof, census)
                self.assertEqual(readiness['tenant_count'], 2)
                refused = docker('run', '--rm', '--network', network, '--env-file', str(env),
                                 '-e', f'AGATA_DATABASE_URL=postgresql+asyncpg://gpoint:disposable-ci-password@{postgres}:5432/gpoint', IMAGE,
                                 'alembic', 'upgrade', 'head', check=False)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn('requires a complete AGATA_MIGRATION_TENANT_IDS inventory for a non-bypass migrator', refused.stderr)
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
                    self.assertTrue(guard.alive(), 'session advisory lock survives transaction rollback')
                    released = guard.query(
                        "SELECT json_build_object('released',pg_advisory_unlock(1196441417,1314150988));")
                    self.assertTrue(released['released'])
                    self.assertFalse(guard.alive(), 'released advisory lock must fail closed')
                def migrate(inventory, guard):
                    # The snapshot's relation lock is closed before DDL; a
                    # transient insert can run, while the migration re-censuses
                    # under ACCESS EXCLUSIVE inside the migration transaction.
                    transient = docker('exec', postgres, 'psql', '-U','postgres','-d','gpoint','-v','ON_ERROR_STOP=1','-c',
                        f"BEGIN; SET LOCAL lock_timeout='100ms'; INSERT INTO agata.tenants VALUES('{uuid4()}'); ROLLBACK", check=False)
                    self.assertEqual(transient.returncode, 0, transient.stderr)
                    try:
                        gate.migrate_image(IMAGE, inventory, guard)
                    except RuntimeError:
                        suffix = IMAGE.rsplit(':', 1)[1][:12]
                        diagnostic = docker('run', '--rm', '--name', 'gpoint-agata-migration-' + suffix,
                            '--network', network, *gate.migration_env_args(), '-e', 'AGATA_MIGRATION_TENANT_IDS=' + inventory,
                            '-e', 'PGOPTIONS=-c lock_timeout=15000 -c statement_timeout=120000', IMAGE,
                            'alembic', 'upgrade', 'head', check=False)
                        relevant = [line for line in diagnostic.stderr.splitlines()
                                    if 'psycopg.errors.' in line or 'sqlalchemy.exc.' in line or 'RuntimeError:' in line]
                        raise AssertionError('disposable migration diagnostic: ' + ' | '.join(relevant[-4:]))
                facts = gate.guarded_migration(proof, gate.TenantGuard(), migrate)
                self.assertEqual(facts['tenant_count'], 2)
                self.assertNotIn('inventory', facts)
                self.assertEqual(gate.effective_probe(IMAGE)['head'], 'agata0012')
                owners = docker('exec', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint', '-Atc',
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='agata' AND r.rolname IN ('gpoint','gpoint_runtime','gsales_runtime')").stdout.strip()
                self.assertEqual(owners, '0', 'tenant runtime still owns protected AGATA objects')
                for statement in ('ALTER TABLE agata.tenants DISABLE ROW LEVEL SECURITY',
                                  'DROP TABLE agata.tenants CASCADE',
                                  'CREATE TABLE agata.runtime_escape_fixture(id int)'):
                    denied = docker('exec', '-e', 'PGPASSWORD=disposable-ci-password', postgres,
                        'psql', '-h', '127.0.0.1', '-U', 'gpoint', '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1',
                        '-Atc', statement, check=False)
                    self.assertNotEqual(denied.returncode, 0, 'Core runtime can alter protected AGATA schema/RLS')
                marked = docker('exec',postgres,'psql','-U','postgres','-d','gpoint','-Atc',
                    'SELECT count(*) FROM agata.messages WHERE incoming_read_at IS NOT NULL').stdout.strip()
                self.assertEqual(marked, '2')
                preserved = docker('exec', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint', '-Atc',
                    f"SELECT version || ':' || status || ':' || (rules->>'policy_marker') "
                    f"FROM agata.regional_policy_profiles WHERE tenant_id='{tenant1}' "
                    "ORDER BY version").stdout.strip().splitlines()
                self.assertEqual(preserved, [
                    '1:retired:existing GPOINT policy',
                    '2:active:existing GPOINT policy',
                ])
                docker('exec', postgres, 'psql', '-U', 'postgres', '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', '-c',
                    'GRANT USAGE ON SCHEMA agata TO gpoint_runtime,gsales_runtime; '
                    'GRANT EXECUTE ON FUNCTION agata.runtime_tenant_id() TO gpoint_runtime,gsales_runtime; '
                    'GRANT SELECT ON agata.tenants,agata.regional_policy_profiles TO gpoint_runtime,gsales_runtime')
                for role, owned, foreign in (('gpoint_runtime', tenant1, tenant2), ('gsales_runtime', tenant2, tenant1)):
                    own = docker('exec', '-e', 'PGPASSWORD=disposable-ci-password', postgres,
                        'psql', '-h', '127.0.0.1', '-U', role, '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', '-Atc',
                        f"SET app.tenant_id='{owned}'; SELECT count(*) FROM agata.tenants")
                    cross = docker('exec', '-e', 'PGPASSWORD=disposable-ci-password', postgres,
                        'psql', '-h', '127.0.0.1', '-U', role, '-d', 'gpoint', '-v', 'ON_ERROR_STOP=1', '-Atc',
                        f"SET app.tenant_id='{foreign}'; SELECT count(*) FROM agata.tenants")
                    self.assertEqual(own.stdout.strip().splitlines()[-1], '1')
                    self.assertEqual(cross.stdout.strip().splitlines()[-1], '0')
            finally:
                docker('rm', '-f', postgres, check=False)
                docker('network', 'rm', network, check=False)


if __name__ == '__main__': unittest.main()
