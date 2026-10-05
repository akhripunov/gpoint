"""Creates/removes only one ephemeral GPoint test container; never reads production env."""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
SALES = ROOT / "services/gsales"
AGATA = ROOT / "services/agata"
GPOINT = "7508f36f-774a-48aa-8c6b-42f6f160415d"
GSALES = "c4d67889-877e-4d1b-a8d8-597c479d638f"


def install_cleanup_signal_handlers():
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)


def start_test_container(container, password):
    fd, env_path = tempfile.mkstemp(prefix=".gsales-postgres-", suffix=".env")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("POSTGRES_PASSWORD=" + password + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        args = [
            "docker", "run", "-d", "--name", container,
            "-p", "127.0.0.1::5432", "--env-file", env_path,
            "pgvector/pgvector:0.8.6-pg16",
        ]
        try:
            result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("disposable_postgres_start_failed") from None
        if result.returncode != 0:
            raise RuntimeError("disposable_postgres_start_failed")
        return Path(env_path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(env_path).unlink(missing_ok=True)
        raise


def main():
    install_cleanup_signal_handlers()
    real_codex = "--real-codex" in sys.argv
    if real_codex:
        import fcntl
        lock_path = Path.home() / "Library/Application Support/GPoint/agata-codex-worker/locks/pool.lock"
        with lock_path.open("rb") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("REAL_CODEX_REHEARSAL=BLOCKED: installed worker owns common lock; separate owner permission required to temporarily stop it")
                return 2
    container = "gpoint-gsales-test-" + uuid4().hex[:10]
    password = uuid4().hex
    password_file = None
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGATA_", "GSALES_"))}
    env["PYTHONPATH"] = str(SALES)
    if real_codex:
        env["GSALES_QA_REAL_CODEX"] = "true"
        env["AGATA_CODEX_JOB_TIMEOUT_SECONDS"] = "90"
    try:
        password_file = start_test_container(container, password)
        port = (
            subprocess.check_output(["docker", "port", container, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        database_host = os.environ.get("GSALES_TEST_DB_HOST", "127.0.0.1")
        admin_base = f"postgresql://postgres:{password}@{database_host}:{port}/postgres"
        for _ in range(45):
            try:
                with psycopg.connect(admin_base, connect_timeout=1, autocommit=True) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.Error:
                time.sleep(0.5)
        else:
            raise RuntimeError("disposable_postgres_not_ready")
        # Production migrator rejects every other database identity. This name
        # belongs only to the unique disposable container created above.
        dbname = "gpoint"
        with psycopg.connect(admin_base, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(dbname)))
            for role in ("gpoint_test_runtime", "gsales_agata_runtime", "gsales_runtime"):
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
        admin = admin_base.rsplit("/", 1)[0] + "/" + dbname
        env["GSALES_TEST_ADMIN_DSN"] = admin
        for role, var in (
            ("gpoint_test_runtime", "GSALES_TEST_GPOINT_DSN"),
            ("gsales_agata_runtime", "GSALES_TEST_AGATA_DSN"),
            ("gsales_runtime", "GSALES_TEST_RUNTIME_DSN"),
        ):
            env[var] = admin.replace("postgres:", role + ":")
        env.update(
            AGATA_DATABASE_URL=admin.replace("postgresql://", "postgresql+psycopg://"),
            AGATA_TENANT_ID=GPOINT,
            GSALES_TENANT_ID=GSALES,
        )
        key = base64.b64encode(b"a" * 32).decode()
        for suffix in (
            "MESSAGE_AEAD_KEY",
            "REQUEST_FINGERPRINT_HMAC_KEY",
            "RETRIEVAL_QUERY_HMAC_KEY",
            "SHADOW_CANDIDATE_HMAC_KEY",
            "RESPONSE_FINGERPRINT_HMAC_KEY",
        ):
            env["AGATA_" + suffix] = key
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "agata0010"],
            cwd=AGATA,
            env={**env, "PYTHONPATH": str(AGATA)},
            check=True,
        )
        with psycopg.connect(admin) as conn:
            conn.execute("CREATE TABLE public.gsales_public_probe (id int PRIMARY KEY)")
            conn.execute("INSERT INTO agata.tenants(id) VALUES (%s)", (GSALES,))
            for tenant in (GPOINT, GSALES):
                conn.execute(
                    "INSERT INTO agata.users(tenant_id,external_id) VALUES (%s,%s)",
                    (tenant, "isolation-probe"),
                )
            for role in ("gpoint_test_runtime", "gsales_agata_runtime"):
                conn.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA agata TO {}").format(sql.Identifier(role))
                )
                conn.execute(
                    sql.SQL(
                        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agata TO {}"
                    ).format(sql.Identifier(role))
                )
                conn.execute(
                    sql.SQL("REVOKE INSERT,UPDATE,DELETE ON agata.alembic_version FROM {}").format(
                        sql.Identifier(role)
                    )
                )
        # Reproduce the actual legacy owner threat: FORCE RLS does not stop a
        # GPoint application login that owns the schema/tables from disabling it.
        with psycopg.connect(admin) as conn:
            conn.execute("ALTER SCHEMA agata OWNER TO gpoint_test_runtime")
            conn.execute("""DO $$ DECLARE obj record; BEGIN
                FOR obj IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                  WHERE n.nspname='agata' AND c.relkind='r' LOOP
                  EXECUTE format('ALTER TABLE agata.%I OWNER TO gpoint_test_runtime',obj.relname);
                END LOOP;
                FOR obj IN SELECT format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) AS identity
                  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                  WHERE n.nspname='agata' AND p.prokind='f' LOOP
                  EXECUTE format('ALTER FUNCTION %s OWNER TO gpoint_test_runtime',obj.identity);
                END LOOP;
            END $$""")
        with psycopg.connect(env["GSALES_TEST_GPOINT_DSN"]) as conn:
            conn.execute("ALTER TABLE agata.users DISABLE ROW LEVEL SECURITY")
            assert conn.execute("SELECT count(*) FROM agata.users").fetchone()[0] == 2
            # Restore baseline guards so subsequent tests also reproduce the
            # separate tenant GUC spoof rather than disabled RLS.
            conn.execute("ALTER TABLE agata.users ENABLE ROW LEVEL SECURITY")
            conn.execute("ALTER TABLE agata.users FORCE ROW LEVEL SECURITY")
        print("LEGACY_GPOINT_OWNER_RLS_BYPASS_REPRODUCED", flush=True)
        # A concrete negative test before the fix: the same GSALES login can
        # read GPOINT users merely by setting a GUC. New migration must close it.
        with psycopg.connect(env["GSALES_TEST_AGATA_DSN"]) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (GPOINT,))
            assert (
                conn.execute(
                    "SELECT count(*) FROM agata.users WHERE tenant_id=%s", (GPOINT,)
                ).fetchone()[0]
                == 1
            )
        print("LEGACY_TENANT_CONTEXT_SPOOF_REPRODUCED", flush=True)
        env["AGATA_ROLE_TENANT_BINDINGS"] = json.dumps(
            {"gpoint_test_runtime": GPOINT, "gsales_agata_runtime": GSALES, "gsales_runtime": GSALES}
        )
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=AGATA,
            env={**env, "PYTHONPATH": str(AGATA)},
            check=True,
        )
        env["GSALES_MIGRATION_DATABASE_URL"] = admin
        # Exercise the real guarded migrator, never bypass its identity/binding
        # checks by executing SQL directly. Replays must retain the ledger.
        rehearsal_sha = "a" * 40
        for _ in range(2):
            applied = subprocess.run(
                [sys.executable, "-m", "app.db.migrate", "--apply",
                 "--candidate", rehearsal_sha, "--owner-approved-sha", rehearsal_sha],
                cwd=SALES, env=env, capture_output=True, text=True, timeout=60,
            )
            if applied.returncode:
                raise RuntimeError("disposable_gsales_migrator_failed_details_withheld")
        with psycopg.connect(admin) as conn:
            ledger = conn.execute("SELECT version,sha256 FROM gsales_meta.migrations ORDER BY version").fetchall()
            checksum = hashlib.sha256((SALES / "migrations/001_gsales.sql").read_bytes()).hexdigest()
            if ledger != [("001_gsales", checksum)]:
                raise RuntimeError("disposable_gsales_migration_ledger_mismatch")
        print("GSALES_PRODUCTION_MIGRATOR_REPLAY=PASS", flush=True)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_shared_worker_http.py" if real_codex else "tests"], cwd=SALES, env=env)
        if result.returncode or real_codex:
            return result.returncode
        # Run shared AGATA HTTP/security tests in a separate module namespace.
        env["AGATA_GSALES_TEST_DATABASE_URL"] = env["GSALES_TEST_AGATA_DSN"].replace(
            "postgresql://", "postgresql+asyncpg://"
        )
        env["AGATA_GSALES_TEST_ADMIN_DSN"] = admin
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_sales_postgres.py"],
            cwd=AGATA,
            env={**env, "PYTHONPATH": str(AGATA)},
        )
        if result.returncode:
            return result.returncode
        # Full pre-existing Core PostgreSQL regression suite, in distinct fresh
        # databases. Its fixture administration never uses application roles.
        with psycopg.connect(admin_base, autocommit=True) as conn:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE ci_owner LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT"
                ).format(sql.Literal(password))
            )
            for name in ("gpoint_core_agata_test", "rls_core_agata_test"):
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER ci_owner").format(sql.Identifier(name))
                )
        for name in ("gpoint_core_agata_test", "rls_core_agata_test"):
            dsn = admin_base.rsplit("/", 1)[0] + "/" + name
            with psycopg.connect(dsn) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        regression = {
            **env,
            "PYTHONPATH": str(AGATA),
            "AGATA_ROLE_TENANT_BINDINGS": json.dumps({"ci_owner": GPOINT}),
            "AGATA_INTERNAL_API_TOKEN": "disposable-regression-token",
            "AGATA_TEST_DATABASE_URL": admin_base.rsplit("/", 1)[0].replace(
                "postgresql://", "postgresql+psycopg://"
            )
            + "/gpoint_core_agata_test",
            "AGATA_RLS_TEST_DATABASE_URL": admin_base.rsplit("/", 1)[0]
            .replace("postgres:", "ci_owner:")
            .replace("postgresql://", "postgresql+psycopg://")
            + "/rls_core_agata_test",
        }
        # Keep the dedicated GSALES test DSN to run every Core test without SKIP.
        regression["AGATA_DATABASE_URL"] = regression["AGATA_TEST_DATABASE_URL"].replace(
            "+psycopg", "+asyncpg"
        )
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_postgres_baseline.py"],
            cwd=AGATA,
            env=regression,
        )
        if result.returncode:
            return result.returncode
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--ignore=tests/test_postgres_baseline.py", "-rs"],
            cwd=AGATA,
            env=regression,
        )
        return result.returncode
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if password_file is not None:
            password_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
