"""Real candidate migrator and protected backup against disposable PostgreSQL only.

Prepare supplies GSALES_MIGRATION_TEST_IMAGE after building the exact candidate.
No host or production database configuration is read by this rehearsal.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("GSALES_MIGRATION_TEST_IMAGE")


@unittest.skipUnless(IMAGE, "Prepare must supply the exact G Sales candidate image")
class GSalesMigrationDockerTests(unittest.TestCase):
    def setUp(self):
        def interrupt(_signum, _frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.signal(signum, interrupt)
            self.addCleanup(signal.signal, signum, previous)

    def test_migrator_idempotency_checksum_and_complete_backup_restore(self):
        spec = importlib.util.spec_from_file_location("backup_gate", ROOT / "scripts/agata-production-migration.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        suffix = uuid4().hex[:12]
        network, postgres = "gpoint-sales-migrate-net-" + suffix, "gpoint-sales-migrate-pg-" + suffix
        migrator = "gpoint-sales-migrator-" + suffix
        tenant, password = str(uuid4()), uuid4().hex
        sha = "a" * 40  # Approval inputs belong only to this disposable rehearsal.

        def docker(*args, input=None, check=True):
            try:
                result = subprocess.run(["docker", *args], input=input, text=True, capture_output=True, timeout=180)
            except (OSError, subprocess.TimeoutExpired):
                self.fail("disposable migration Docker command unavailable or timed out")
            if check and result.returncode:
                self.fail("disposable migration Docker command failed; credential details withheld")
            return result

        def query(statement):
            return docker("exec", "-i", postgres, "psql", "-XqAt", "-U", "postgres", "-d", "gpoint",
                          "-v", "ON_ERROR_STOP=1", input=statement).stdout.strip()

        with tempfile.TemporaryDirectory(prefix="gpoint-sales-migration-") as scratch:
            env = Path(scratch) / "migration.env"
            env.write_text(f"POSTGRES_USER=postgres\nPOSTGRES_PASSWORD={password}\nPGPASSWORD={password}\nGSALES_TENANT_ID={tenant}\n"
                           f"GSALES_MIGRATION_DATABASE_URL=postgresql://postgres:{password}@{postgres}:5432/gpoint\n")
            env.chmod(0o600)

            def migrate(check=True):
                return docker("run", "--rm", "--name", migrator, "--network", network, "--env-file", str(env),
                              "--entrypoint", "python", IMAGE, "-m", "app.db.migrate", "--apply",
                              "--candidate", sha, "--owner-approved-sha", sha, check=check)

            try:
                docker("network", "create", network)
                docker("run", "-d", "--name", postgres, "--network", network, "--env-file", str(env),
                       "pgvector/pgvector:0.8.6-pg16")
                for _ in range(60):
                    if not docker("exec", postgres, "psql", "-h", "127.0.0.1", "-U", "postgres", "-Atc", "SELECT 1", check=False).returncode:
                        break
                    time.sleep(0.5)
                else:
                    self.fail("disposable migration PostgreSQL not ready")
                docker("exec", postgres, "createdb", "-U", "postgres", "gpoint")
                query(f"""
                    CREATE ROLE gsales_runtime LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE;
                    CREATE ROLE gpoint_runtime LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE;
                    CREATE SCHEMA agata; CREATE SCHEMA agata_identity;
                    REVOKE ALL ON SCHEMA agata_identity FROM PUBLIC;
                    CREATE TABLE agata.tenants(id uuid PRIMARY KEY);
                    INSERT INTO agata.tenants VALUES('{tenant}');
                    CREATE TABLE agata_identity.role_tenants(login_role name PRIMARY KEY, tenant_id uuid NOT NULL);
                    INSERT INTO agata_identity.role_tenants VALUES('gsales_runtime','{tenant}');
                    CREATE TABLE agata.conversations(id uuid, tenant_id uuid, source_system text);
                    CREATE TABLE agata.messages(id uuid, tenant_id uuid, conversation_id uuid, role text, scope text, scenario_id uuid);
                    CREATE TABLE public.backup_fixture(id int PRIMARY KEY); INSERT INTO public.backup_fixture VALUES(42);
                """)
                core_catalog_sql = """SELECT n.nspname,c.relname,c.relkind,c.relowner,c.relacl,c.relrowsecurity,c.relforcerowsecurity
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname IN ('agata','agata_identity','public') ORDER BY 1,2;"""
                core_before = query(core_catalog_sql)
                migrate()
                ledger_before = query("SELECT version,sha256,applied_at FROM gsales_meta.migrations ORDER BY version")
                expected_checksum = hashlib.sha256((ROOT / "services/gsales/migrations/001_gsales.sql").read_bytes()).hexdigest()
                self.assertEqual(query("SELECT version,sha256 FROM gsales_meta.migrations"), "001_gsales|" + expected_checksum)
                migrate()
                self.assertEqual(query("SELECT version,sha256,applied_at FROM gsales_meta.migrations ORDER BY version"), ledger_before)
                self.assertEqual(query(core_catalog_sql), core_before, "G Sales DDL changed Core objects or privileges")
                self.assertEqual(query("SELECT count(*) FROM pg_tables WHERE schemaname='gsales'"), "6")
                self.assertEqual(query("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='gsales' AND c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity"), "6")
                self.assertEqual(query("SELECT has_schema_privilege('gsales_runtime','gsales_meta','USAGE'),has_schema_privilege('gpoint_runtime','gsales','USAGE'),has_schema_privilege('gsales_runtime','agata_identity','USAGE')"), "f|f|f")
                self.assertEqual(query("SELECT has_table_privilege('gsales_runtime','gsales.sellers','DELETE'),has_table_privilege('gsales_runtime','gsales_meta.migrations','SELECT')"), "f|f")
                query("UPDATE gsales_meta.migrations SET sha256=repeat('0',64)")
                rejected = migrate(check=False)
                self.assertNotEqual(rejected.returncode, 0, "changed migration checksum was accepted")
                self.assertIn("migration_checksum_changed", rejected.stderr)
                query(f"UPDATE gsales_meta.migrations SET sha256='{expected_checksum}'")
                foreign_tenant = str(uuid4())
                query(f"INSERT INTO gsales.sellers(tenant_id,agata_character_id,display_name) VALUES"
                      f"('{tenant}','{uuid4()}','own seller'),('{foreign_tenant}','{uuid4()}','foreign seller')")
                own_rows = docker("exec", "--env-file", str(env), "-i", postgres, "psql", "-XqAt", "-h", "127.0.0.1",
                                  "-U", "gsales_runtime", "-d", "gpoint", "-v", "ON_ERROR_STOP=1",
                                  input=f"SET app.tenant_id='{foreign_tenant}'; SELECT display_name FROM gsales.sellers")
                self.assertEqual(own_rows.stdout.strip(), "own seller", "runtime tenant spoof exposed foreign G Sales data")
                forbidden = docker("exec", "--env-file", str(env), "-i", postgres, "psql", "-XqAt", "-h", "127.0.0.1",
                                   "-U", "gpoint_runtime", "-d", "gpoint", "-v", "ON_ERROR_STOP=1",
                                   input="SELECT display_name FROM gsales.sellers", check=False)
                self.assertNotEqual(forbidden.returncode, 0, "Core role gained access to G Sales data")
                gate.DB_CONTAINER, gate.DB_NETWORK = postgres, network
                backup = gate.complete_backup(sha, directory=Path(scratch) / "backup")
                self.assertEqual(backup["status"], "COMPLETE_PROTECTED")
                self.assertEqual(Path(backup["path"]).stat().st_mode & 0o777, 0o600)
                docker("exec", postgres, "dropdb", "-U", "postgres", "gpoint")
                docker("exec", postgres, "createdb", "-U", "postgres", "gpoint")
                with Path(backup["path"]).open("rb") as stream:
                    restored = subprocess.run(["docker", "exec", "-i", postgres, "pg_restore", "-U", "postgres",
                                               "-d", "gpoint", "--no-owner", "--clean", "--if-exists"], stdin=stream, capture_output=True, timeout=120)
                self.assertEqual(restored.returncode, 0, "disposable backup restore failed; details withheld")
                # Missing gsales_meta breaks this query (and would rerun non-idempotent DDL).
                self.assertEqual(query("SELECT version,sha256,applied_at FROM gsales_meta.migrations ORDER BY version"), ledger_before)
                self.assertEqual(query("SELECT id FROM public.backup_fixture"), "42")
                self.assertEqual(query("SELECT display_name FROM gsales.sellers ORDER BY display_name"), "foreign seller\nown seller")
                self.assertEqual(query(f"SELECT tenant_id::text FROM agata_identity.role_tenants WHERE login_role='gsales_runtime'"), tenant)
                migrate()
                self.assertEqual(query("SELECT version,sha256,applied_at FROM gsales_meta.migrations ORDER BY version"), ledger_before)
            finally:
                docker("rm", "-f", migrator, check=False)
                docker("rm", "-f", postgres, check=False)
                docker("network", "rm", network, check=False)


if __name__ == "__main__":
    unittest.main()
