import importlib.util
import json
from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eros_native_database", ROOT / "scripts/eros-native-database.py")
DB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DB)


def ledger(version=1, checksum="a" * 96, description="initial"):
    return [{"version": version, "description": description, "success": True, "checksum": checksum}]


def facts(database="eros"):
    return {"name": database, "owner": "eros_ddl", "allowConnect": True,
            "publicConnect": False, "runtimeConnect": True, "migratorConnect": True,
            "runtimeDbCreate": False, "migratorDbCreate": False, "runtimeDbTemp": True,
            "otherConnect": [],
            "marker": "a" * 48, "extension": "0.8.6", "engineOwner": "eros_ddl",
            "clusterSystemIdentifier": "742001",
            "runtimeSchemaUsage": True, "runtimeSchemaCreate": False,
            "migratorSchemaUsage": True, "migratorSchemaCreate": True,
            "runtimePublicSchemaCreate": False,
            "runtimePublicSchemaUsage": True, "migratorPublicSchemaCreate": True,
            "runtimeUnauthorizedTables": 0, "runtimeUnauthorizedSequences": 0, "serverVersionNum": 160001,
            "runtimeForeign": [], "migratorForeign": [], "sizeBytes": 1024,
            "roles": [{"name": "eros_ddl", "login": False, "superuser": False, "createdb": False,
                       "createrole": False, "bypassrls": False, "inherit": False},
                      {"name": "eros_runtime", "login": True, "superuser": False, "createdb": False,
                       "createrole": False, "bypassrls": True, "inherit": False},
                      {"name": "eros_migrator", "login": True, "superuser": False, "createdb": False,
                       "createrole": False, "bypassrls": False, "inherit": False}]}


class ErosNativeDatabaseTests(unittest.TestCase):
    def test_database_inventory_quotes_database_names_as_sql_string_literals(self):
        database = "eros_check_123_1"
        with patch.object(DB, "_docker_psql", return_value=json.dumps(facts(database))) as psql:
            DB._database_facts("a" * 48, database)
        query = psql.call_args.args[1]
        self.assertIn("d.datname='eros_check_123_1'", query)
        self.assertIn("x.datname NOT IN ('eros','eros_check_123_1')", query)
        self.assertNotIn('d.datname="eros_check_123_1"', query)

    def test_acl_gate_accepts_dedicated_database_and_rejects_each_boundary(self):
        with patch.object(DB, "_docker_psql", return_value=json.dumps(facts())):
            self.assertEqual(DB._database_facts("a" * 48)["owner"], "eros_ddl")
        for key, value in (("owner", "postgres"), ("publicConnect", True),
                           ("runtimeForeign", ["gpoint"]), ("migratorForeign", ["postgres"]),
                           ("otherConnect", ["other_role"]),
                           ("runtimeDbCreate", True), ("migratorDbCreate", True),
                           ("runtimeDbTemp", False), ("runtimeSchemaUsage", False),
                           ("runtimeSchemaCreate", True), ("runtimePublicSchemaCreate", True),
                           ("migratorSchemaCreate", False), ("runtimeUnauthorizedTables", 1),
                           ("runtimeUnauthorizedSequences", 1), ("runtimeConnect", False),
                           ("migratorConnect", False),
                           ("marker", "b" * 48), ("engineOwner", "postgres")):
            bad = facts(); bad[key] = value
            with patch.object(DB, "_docker_psql", return_value=json.dumps(bad)):
                with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "identity or ACL"):
                    DB._database_facts("a" * 48)

    def test_role_acl_rejects_superuser_and_wrong_runtime_bypass(self):
        for role_name, key, value in (("eros_runtime", "superuser", True),
                                      ("eros_runtime", "bypassrls", False),
                                      ("eros_migrator", "createdb", True),
                                      ("eros_ddl", "login", True)):
            bad = facts()
            next(row for row in bad["roles"] if row["name"] == role_name)[key] = value
            with patch.object(DB, "_docker_psql", return_value=json.dumps(bad)):
                with self.subTest(role=role_name, key=key), self.assertRaisesRegex(RuntimeError, "role privileges"):
                    DB._database_facts("a" * 48)

    def test_sqlx_ledger_allows_empty_bootstrap_but_rejects_missing_ledger_with_tables(self):
        with patch.object(DB, "_docker_psql", side_effect=["f", "0"]):
            self.assertEqual(DB._ledger(), [])
        with patch.object(DB, "_docker_psql", side_effect=["f", "1"]):
            with self.assertRaisesRegex(RuntimeError, "without its stock SQLx ledger"):
                DB._ledger()

    def test_sqlx_ledger_rejects_failed_rows_and_duplicate_versions(self):
        bad_rows = [ledger()[0] | {"success": False}, ledger()[0], ledger()[0]]
        for rows in ([bad_rows[0]], bad_rows[1:]):
            with patch.object(DB, "_docker_psql", side_effect=["t", json.dumps(rows)]):
                with self.assertRaisesRegex(RuntimeError, "failed or malformed|duplicated"):
                    DB._ledger()

    def test_sqlx_ledger_accepts_stock_sha384_and_rejects_non_sha384_checksums(self):
        with patch.object(DB, "_docker_psql", side_effect=["t", json.dumps(ledger())]):
            self.assertEqual(DB._ledger(), ledger())
        with patch.object(DB, "_docker_psql", side_effect=["t", json.dumps(ledger(checksum="a" * 64))]):
            with self.assertRaisesRegex(RuntimeError, "failed or malformed"):
                DB._ledger()

    def test_migration_delta_accepts_noop_or_append_and_rejects_drift(self):
        old = ledger()
        self.assertEqual(DB._migration_delta(old, old), [])
        self.assertEqual(DB._migration_delta(old, old + ledger(2, "b" * 96, "next")),
                         ledger(2, "b" * 96, "next"))
        with self.assertRaisesRegex(RuntimeError, "changed or removed"):
            DB._migration_delta(old, ledger(1, "c" * 96))
        with self.assertRaisesRegex(RuntimeError, "changed or removed"):
            DB._migration_delta(old, [])
        existing = [ledger(1)[0], ledger(3, "c" * 64, "later")[0]]
        with self.assertRaisesRegex(RuntimeError, "behind the live SQLx head"):
            DB._migration_delta(existing, [existing[0], ledger(2, "b" * 64, "late-old")[0], existing[1]])

    def test_exact_image_run_identity_and_dsn_database_rewrite(self):
        with self.assertRaisesRegex(ValueError, "exact Eros candidate"):
            DB.lifecycle("prepare", "main", "ghcr.io/akhripunov/gpoint-eros@sha256:" + "a" * 64, "1-1")
        with self.assertRaisesRegex(ValueError, "exact Eros candidate"):
            DB.lifecycle("prepare", "a" * 40, "ghcr.io/akhripunov/gpoint-eros:latest", "1-1")
        self.assertEqual(DB._dsn_for_database("postgresql://eros_migrator:p%40ss@gpoint-eros-db:5432/eros", "eros_check_1_1"),
                         "postgresql://eros_migrator:p%40ss@gpoint-eros-db:5432/eros_check_1_1")

    def test_postgres_runtime_identity_requires_live_container_and_persistent_mounts(self):
        mounts = [{"Type": "volume", "Name": "postgres-data", "Source": "/var/lib/docker/volumes/postgres-data",
                   "Destination": "/var/lib/postgresql/data", "RW": True}]
        output = "a" * 64 + "|sha256:" + "b" * 64 + "|" + json.dumps(mounts) + "|true"
        with patch.object(DB, "_run", return_value=type("Result", (), {"stdout": output})()):
            identity = DB._postgres_identity()
        self.assertEqual(identity["containerId"], "a" * 64)
        self.assertEqual(identity["imageId"], "sha256:" + "b" * 64)
        with patch.object(DB, "_run", return_value=type("Result", (), {"stdout": "a" * 64 + "|sha256:" + "b" * 64 + "|[]|true"})()):
            with self.assertRaisesRegex(RuntimeError, "identity is incomplete"):
                DB._postgres_identity()

    def test_isolated_backup_restore_grants_only_runtime_roles_and_cleans_partial_restore(self):
        backup = Path("/protected/eros.dump")
        calls = []
        with patch.object(DB, "_docker_psql", side_effect=lambda database, sql: calls.append((database, sql))), \
             patch.object(DB, "_run_stream") as restore:
            DB._restore_backup_into(backup, "eros_check_123_1")
        self.assertIn("CREATE DATABASE", calls[0][1])
        self.assertIn("REVOKE CONNECT ON DATABASE", calls[1][1])
        self.assertIn("TO eros_runtime, eros_migrator", calls[1][1])
        self.assertIn("pg_restore --exit-on-error", restore.call_args.args[0][-3])
        with patch.object(DB, "_docker_psql", side_effect=[None, RuntimeError("ACL grant failed")]), \
             patch.object(DB, "_run_stream") as restore, patch.object(DB, "_drop_database") as drop:
            with self.assertRaisesRegex(RuntimeError, "ACL grant failed"):
                DB._restore_backup_into(backup, "eros_check_123_1")
        restore.assert_not_called()
        drop.assert_called_once_with("eros_check_123_1")

    def test_apply_skips_schema_apply_when_rehearsal_has_no_pending_migrations(self):
        plan = {"schema": "gpoint-eros-native-db-plan/v1", "candidateSha": "a" * 40,
                "image": "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64,
                "runId": "123-1", "databaseIdentity": "a" * 48,
                "postgresIdentity": {"containerId": "d" * 64, "imageId": "sha256:" + "e" * 64,
                                     "mountsSha256": "f" * 64}, "clusterSystemIdentifier": "742001",
                "backupPath": "/opt/gpoint/backups/eros/native-123-1.dump",
                "backupSha256": hashlib.sha256(b"exact backup").hexdigest(),
                "baselineLedger": ledger(), "baselineLedgerSha256": DB._ledger_digest(ledger()),
                "targetLedger": ledger(), "targetLedgerSha256": DB._ledger_digest(ledger()),
                "pendingMigrations": [], "schemaApply": False, "rollbackCheck": False}
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value=plan["postgresIdentity"]), \
             patch.object(DB, "_database_facts", return_value=facts()), \
             patch.object(DB, "_ledger", return_value=ledger()), \
             patch.object(DB, "BACKUP_DIR", Path("/opt/gpoint/backups/eros")), \
             patch.object(DB, "_run_migrator") as migrate, \
             patch.object(DB, "_load_plan", return_value=plan), \
             patch.object(DB, "_file_digest", return_value=plan["backupSha256"]):
            DB._safe_write_plan = lambda *_: None
            result = DB.lifecycle("apply", "a" * 40, plan["image"], "123-1")
        self.assertFalse(result["schemaApply"])
        migrate.assert_not_called()

    def _run_prepare(self, *, before=None, after=None, mode="prepare"):
        before = ledger() if before is None else before
        after = before if after is None else after
        written = []
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value={"containerId": "d" * 64,
                 "imageId": "sha256:" + "e" * 64, "mountsSha256": "f" * 64}), \
             patch.object(DB, "_database_facts", side_effect=[facts(), facts("eros_check_123_1"), facts("eros_check_123_1")]), \
             patch.object(DB, "_ledger", side_effect=[before, before, after]), \
             patch.object(DB, "_prepare_backup_and_restore", return_value=(Path("/opt/gpoint/backups/eros/native-123-1.dump"), "c" * 64)), \
             patch.object(DB, "_run_migrator") as migrate, \
             patch.object(DB, "_safe_write_plan", side_effect=lambda path, plan: written.append(plan)), \
             patch.object(DB, "_drop_database") as drop:
            if mode == "rollback-check":
                result = DB.lifecycle(mode, "a" * 40, "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64, "123-1")
            else:
                result = DB.lifecycle(mode, "a" * 40, "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64, "123-1")
        return result, written, migrate, drop

    def test_prepare_rehearses_noop_and_pending_upgrade_on_isolated_restore(self):
        plan, written, migrate, drop = self._run_prepare()
        self.assertEqual(plan["pendingMigrations"], [])
        self.assertEqual(len(written), 1)
        migrate.assert_called_once_with("ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64,
                                        "eros_check_123_1", {"EROS_DATABASE_IDENTITY": "a" * 48})
        drop.assert_called_once_with("eros_check_123_1")

    def test_prepare_blocks_before_plan_when_post_migration_acl_drifts(self):
        written = []
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value={"containerId": "d" * 64,
                 "imageId": "sha256:" + "e" * 64, "mountsSha256": "f" * 64}), \
             patch.object(DB, "_database_facts", side_effect=[facts(), facts("eros_check_123_1"),
                 RuntimeError("dedicated Eros database identity or ACL gate failed")]), \
             patch.object(DB, "_ledger", side_effect=[ledger(), ledger(), ledger()]), \
             patch.object(DB, "_prepare_backup_and_restore", return_value=(Path("/opt/gpoint/backups/eros/native-123-1.dump"), "c" * 64)), \
             patch.object(DB, "_run_migrator"), \
             patch.object(DB, "_safe_write_plan", side_effect=lambda path, plan: written.append(plan)), \
             patch.object(DB, "_drop_database") as drop:
            with self.assertRaisesRegex(RuntimeError, "identity or ACL gate failed"):
                DB.lifecycle("prepare", "a" * 40, "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64, "123-1")
        self.assertEqual(written, [])
        drop.assert_called_once_with("eros_check_123_1")

    def test_live_apply_runs_only_rehearsed_delta_and_verifies_exact_ledger(self):
        old = ledger()
        added = ledger(2, "b" * 64, "next")
        target = old + added
        plan = {"schema": "gpoint-eros-native-db-plan/v1", "candidateSha": "a" * 40,
                "image": "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64,
                "runId": "123-1", "databaseIdentity": "a" * 48,
                "postgresIdentity": {"containerId": "d" * 64, "imageId": "sha256:" + "e" * 64,
                                     "mountsSha256": "f" * 64}, "clusterSystemIdentifier": "742001",
                "backupPath": "/opt/gpoint/backups/eros/native-123-1.dump",
                "backupSha256": hashlib.sha256(b"exact backup").hexdigest(),
                "baselineLedger": old, "baselineLedgerSha256": DB._ledger_digest(old),
                "targetLedger": target, "targetLedgerSha256": DB._ledger_digest(target),
                "pendingMigrations": added, "schemaApply": True, "rollbackCheck": False}
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value=plan["postgresIdentity"]), \
             patch.object(DB, "_database_facts", side_effect=[facts(), facts()]), \
             patch.object(DB, "_ledger", side_effect=[old, target]), \
             patch.object(DB, "BACKUP_DIR", Path("/opt/gpoint/backups/eros")), \
             patch.object(DB, "_load_plan", return_value=plan), \
             patch.object(DB, "_file_digest", return_value=plan["backupSha256"]), \
             patch.object(DB, "_run_migrator") as migrate:
            result = DB.lifecycle("apply", "a" * 40, plan["image"], "123-1")
        self.assertEqual(result["targetLedger"], target)
        migrate.assert_called_once_with(plan["image"], "eros", {"EROS_DATABASE_IDENTITY": "a" * 48})

    def test_live_apply_rejects_database_drift_or_changed_backup_before_migration(self):
        old = ledger()
        plan = {"schema": "gpoint-eros-native-db-plan/v1", "candidateSha": "a" * 40,
                "image": "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64,
                "runId": "123-1", "databaseIdentity": "a" * 48,
                "postgresIdentity": {"containerId": "d" * 64, "imageId": "sha256:" + "e" * 64,
                                     "mountsSha256": "f" * 64}, "clusterSystemIdentifier": "742001",
                "backupPath": "/opt/gpoint/backups/eros/native-123-1.dump",
                "backupSha256": "c" * 64, "baselineLedger": old,
                "baselineLedgerSha256": DB._ledger_digest(old), "targetLedger": old,
                "targetLedgerSha256": DB._ledger_digest(old), "pendingMigrations": [],
                "schemaApply": False, "rollbackCheck": False}
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value={"containerId": "d" * 64,
                 "imageId": "sha256:" + "e" * 64, "mountsSha256": "f" * 64}), \
             patch.object(DB, "BACKUP_DIR", Path("/opt/gpoint/backups/eros")), \
             patch.object(DB, "_load_plan", return_value=plan), \
             patch.object(DB, "_run_migrator") as migrate:
            with patch.object(DB, "_file_digest", return_value="d" * 64):
                with self.assertRaisesRegex(RuntimeError, "backup is missing or changed"):
                    DB.lifecycle("apply", "a" * 40, plan["image"], "123-1")
            migrate.assert_not_called()
            with patch.object(DB, "_file_digest", return_value="c" * 64), \
                 patch.object(DB, "_database_facts", return_value=facts()), \
                 patch.object(DB, "_ledger", return_value=ledger(1, "d" * 64)):
                with self.assertRaisesRegex(RuntimeError, "changed after isolated migration rehearsal"):
                    DB.lifecycle("apply", "a" * 40, plan["image"], "123-1")
            migrate.assert_not_called()


        newer = ledger(2, "b" * 64, "next")
        plan, written, migrate, drop = self._run_prepare(after=ledger() + newer)
        self.assertEqual(plan["pendingMigrations"], newer)
        self.assertTrue(plan["schemaApply"])
        self.assertEqual(len(written), 1)
        drop.assert_called_once_with("eros_check_123_1")

    def test_rollback_compatibility_requires_zero_migrations_and_records_no_downgrade_evidence(self):
        plan, written, migrate, drop = self._run_prepare(mode="rollback-check")
        self.assertTrue(plan["rollbackCheck"])
        self.assertEqual(plan["pendingMigrations"], [])
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0]["rollbackCheck"])
        drop.assert_called_once_with("eros_check_123_1")
        with self.assertRaisesRegex(RuntimeError, "image rollback is incompatible"):
            self._run_prepare(mode="rollback-check", after=ledger() + ledger(2, "b" * 64, "next"))

    def test_eros_image_rollback_verification_requires_unchanged_database_and_recovery_backup(self):
        old = ledger()
        plan = {"schema": "gpoint-eros-native-db-plan/v1", "candidateSha": "a" * 40,
                "image": "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64,
                "runId": "123-1", "databaseIdentity": "a" * 48,
                "postgresIdentity": {"containerId": "d" * 64, "imageId": "sha256:" + "e" * 64,
                                     "mountsSha256": "f" * 64}, "clusterSystemIdentifier": "742001",
                "backupPath": "/opt/gpoint/backups/eros/native-123-1.dump",
                "backupSha256": "c" * 64, "baselineLedger": old,
                "targetLedger": old, "pendingMigrations": [], "rollbackCheck": True}
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value=plan["postgresIdentity"]), \
             patch.object(DB, "_database_facts", return_value=facts()), \
             patch.object(DB, "_ledger", return_value=old), \
             patch.object(DB, "_load_plan", return_value=plan), \
             patch.object(DB, "_file_digest", return_value="c" * 64), \
             patch.object(DB, "_run_migrator") as migrate:
            result = DB.lifecycle("verify-unchanged", "a" * 40, plan["image"], "123-1")
        self.assertTrue(result["rollbackCheck"])
        migrate.assert_not_called()
        with patch.object(DB, "_read_env", return_value={"EROS_DATABASE_IDENTITY": "a" * 48}), \
             patch.object(DB, "_postgres_identity", return_value=plan["postgresIdentity"]), \
             patch.object(DB, "_database_facts", return_value=facts()), \
             patch.object(DB, "_ledger", return_value=ledger(2, "d" * 64, "unexpected")), \
             patch.object(DB, "_load_plan", return_value=plan), \
             patch.object(DB, "_file_digest", return_value="c" * 64):
            with self.assertRaisesRegex(RuntimeError, "database changed during image rollback"):
                DB.lifecycle("verify-unchanged", "a" * 40, plan["image"], "123-1")


if __name__ == "__main__":
    unittest.main()
