#!/usr/bin/env python3
"""Fail-closed SQLx lifecycle gate used by the existing Native Deploy workflow.

The helper runs on the trusted VPS with the selected stock image. It snapshots
the dedicated Eros database, rehearses that image's migrations on an isolated
restore, and applies only the rehearsed delta to production. It never rolls a
database back.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit

CONTAINER = "shared_postgres_1"
ENV_PATH = Path("/opt/gpoint/env/eros.env")
BACKUP_DIR = Path("/opt/gpoint/backups/eros")
AGE_IDENTITY = Path("/opt/gpoint/env/gpoint-backup.agekey")
AGE_RECIPIENT = re.compile(r"age1[0-9a-z]{58}\Z")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^ghcr\.io/akhripunov/gpoint-eros@sha256:[0-9a-f]{64}$")
RUN_ID = re.compile(r"^[0-9]{1,20}-[0-9]{1,8}$")
DB_NAME = re.compile(r"^eros_[a-z0-9_]{1,48}$")
TEST_MEMORY_COUNTS = {
    "engine.companion_memories": 476,
    "engine.relationship_journal": 11,
}
TEST_MEMORY_TABLE_NAMES = tuple(name.split(".", 1)[1] for name in TEST_MEMORY_COUNTS)

# Signed upstream release tags were verified against these commits. Pin the
# published multi-arch manifests so the legacy SQLx history is validated by
# stock Eros migrators, never by a local imitation of SQLx.
EROS_V231_SHA = "fd72b0ff1c145d4bdd9cc27b731e9e6512dc038b"
EROS_V231_IMAGE = "ghcr.io/etherfunlab/eros-engine@sha256:0b2b29a989e7795058e00956712bba0e4be037817253fb4ffe7f5194dc838bf8"
# SHA-256 over canonical [version, description, SHA-384 checksum] triples from
# the official v2.3.1 SQL, using the descriptions stored by SQLx (spaces
# instead of filename underscores). Covers the v2.2.1 and v2.3.1 checkpoints.
EROS_V221_HISTORY_SHA256 = "191bc7096249009abb8b4ab28987758663682af0f872f25e6b5d88740960d687"
EROS_V231_HISTORY_SHA256 = "eb3911a64ca49c666cc7ac3f30e965fbeca39de1f25a326d17deb35e8431f55f"
EROS_V250_SHA = "d12172de0e674b9f6fbe8cf78ebbb3338b746a7b"
EROS_V250_IMAGE = "ghcr.io/etherfunlab/eros-engine@sha256:bf9a887f7a1a12816957eaf83b4ad0fde7d581e2795ad937d3ee33b95f3065a5"
EROS_V270_SHA = "83a54d1dba57874d55ecaa9756473f126b62844f"
EROS_V281_SHA = "c7d63d77e9b52d7ae4952b8374512f571da065d5"
EROS_V270_HISTORY = {
    0: ("baseline", "36dee22457e174da8003b5efe9730bd877745eef9334d275eb8ae40662863d9d02880c73185e5f672a5da8f589f141c2"),
    78: ("chat images events event", "e1d7b3a625212c58ec2c462a8f6e37685b52e38eb13bfb8b7655a9d516cefd4df30ef9876344b31908b5a8c37a50aba1"),
    79: ("human facts", "7da3303ed9cf36ee261fdc225876ad849b3c6dd389cf7ffe4dd54b173a506e495fb061503f6b1c8436231c5b6392e449"),
    80: ("human facts backfill", "b4514743964b097b8c9c7b6be0b72208a02386bc1ce35ee30dd69428989bd05c27e2a74bca02a1df0c0f559cdbb59347"),
    81: ("insights 2 cleanup", "6e4d48bb0c167b1429c013363df46d58ec21ed648b0f0405e27aacac32941d9d8aec8914fdfcf6baf24ead5ca69ac768"),
    82: ("relationship memory", "5d63b454a5a8e43a5021fda22031e60c340f8bd6ecca497a2c322d4dd4dfc1756484265654b53b607f8ebca1dc64d8d7"),
    83: ("life choices romantic kinds", "b6716f71621c33c859a61b4cfbdb94d0a5d97f795ca6483e8a16774582e5ca4575f9d0c656bc2d0780bae95152629fdb"),
    84: ("row deletions", "e8ce20d4b600254cc513d056cd6efeddef391d081916c1ef4d4b3632e9e74595abd9aae3832937e92e29b0d9e919f430"),
    85: ("activities", "ee3d6ea0b6e30a3a74c89c0cff6d55365923bfe089b6295076efc12ab173f03bfcfa384a0a5ac7192d595a90a776442b"),
}
EROS_V281_HISTORY = {
    **EROS_V270_HISTORY,
    86: ("fk indexes", "3c957f7e862d7e9a72faa10bb22c69ac5227cb80a803853436e151eedad513c7e6c8b7531eac0f8872c958589a882f15"),
    87: ("function search path", "0bca779c4c9cb2e9b06636ca902723530ae5301fcdadad5538f0faf559b802391148c0525109e09dae35cce9ad0c3a7a"),
}


def _run(argv: list[str], *, input: str | bytes | None = None, timeout: int = 180,
         check: bool = True, stdout=None, stdin=None) -> subprocess.CompletedProcess:
    if input is not None and stdin is not None:
        raise ValueError("cannot provide both input and stdin")
    try:
        result = subprocess.run(argv, input=input, text=not isinstance(input, bytes),
                                stdout=subprocess.PIPE if stdout is None else stdout,
                                stderr=subprocess.PIPE, stdin=stdin, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Eros database lifecycle command failed") from None
    if check and result.returncode:
        raise RuntimeError("Eros database lifecycle command failed; inspect exact database state read-only")
    return result


def _run_stream(argv: list[str], input_path: Path, timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        with input_path.open("rb") as stream:
            result = subprocess.run(argv, stdin=stream, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Eros database restore command failed") from None
    if result.returncode:
        raise RuntimeError("Eros database restore command failed")
    return result


def _docker_psql(database: str, sql: str) -> str:
    if database != "postgres" and not DB_NAME.fullmatch(database) and database != "eros":
        raise ValueError("database name is outside the Eros lifecycle scope")
    result = _run(["docker", "exec", "-i", "-e", "PGCONNECT_TIMEOUT=5", CONTAINER,
                   "sh", "-ec", 'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
                   "eros-native-db", database], input=sql, timeout=45)
    return result.stdout.strip()


def _read_env(path: Path = ENV_PATH) -> dict[str, str]:
    try:
        info = path.stat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError("protected Eros environment permissions are unsafe")
        rows = {}
        for line in path.read_text().splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key in rows or "\n" in value or "\r" in value:
                raise RuntimeError("protected Eros environment is malformed")
            rows[key] = value
    except OSError:
        raise RuntimeError("protected Eros environment is unavailable") from None
    for key, role in (("EROS_RUNTIME_DATABASE_URL", "eros_runtime"),
                      ("EROS_MIGRATOR_DATABASE_URL", "eros_migrator")):
        parsed = urlsplit(rows.get(key, ""))
        if (parsed.scheme != "postgresql" or parsed.hostname != "gpoint-eros-db"
                or parsed.port != 5432 or parsed.path != "/eros" or parsed.username != role
                or not parsed.password):
            raise RuntimeError("protected Eros database identity is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", rows.get("EROS_DATABASE_IDENTITY", "")):
        raise RuntimeError("protected Eros database marker is invalid")
    return rows


# Product-owned world enrollment is the only additional production login.
# Effective grants, including PUBLIC grants and role membership, must match
# this exact column allowlist. No changes to the stock SQLx ledger.
WORLD_WRITER = "eros_world_writer"
WORLD_WRITER_COLUMNS = {
    ("chat_sessions", "SELECT"): ("id", "user_id", "instance_id", "channel", "archived"),
    ("persona_instances", "SELECT"): ("id", "owner_uid", "status", "genome_id"),
    ("world_enrollments", "SELECT"): ("owner_uid", "stories_enabled", "town_enabled"),
    ("world_enrollments", "INSERT"): ("owner_uid", "stories_enabled", "town_enabled"),
    ("world_enrollments", "UPDATE"): ("stories_enabled", "town_enabled"),
    ("world_worldviews", "SELECT"): ("owner_uid", "content"),
    ("world_worldviews", "INSERT"): ("owner_uid", "content"),
    ("world_worldviews", "UPDATE"): ("content", "updated_at"),
}


def world_writer_acl_query(login: bool = True) -> str:
    allowed = ",".join(
        f"('engine','{table}','{column}','{privilege}')"
        for (table, privilege), columns in WORLD_WRITER_COLUMNS.items()
        for column in columns
    )
    login_sql = "true" if login else "false"
    return f"""WITH writer AS (
      SELECT * FROM pg_roles WHERE rolname='eros_world_writer'
    ), relations AS (
      SELECT c.oid,c.relkind,n.nspname::text AS schema_name,c.relname::text AS table_name
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE left(n.nspname,3) <> 'pg_' AND n.nspname <> 'information_schema'
    ), columns AS (
      SELECT r.*,a.attnum,a.attname::text AS column_name FROM relations r
      JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum>0 AND NOT a.attisdropped
      WHERE r.relkind IN ('r','p','v','m','f')
    ), allowed(schema_name,table_name,column_name,privilege) AS (VALUES {allowed}),
    actual AS (
      SELECT c.schema_name,c.table_name,c.column_name,p.privilege
      FROM columns c CROSS JOIN writer w
      CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) p(privilege)
      WHERE has_column_privilege(w.oid,c.oid,c.attnum,p.privilege)
    )
    SELECT EXISTS (
      SELECT 1 FROM writer w
      WHERE w.rolcanlogin={login_sql} AND NOT w.rolsuper AND NOT w.rolcreatedb
        AND NOT w.rolcreaterole AND NOT w.rolinherit AND w.rolbypassrls
        AND NOT w.rolreplication AND w.rolconnlimit=6 AND w.rolconfig IS NULL
        AND NOT EXISTS(SELECT FROM pg_auth_members m WHERE w.oid IN (m.roleid,m.member))
        AND NOT EXISTS(SELECT FROM pg_db_role_setting s WHERE s.setrole=w.oid)
        AND NOT EXISTS(SELECT FROM pg_shdepend s WHERE s.refclassid='pg_authid'::regclass
          AND s.refobjid=w.oid AND s.deptype='o')
        AND has_database_privilege(w.oid,current_database(),'CONNECT')
        AND NOT has_database_privilege(w.oid,current_database(),'CREATE')
        AND NOT has_database_privilege(w.oid,current_database(),'CONNECT WITH GRANT OPTION')
        AND NOT EXISTS(SELECT FROM pg_database d WHERE d.datallowconn
          AND d.datname<>current_database() AND has_database_privilege(w.oid,d.oid,'CONNECT'))
        AND has_schema_privilege(w.oid,'engine','USAGE')
        AND NOT EXISTS(SELECT FROM pg_namespace n
          WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
            AND (has_schema_privilege(w.oid,n.oid,'CREATE')
              OR (n.nspname NOT IN ('engine','public') AND has_schema_privilege(w.oid,n.oid,'USAGE'))))
        AND NOT EXISTS(SELECT FROM relations r
          CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) p(privilege)
          WHERE r.relkind IN ('r','p','v','m','f') AND has_table_privilege(w.oid,r.oid,p.privilege))
        AND NOT EXISTS(SELECT FROM relations r
          CROSS JOIN (VALUES ('USAGE'),('SELECT'),('UPDATE')) p(privilege)
          WHERE r.relkind='S' AND has_sequence_privilege(w.oid,r.oid,p.privilege))
        AND NOT EXISTS(SELECT FROM columns c
          CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) p(privilege)
          WHERE has_column_privilege(w.oid,c.oid,c.attnum,p.privilege || ' WITH GRANT OPTION'))
        AND NOT EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
          AND p.prosecdef AND has_schema_privilege(w.oid,n.oid,'USAGE')
          AND has_function_privilege(w.oid,p.oid,'EXECUTE'))
        AND NOT EXISTS((SELECT * FROM actual EXCEPT SELECT * FROM allowed)
          UNION ALL (SELECT * FROM allowed EXCEPT SELECT * FROM actual))
    )"""


def verify_world_writer() -> None:
    if _docker_psql("eros", world_writer_acl_query()) != "t":
        raise RuntimeError("Eros world writer effective ACL differs from exact approved scope")


def _database_facts(expected_identity: str, database: str = "eros") -> dict:
    if database != "eros" and not DB_NAME.fullmatch(database):
        raise ValueError("database name is outside the Eros lifecycle scope")
    database_literal = "'" + database.replace("'", "''") + "'"
    query = rf"""SELECT json_build_object(
      'name',d.datname,'owner',pg_get_userbyid(d.datdba),'allowConnect',d.datallowconn,
      'sizeBytes',pg_database_size(d.datname),
      'serverVersionNum',current_setting('server_version_num')::integer,
      'clusterSystemIdentifier',(SELECT system_identifier::text FROM pg_control_system()),
      'publicConnect',EXISTS(SELECT 1 FROM aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) x
        WHERE x.grantee=0 AND x.privilege_type='CONNECT'),
      'runtimeDbCreate',has_database_privilege('eros_runtime',d.oid,'CREATE'),
      'migratorDbCreate',has_database_privilege('eros_migrator',d.oid,'CREATE'),
      'runtimeDbTemp',has_database_privilege('eros_runtime',d.oid,'TEMPORARY'),
      'runtimeConnect',has_database_privilege('eros_runtime',d.oid,'CONNECT'),
      'migratorConnect',has_database_privilege('eros_migrator',d.oid,'CONNECT'),
      'otherConnect',(SELECT coalesce(json_agg(r.rolname ORDER BY r.rolname),'[]'::json)
        FROM pg_roles r WHERE NOT r.rolsuper AND r.rolname NOT IN ('eros_ddl','eros_runtime','eros_migrator')
          AND has_database_privilege(r.oid,d.oid,'CONNECT')),
      'worldWriterExists',EXISTS(SELECT FROM pg_roles WHERE rolname='eros_world_writer'),
      'marker',(SELECT identity FROM public.eros_deployment_identity WHERE singleton=true),
      'extension',(SELECT extversion FROM pg_extension WHERE extname='vector'),
      'engineOwner',(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='engine'),
      'runtimeForeign',(SELECT coalesce(json_agg(datname ORDER BY datname),'[]'::json) FROM pg_database x
        WHERE x.datallowconn AND x.datname NOT IN ('eros',{database_literal}) AND has_database_privilege('eros_runtime',x.oid,'CONNECT')),
      'migratorForeign',(SELECT coalesce(json_agg(datname ORDER BY datname),'[]'::json) FROM pg_database x
        WHERE x.datallowconn AND x.datname NOT IN ('eros',{database_literal}) AND has_database_privilege('eros_migrator',x.oid,'CONNECT')),
      'runtimeSchemaUsage',has_schema_privilege('eros_runtime','engine','USAGE'),
      'runtimeSchemaCreate',has_schema_privilege('eros_runtime','engine','CREATE'),
      'runtimePublicSchemaCreate',has_schema_privilege('eros_runtime','public','CREATE'),
      'migratorSchemaUsage',has_schema_privilege('eros_migrator','engine','USAGE'),
      'migratorSchemaCreate',has_schema_privilege('eros_migrator','engine','CREATE'),
      'runtimePublicSchemaUsage',has_schema_privilege('eros_runtime','public','USAGE'),
      'migratorPublicSchemaCreate',has_schema_privilege('eros_migrator','public','CREATE'),
      'runtimeUnauthorizedTables',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='engine' AND c.relkind IN ('r','p')
          AND NOT (has_table_privilege('eros_runtime',c.oid,'SELECT') AND has_table_privilege('eros_runtime',c.oid,'INSERT')
            AND has_table_privilege('eros_runtime',c.oid,'UPDATE') AND has_table_privilege('eros_runtime',c.oid,'DELETE'))),
      'runtimeUnauthorizedSequences',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='engine' AND c.relkind='S'
          AND NOT (has_sequence_privilege('eros_runtime',c.oid,'USAGE') AND has_sequence_privilege('eros_runtime',c.oid,'SELECT')
            AND has_sequence_privilege('eros_runtime',c.oid,'UPDATE'))),
      'roles',(SELECT json_agg(json_build_object('name',rolname,'login',rolcanlogin,'superuser',rolsuper,
        'createdb',rolcreatedb,'createrole',rolcreaterole,'bypassrls',rolbypassrls,'inherit',rolinherit)
        ORDER BY rolname) FROM pg_roles WHERE rolname IN ('eros_ddl','eros_runtime','eros_migrator'))
    )::text FROM pg_database d WHERE d.datname={database_literal}"""
    try:
        raw = _docker_psql(database, query)
        facts = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError("dedicated Eros database inventory is invalid") from None
    expected_other = [WORLD_WRITER] if database == 'eros' and facts.get('worldWriterExists') is True else []
    if (facts.get("name") != database or facts.get("owner") != "eros_ddl" or facts.get("allowConnect") is not True
            or facts.get("publicConnect") is not False or facts.get("runtimeConnect") is not True
            or facts.get("runtimeDbCreate") is not False or facts.get("migratorDbCreate") is not False
            or facts.get("runtimeDbTemp") is not True
            or facts.get("migratorConnect") is not True or facts.get("otherConnect") != expected_other
            or facts.get("runtimeSchemaUsage") is not True or facts.get("runtimeSchemaCreate") is not False
            or facts.get("runtimePublicSchemaCreate") is not False
            or facts.get("migratorSchemaUsage") is not True or facts.get("migratorSchemaCreate") is not True
            or facts.get("runtimePublicSchemaUsage") is not True or facts.get("migratorPublicSchemaCreate") is not True
            or facts.get("runtimeUnauthorizedTables") != 0 or facts.get("runtimeUnauthorizedSequences") != 0
            or facts.get("marker") != expected_identity
            or not re.fullmatch(r"[0-9]+", facts.get("clusterSystemIdentifier", ""))
            or facts.get("extension") != "0.8.6" or facts.get("serverVersionNum", 0) < 160000
            or facts.get("serverVersionNum", 0) >= 170000 or facts.get("engineOwner") != "eros_ddl"
            or facts.get("runtimeForeign") != [] or facts.get("migratorForeign") != []):
        raise RuntimeError("dedicated Eros database identity or ACL gate failed")
    if not isinstance(facts.get("sizeBytes"), int) or facts["sizeBytes"] <= 0:
        raise RuntimeError("dedicated Eros database size is unavailable")
    role_by_name = {row.get("name"): row for row in facts.get("roles") or []}
    if set(role_by_name) != {"eros_ddl", "eros_runtime", "eros_migrator"}:
        raise RuntimeError("dedicated Eros database roles are incomplete")
    ddl, runtime, migrator = (role_by_name[name] for name in ("eros_ddl", "eros_runtime", "eros_migrator"))
    if (ddl["login"] or ddl["superuser"] or ddl["createdb"] or ddl["createrole"] or ddl["bypassrls"] or ddl["inherit"]
            or not runtime["login"] or runtime["superuser"] or runtime["createdb"] or runtime["createrole"]
            or not runtime["bypassrls"] or runtime["inherit"] or not migrator["login"] or migrator["superuser"]
            or migrator["createdb"] or migrator["createrole"] or migrator["bypassrls"] or migrator["inherit"]):
        raise RuntimeError("dedicated Eros role privileges differ from the approved ACL")
    if expected_other:
        verify_world_writer()
    return facts


def _schema_fingerprint(database: str) -> str:
    if database != "eros" and not DB_NAME.fullmatch(database):
        raise ValueError("database name is outside the Eros lifecycle scope")
    # The plain dump is hashed, never executed or restored. PostgreSQL's
    # random psql restrict key would otherwise make identical schemas differ.
    result = _run(["docker", "exec", "-i", CONTAINER, "sh", "-ec",
                   'exec pg_dump --schema-only --no-owner --no-privileges '
                   '--restrict-key=GpointSchemaFingerprint -U "$POSTGRES_USER" -d "$1"',
                   "eros-native-schema", database], timeout=180)
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def _memory_inventory(database: str = "eros") -> dict:
    """Return aggregate schema, table and embedding facts without reading memory content."""
    if database != "eros" and not DB_NAME.fullmatch(database):
        raise ValueError("database name is outside the Eros lifecycle scope")
    names = _docker_psql(database, """SELECT n.nspname || '.' || c.relname
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname IN ('engine','public') AND c.relkind IN ('r','p')
      ORDER BY n.nspname,c.relname""").splitlines()
    table_counts = {}
    for qualified in names:
        schema, separator, table = qualified.partition(".")
        if (not separator or schema not in {"engine", "public"}
                or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", table)):
            raise RuntimeError("Eros table inventory contains an unsafe identifier")
        count = _docker_psql(database, f'SELECT count(*) FROM "{schema}"."{table}"')
        if not re.fullmatch(r"[0-9]+", count):
            raise RuntimeError("Eros table count inventory is invalid")
        table_counts[qualified] = int(count)
    targets = {}
    for table in TEST_MEMORY_TABLE_NAMES:
        if f"engine.{table}" not in table_counts:
            raise RuntimeError("approved Eros memory table is missing")
        sql = f"""SELECT json_build_object(
          'rows',count(*),
          'vectors',count(embedding),
          'dimension512',count(*) FILTER (WHERE embedding IS NOT NULL AND vector_dims(embedding)=512),
          'inboundFks',(SELECT count(*) FROM pg_constraint WHERE contype='f'
            AND confrelid='engine.{table}'::regclass),
          'deleteTriggers',(SELECT count(*) FROM pg_trigger WHERE tgrelid='engine.{table}'::regclass
            AND NOT tgisinternal))::text FROM engine.{table}"""
        try:
            targets[f"engine.{table}"] = json.loads(_docker_psql(database, sql))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError("Eros memory table inventory is malformed") from None
    return {"schemaSha256": _schema_fingerprint(database), "tableCounts": table_counts,
            "targets": targets}


def _validate_test_memory_inventory(actual: dict, baseline: dict, *, cleared: bool = False) -> bool:
    """Fail closed unless only the OWNER-confirmed synthetic vectors differ."""
    if not isinstance(actual, dict) or not isinstance(baseline, dict):
        raise RuntimeError("approved vector inventory is malformed")
    if (actual.get("schemaSha256") != baseline.get("schemaSha256")
            or not re.fullmatch(r"[0-9a-f]{64}", str(actual.get("schemaSha256", "")))):
        raise RuntimeError("Eros schema fingerprint changed")
    expected_tables = baseline.get("tableCounts")
    current_tables = actual.get("tableCounts")
    if (not isinstance(expected_tables, dict) or not isinstance(current_tables, dict)
            or set(expected_tables) != set(current_tables)):
        raise RuntimeError("non-target Eros rows changed")
    for name, expected in expected_tables.items():
        wanted = 0 if cleared and name in TEST_MEMORY_COUNTS else expected
        if current_tables.get(name) != wanted:
            raise RuntimeError("non-target Eros rows changed" if name not in TEST_MEMORY_COUNTS
                               else "approved vector inventory changed")
    expected_targets = baseline.get("targets")
    current_targets = actual.get("targets")
    if not isinstance(expected_targets, dict) or not isinstance(current_targets, dict):
        raise RuntimeError("approved vector inventory is malformed")
    for name, expected_rows in TEST_MEMORY_COUNTS.items():
        before = expected_targets.get(name)
        after = current_targets.get(name)
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or before.get("rows") != expected_rows or before.get("vectors") != expected_rows
                or before.get("dimension512") != expected_rows
                or before.get("inboundFks") != 0 or before.get("deleteTriggers") != 0
                or after.get("inboundFks") != 0 or after.get("deleteTriggers") != 0):
            raise RuntimeError("approved vector inventory has unverified references or triggers")
        expected_after = {"rows": 0, "vectors": 0, "dimension512": 0} if cleared else {
            "rows": expected_rows, "vectors": expected_rows, "dimension512": expected_rows}
        if any(after.get(key) != value for key, value in expected_after.items()):
            raise RuntimeError("approved vector inventory changed")
    return True


def _memory_reset_is_cleared(actual: dict, baseline: dict, receipt: dict | None) -> bool:
    """Reconcile a durable reset receipt without assuming the DELETE must run again."""
    if receipt is None:
        _validate_test_memory_inventory(actual, baseline)
        return False
    state = receipt.get("state")
    if state == "COMPLETE":
        _validate_test_memory_inventory(actual, baseline, cleared=True)
        return True
    if state != "PENDING":
        raise RuntimeError("existing Voyage memory reset receipt is invalid")
    try:
        _validate_test_memory_inventory(actual, baseline)
        return False
    except RuntimeError:
        _validate_test_memory_inventory(actual, baseline, cleared=True)
        return True


def _clear_test_memory_rows() -> None:
    """Delete only the two fixed vector tables atomically and assert every other table is unchanged."""
    companion = TEST_MEMORY_COUNTS["engine.companion_memories"]
    journal = TEST_MEMORY_COUNTS["engine.relationship_journal"]
    sql = f"""BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
LOCK TABLE engine.relationship_journal,engine.companion_memories IN ACCESS EXCLUSIVE MODE;
DO $memory_reset$
DECLARE
  row record;
  amount bigint;
  deleted bigint;
  before_counts jsonb := '{{}}'::jsonb;
  after_counts jsonb := '{{}}'::jsonb;
BEGIN
  IF current_database() <> 'eros' THEN RAISE EXCEPTION 'wrong database'; END IF;
  IF EXISTS (SELECT FROM pg_constraint WHERE contype='f'
      AND confrelid IN ('engine.companion_memories'::regclass,'engine.relationship_journal'::regclass))
    OR EXISTS (SELECT FROM pg_trigger WHERE NOT tgisinternal
      AND tgrelid IN ('engine.companion_memories'::regclass,'engine.relationship_journal'::regclass)) THEN
    RAISE EXCEPTION 'memory table references or triggers are present';
  END IF;
  IF (SELECT count(*) FROM engine.companion_memories) <> {companion}
    OR (SELECT count(embedding) FROM engine.companion_memories) <> {companion}
    OR (SELECT count(*) FROM engine.companion_memories WHERE vector_dims(embedding)=512) <> {companion}
    OR (SELECT count(*) FROM engine.relationship_journal) <> {journal}
    OR (SELECT count(embedding) FROM engine.relationship_journal) <> {journal}
    OR (SELECT count(*) FROM engine.relationship_journal WHERE vector_dims(embedding)=512) <> {journal} THEN
    RAISE EXCEPTION 'approved vector inventory changed';
  END IF;
  FOR row IN SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname IN ('engine','public') AND c.relkind IN ('r','p')
        AND NOT (n.nspname='engine' AND c.relname IN ('companion_memories','relationship_journal'))
      ORDER BY n.nspname,c.relname LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I',row.nspname,row.relname) INTO amount;
    before_counts := before_counts || jsonb_build_object(row.nspname||'.'||row.relname,amount);
  END LOOP;
  DELETE FROM engine.relationship_journal;
  GET DIAGNOSTICS deleted = ROW_COUNT;
  IF deleted <> {journal} THEN RAISE EXCEPTION 'journal delete count changed'; END IF;
  DELETE FROM engine.companion_memories;
  GET DIAGNOSTICS deleted = ROW_COUNT;
  IF deleted <> {companion} THEN RAISE EXCEPTION 'companion memory delete count changed'; END IF;
  FOR row IN SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname IN ('engine','public') AND c.relkind IN ('r','p')
        AND NOT (n.nspname='engine' AND c.relname IN ('companion_memories','relationship_journal'))
      ORDER BY n.nspname,c.relname LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I',row.nspname,row.relname) INTO amount;
    after_counts := after_counts || jsonb_build_object(row.nspname||'.'||row.relname,amount);
  END LOOP;
  IF before_counts <> after_counts THEN RAISE EXCEPTION 'non-target Eros rows changed'; END IF;
END $memory_reset$;
COMMIT;"""
    _docker_psql("eros", sql)


def _postgres_identity() -> dict:
    result = _run(["docker", "inspect", "--format", "{{.Id}}|{{.Image}}|{{json .Mounts}}|{{.State.Running}}", CONTAINER], timeout=15)
    try:
        container_id, image_id, mounts_raw, running = result.stdout.strip().split("|", 3)
        mounts = json.loads(mounts_raw)
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("PostgreSQL container identity is malformed") from None
    if (not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or running != "true"
            or not isinstance(mounts, list) or not mounts
            or any(not row.get("Source") or not row.get("Destination") for row in mounts)):
        raise RuntimeError("PostgreSQL container identity is incomplete")
    normalized = sorted(({key: row.get(key) for key in ("Type", "Name", "Source", "Destination", "RW")}
                         for row in mounts), key=lambda row: (row["Destination"] or "", row["Source"] or ""))
    return {"containerId": container_id, "imageId": image_id,
            "mountsSha256": hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest()}


def _ledger(database: str = "eros") -> list[dict]:
    exists = _docker_psql(database, "SELECT to_regclass('public._sqlx_migrations') IS NOT NULL")
    if exists not in ("t", "f"):
        raise RuntimeError("Eros SQLx ledger existence is unknown")
    if exists == "f":
        # A truly empty bootstrap database has no SQLx table yet. A non-empty
        # engine schema without the ledger is ambiguous and is never adopted.
        tables = _docker_psql(database, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='engine' AND c.relkind IN ('r','p')")
        if tables != "0":
            raise RuntimeError("Eros engine schema exists without its stock SQLx ledger")
        return []
    raw = _docker_psql(database, "SELECT coalesce(json_agg(json_build_object('version',version,'description',description,'success',success,'checksum',encode(checksum,'hex')) ORDER BY version),'[]'::json)::text FROM public._sqlx_migrations")
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError("Eros SQLx ledger is malformed") from None
    versions = []
    for row in rows:
        if (not isinstance(row.get("version"), int) or row["version"] < 0
                or not isinstance(row.get("description"), str) or not row["description"]
                or row.get("success") is not True
                # SQLx 0.8.6 stores SHA-384 migration checksums as 48 bytes.
                or not re.fullmatch(r"[0-9a-f]{96}", row.get("checksum", ""))):
            raise RuntimeError("Eros SQLx ledger contains failed or malformed migration state")
        versions.append(row["version"])
    if versions != sorted(set(versions)):
        raise RuntimeError("Eros SQLx migration versions are duplicated or unordered")
    return rows


def _ledger_digest(rows: list[dict]) -> str:
    body = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise RuntimeError("Eros recovery backup is unavailable") from None
    return digest.hexdigest()


def _migration_delta(before: list[dict], after: list[dict]) -> list[dict]:
    known = {row["version"]: row for row in before}
    observed = {row["version"]: row for row in after}
    if any(version not in observed or observed[version] != row for version, row in known.items()):
        raise RuntimeError("stock Eros migration rehearsal changed or removed an applied SQLx migration")
    pending = [row for row in after if row["version"] not in known]
    if pending and before and min(row["version"] for row in pending) <= max(known):
        raise RuntimeError("stock Eros transition inserts a migration behind the live SQLx head")
    return pending


def _versions(rows: list[dict]) -> tuple[int, ...]:
    return tuple(row["version"] for row in rows)


def _migration_history_digest(rows: list[dict]) -> str:
    entries = [[row.get("version"), row.get("description"), row.get("checksum")] for row in rows]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _validate_v231_source_history(rows: list[dict], upstream_sha: str) -> list[dict]:
    if upstream_sha != EROS_V231_SHA:
        raise RuntimeError("Eros source SQLx history is not tied to the confirmed v2.3.1 upstream")
    versions = _versions(rows)
    if versions == tuple(range(72)):
        expected = EROS_V221_HISTORY_SHA256
    elif versions == tuple(range(78)):
        expected = EROS_V231_HISTORY_SHA256
    else:
        raise RuntimeError("Eros source SQLx history is not an exact v2.2.1 or v2.3.1 checkpoint")
    if any(row.get("success") is not True or not re.fullmatch(r"[0-9a-f]{96}", row.get("checksum", ""))
           or not row.get("description") for row in rows):
        raise RuntimeError("Eros source SQLx history contains failed or malformed migrations")
    if _migration_history_digest(rows) != expected:
        raise RuntimeError("Eros source SQLx history differs from the confirmed v2.3.1 upstream migrations")
    return rows


def _validate_exact_history(rows: list[dict], expected: dict[int, tuple[str, str]]) -> list[dict]:
    if _versions(rows) != tuple(expected):
        raise RuntimeError("Eros SQLx ledger does not match the exact upstream migration history")
    for row in rows:
        description, checksum = expected[row["version"]]
        if (row.get("description") != description or row.get("checksum") != checksum
                or row.get("success") is not True):
            raise RuntimeError("Eros SQLx ledger does not match the exact upstream migration history")
    return rows


def _validate_baseline_transition(before: list[dict], after: list[dict], upstream_sha: str) -> list[dict]:
    if upstream_sha != EROS_V250_SHA or _versions(before) != tuple(range(78)):
        raise RuntimeError("Eros baseline fold requires the confirmed upstream v2.5.0 history")
    return _validate_exact_history(after, {version: EROS_V270_HISTORY[version]
                                           for version in (0, 78, 79, 80, 81)})


def _validate_v270_history(rows: list[dict]) -> list[dict]:
    return _validate_exact_history(rows, EROS_V270_HISTORY)


def _validate_v281_history(rows: list[dict]) -> list[dict]:
    return _validate_exact_history(rows, EROS_V281_HISTORY)


def _image_upstream_sha(image: str) -> str:
    result = _run(["docker", "image", "inspect", "--format",
                   '{{index .Config.Labels "com.gpoint.eros.upstream-revision"}}', image], timeout=30)
    sha = result.stdout.strip()
    if not SHA40.fullmatch(sha):
        raise RuntimeError("candidate Eros image has no confirmed upstream revision")
    return sha


def _migration_stage_images(before: list[dict], image: str, *, rollback: bool = False) -> list[tuple[str, str]]:
    versions = _versions(before)
    legacy_v221 = tuple(range(72))
    legacy_v231 = tuple(range(78))
    folded_v250 = (0, 78, 79, 80, 81)
    if rollback:
        return [(image, "")]
    upstream_sha = _image_upstream_sha(image)
    if upstream_sha == EROS_V281_SHA:
        versions = _versions(before)
        if versions not in ((), tuple(EROS_V270_HISTORY), tuple(EROS_V281_HISTORY)):
            raise RuntimeError("Eros v2.8.1 refuses an unknown or unsupported SQLx migration history")
        return [(image, EROS_V281_SHA)]
    if upstream_sha != EROS_V270_SHA:
        return [(image, upstream_sha)]
    supported_folded = (versions and versions[0] == 0
                        and tuple(range(78, versions[-1] + 1)) == versions[1:]
                        and 81 <= versions[-1] <= 85)
    if versions not in ((), legacy_v221, legacy_v231) and not supported_folded:
        raise RuntimeError("Eros v2.7.0 refuses an unknown or unsupported SQLx migration history")
    if versions in (legacy_v221, legacy_v231):
        return [(EROS_V231_IMAGE, EROS_V231_SHA), (EROS_V250_IMAGE, EROS_V250_SHA), (image, EROS_V270_SHA)]
    return [(image, EROS_V270_SHA)]


def _run_migration_chain(image: str, database: str, env: dict[str, str], before: list[dict],
                         *, rollback: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    stages = _migration_stage_images(before, image, rollback=rollback)
    if (EROS_V270_SHA in {upstream_sha for _, upstream_sha in stages}
            and _versions(before) in (tuple(range(72)), tuple(range(78)))):
        # Check all source versions, descriptions and SHA-384 checksums before
        # any stock migrator can change the rehearsal or live database.
        _validate_v231_source_history(before, EROS_V231_SHA)
    current = before
    applied: list[dict] = []
    receipts: list[dict] = []
    for stage_image, upstream_sha in stages:
        prior = current
        _run_migrator(stage_image, database, env)
        current = _ledger(database)
        if upstream_sha == EROS_V231_SHA:
            prior_versions = _versions(prior)
            if prior_versions not in (tuple(range(72)), tuple(range(78))):
                raise RuntimeError("Eros v2.3.1 migration does not start at a confirmed SQLx history")
            delta = _migration_delta(prior, current)
            expected_delta = list(range(72, 78)) if prior_versions == tuple(range(72)) else []
            if _versions(current) != tuple(range(78)) or [row["version"] for row in delta] != expected_delta:
                raise RuntimeError("Eros v2.3.1 migration did not produce the exact 0–77 history")
            applied.extend(delta)
        elif upstream_sha == EROS_V250_SHA:
            _validate_baseline_transition(prior, current, upstream_sha)
            applied.extend(row for row in current if 78 <= row["version"] <= 81)
        elif upstream_sha == EROS_V270_SHA:
            delta = _migration_delta(prior, current)
            _validate_v270_history(current)
            applied.extend(delta)
        elif upstream_sha == EROS_V281_SHA:
            delta = _migration_delta(prior, current)
            _validate_v281_history(current)
            if _versions(prior) == tuple(EROS_V270_HISTORY) and [row["version"] for row in delta] != [86, 87]:
                raise RuntimeError("Eros v2.8.1 did not apply the exact 0086–0087 migration delta")
            applied.extend(delta)
        else:
            delta = _migration_delta(prior, current)
            applied.extend(delta)
        receipts.append({"image": stage_image, "upstreamSha": upstream_sha,
                         "ledgerSha256": _ledger_digest(current)})
    return current, applied, receipts


def _dsn_for_database(url: str, database: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + quote(database, safe=""), parsed.query, parsed.fragment))


def _run_migrator(image: str, database: str, env: dict[str, str]) -> None:
    migrator_url = _dsn_for_database(env["EROS_MIGRATOR_DATABASE_URL"], database)
    # A mode-0600 env file prevents the secret DSN from appearing in argv or
    # shell output. It is removed even when the stock migration exits nonzero.
    fd, env_path = tempfile.mkstemp(prefix=".eros-native-migrate-", dir="/run")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("DATABASE_URL=" + migrator_url + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _run(["docker", "run", "--rm", "--network", "gpoint_eros_data", "--env-file", env_path,
              "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--user", "65532:65532",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
              "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.50", image, "migrate"], timeout=900)
    finally:
        try:
            os.unlink(env_path)
        except OSError:
            raise RuntimeError("temporary Eros migration credentials could not be removed") from None


def _psql_identifier(name: str) -> str:
    if not DB_NAME.fullmatch(name):
        raise ValueError("isolated Eros restore database name is invalid")
    return '"' + name + '"'


def _tmpfs_file(prefix: str) -> Path:
    _tmpfs_free_bytes()
    fd, name = tempfile.mkstemp(prefix=prefix, dir="/run")
    os.fchmod(fd, 0o600)
    os.close(fd)
    return Path(name)


def _tmpfs_free_bytes() -> int:
    if _run(["stat", "-f", "-c", "%T", "/run"], timeout=15).stdout.strip() != "tmpfs":
        raise RuntimeError("private Eros backup staging requires a tmpfs /run")
    return shutil.disk_usage("/run").free


def _require_tmpfs_capacity(database_size_bytes: int) -> None:
    # The live dump and its decrypted verification/restore copy can coexist in /run.
    required = 2 * database_size_bytes + 256 * 1024 * 1024
    if _tmpfs_free_bytes() < required:
        raise RuntimeError("insufficient tmpfs storage for an Eros backup and isolated restore")


def _age_recipient() -> str:
    try:
        info = AGE_IDENTITY.lstat()
    except OSError:
        raise RuntimeError("protected GPoint backup identity is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("protected GPoint backup identity has unsafe ownership or mode")
    recipient = _run(["age-keygen", "-y", str(AGE_IDENTITY)], timeout=30).stdout.strip()
    if not AGE_RECIPIENT.fullmatch(recipient):
        raise RuntimeError("protected GPoint backup recipient is invalid")
    return recipient


def _decrypt_backup_to_tmpfs(backup: Path) -> Path:
    try:
        info = backup.lstat()
    except OSError:
        raise RuntimeError("encrypted Eros recovery backup is unavailable") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError("encrypted Eros recovery backup has unsafe ownership or mode")
    with backup.open("rb") as stream:
        if stream.readline() != b"age-encryption.org/v1\n":
            raise RuntimeError("Eros recovery backup is not age-encrypted")
    plaintext = _tmpfs_file(".eros-native-restore-")
    try:
        with plaintext.open("wb") as stream:
            _run(["age", "-d", "-i", str(AGE_IDENTITY), str(backup)],
                 timeout=1800, stdout=stream)
        if plaintext.stat().st_size == 0:
            raise RuntimeError("encrypted Eros recovery backup is empty after decryption")
        return plaintext
    except Exception:
        try:
            plaintext.unlink()
        except OSError:
            pass
        raise


def _encrypt_backup_to_staged_path(plaintext: Path, staged: Path) -> None:
    recipient = _age_recipient()
    try:
        with plaintext.open("rb") as source:
            _run(["age", "-r", recipient, "-o", str(staged)],
                 timeout=1800, stdin=source)
        if (not staged.is_file() or staged.is_symlink() or staged.stat().st_size == 0
                or staged.stat().st_uid != os.geteuid()):
            raise RuntimeError("encrypted Eros recovery backup is incomplete")
        os.chmod(staged, 0o600)
        with staged.open("rb") as stream:
            if stream.readline() != b"age-encryption.org/v1\n":
                raise RuntimeError("Eros recovery backup is not age-encrypted")
    except Exception:
        try:
            staged.unlink()
        except OSError:
            pass
        raise


def _restore_backup_into(backup: Path, restore_db: str) -> None:
    quoted = _psql_identifier(restore_db)
    plaintext = _decrypt_backup_to_tmpfs(backup)
    created = False
    try:
        _docker_psql("postgres", f"CREATE DATABASE {quoted} OWNER eros_ddl")
        created = True
        _docker_psql("postgres", f"REVOKE CONNECT ON DATABASE {quoted} FROM PUBLIC; GRANT CONNECT ON DATABASE {quoted} TO eros_runtime, eros_migrator")
        _run_stream(["docker", "exec", "-i", CONTAINER, "sh", "-ec",
              'exec pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$1"',
              "eros-native-restore", restore_db], plaintext, timeout=1800)
    except Exception:
        if created:
            _drop_database(restore_db)
        raise
    finally:
        try:
            plaintext.unlink()
        except OSError:
            raise RuntimeError("temporary decrypted Eros backup could not be removed") from None


def _prepare_backup_and_restore(run_id: str) -> tuple[Path, str]:
    live_facts = _database_facts(_read_env()["EROS_DATABASE_IDENTITY"])
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = BACKUP_DIR.stat()
    if info.st_uid != 0 or info.st_mode & 0o077 or BACKUP_DIR.is_symlink():
        raise RuntimeError("Eros backup directory permissions are unsafe")
    if shutil.disk_usage(BACKUP_DIR).free < 2 * live_facts["sizeBytes"] + 256 * 1024 * 1024:
        raise RuntimeError("insufficient VPS storage for an Eros backup and isolated restore")
    _require_tmpfs_capacity(live_facts["sizeBytes"])
    backup = BACKUP_DIR / f"native-{run_id}.dump.age"
    staged = BACKUP_DIR / f".native-{run_id}.dump.age.pending"
    if backup.exists() or backup.is_symlink() or staged.exists() or staged.is_symlink():
        raise RuntimeError("exact Eros backup path already exists")
    plaintext = _tmpfs_file(".eros-native-backup-")
    try:
        with plaintext.open("wb") as stream:
            result = _run(["docker", "exec", CONTAINER, "sh", "-ec", 'exec pg_dump -U "$POSTGRES_USER" -d eros -Fc'],
                          timeout=1800, check=False, stdout=stream)
            if result.returncode:
                raise RuntimeError("Eros database backup failed")
            stream.flush()
            os.fsync(stream.fileno())
        if plaintext.stat().st_size == 0:
            raise RuntimeError("Eros database backup is empty")
        try:
            _encrypt_backup_to_staged_path(plaintext, staged)
            os.replace(staged, backup)
            os.chmod(backup, 0o600)
            backup_sha = _file_digest(backup)
            restored_plaintext = _decrypt_backup_to_tmpfs(backup)
            try:
                _run_stream(["docker", "exec", "-i", CONTAINER, "pg_restore", "--list"],
                            restored_plaintext, timeout=90)
            finally:
                restored_plaintext.unlink()
            restore_db = "eros_check_" + run_id.replace("-", "_")
            _restore_backup_into(backup, restore_db)
            return backup, backup_sha
        except Exception:
            try:
                staged.unlink()
            except OSError:
                pass
            raise
    finally:
        try:
            plaintext.unlink()
        except OSError:
            raise RuntimeError("temporary plaintext Eros backup could not be removed") from None


def _drop_database(database: str) -> None:
    quoted = _psql_identifier(database)
    _docker_psql("postgres", f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")


def _safe_write_plan(path: Path, plan: dict) -> None:
    fd, staged = tempfile.mkstemp(prefix=".eros-native-plan-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(plan, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass


def _memory_reset_receipt_path(candidate: str) -> Path:
    return BACKUP_DIR / f"voyage-memory-reset-{candidate}.json"


def _load_memory_reset_receipt(candidate: str, image: str) -> dict | None:
    path = _memory_reset_receipt_path(candidate)
    if not path.exists():
        return None
    receipt = _load_plan(path)
    if (receipt.get("schema") != "gpoint-eros-test-memory-reset/v1"
            or receipt.get("candidateSha") != candidate or receipt.get("image") != image
            or receipt.get("state") not in {"PENDING", "COMPLETE"}):
        raise RuntimeError("existing Voyage memory reset receipt is for a different release")
    backup = Path(receipt.get("backupPath", ""))
    if (backup.parent != BACKUP_DIR or backup.is_symlink()
            or _file_digest(backup) != receipt.get("backupSha256")):
        raise RuntimeError("original protected recovery backup for memory reset is unavailable")
    if not isinstance(receipt.get("baselineInventory"), dict):
        raise RuntimeError("original approved memory inventory is unavailable")
    return receipt


def _load_plan(path: Path) -> dict:
    try:
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RuntimeError("Eros database rehearsal plan permissions are unsafe")
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RuntimeError("exact Eros database rehearsal plan is unavailable") from None
    if not isinstance(value, dict):
        raise RuntimeError("exact Eros database rehearsal plan is malformed")
    return value


def _validate_reset_plan(plan: dict, candidate: str, image: str, run_id: str,
                         env: dict[str, str]) -> dict:
    if (plan.get("schema") != "gpoint-eros-native-db-plan/v1" or plan.get("candidateSha") != candidate
            or plan.get("image") != image or plan.get("runId") != run_id
            or plan.get("rollbackCheck") is True):
        raise RuntimeError("exact Eros memory reset plan differs from this release")
    backup = Path(plan.get("backupPath", ""))
    if (backup.parent != BACKUP_DIR or backup.is_symlink()
            or _file_digest(backup) != plan.get("backupSha256")):
        raise RuntimeError("exact Eros recovery backup is missing or changed")
    if plan.get("pendingMigrations") != [] or plan.get("targetLedger") != plan.get("baselineLedger"):
        raise RuntimeError("Eros schema changes are not allowed during test memory cleanup")
    baseline_inventory = plan.get("testMemoryResetBaseline")
    if not isinstance(baseline_inventory, dict):
        raise RuntimeError("OWNER-approved test memory baseline is unavailable")
    if _postgres_identity() != plan.get("postgresIdentity"):
        raise RuntimeError("PostgreSQL runtime identity changed after memory cleanup rehearsal")
    facts = _database_facts(env["EROS_DATABASE_IDENTITY"])
    current_ledger = _ledger("eros")
    if (facts.get("marker") != plan.get("databaseIdentity")
            or facts.get("clusterSystemIdentifier") != plan.get("clusterSystemIdentifier")
            or current_ledger != plan.get("baselineLedger")):
        raise RuntimeError("Eros database identity or SQLx head changed after memory cleanup rehearsal")
    return baseline_inventory


def lifecycle(mode: str, candidate: str, image: str, run_id: str, *, rollback: bool = False,
              test_memory_reset: bool = False) -> dict:
    if not SHA40.fullmatch(candidate or "") or not IMAGE.fullmatch(image or "") or not RUN_ID.fullmatch(run_id or ""):
        raise ValueError("exact Eros candidate, digest and run identity are required")
    if mode not in {"prepare", "apply", "rollback-check", "verify-unchanged", "verify-prepared",
                    "verify-applied", "validate-test-memory-reset", "reset-test-memory",
                    "verify-test-memory-reset"}:
        raise ValueError("unsupported Eros database lifecycle mode")
    if mode in {"validate-test-memory-reset", "reset-test-memory", "verify-test-memory-reset"} \
            and not test_memory_reset:
        raise ValueError("OWNER-approved test memory reset scope is required")
    env = _read_env()
    plan_path = BACKUP_DIR / f"native-plan-{run_id}.json"
    if mode in {"prepare", "rollback-check"}:
        rollback = mode == "rollback-check"
        if rollback and test_memory_reset:
            raise ValueError("rollback cannot request test memory cleanup")
        postgres_identity = _postgres_identity()
        facts = _database_facts(env["EROS_DATABASE_IDENTITY"])
        before = _ledger("eros")
        prior_reset = _load_memory_reset_receipt(candidate, image) if test_memory_reset else None
        live_inventory = _memory_inventory("eros") if test_memory_reset else None
        memory_baseline = (prior_reset["baselineInventory"] if prior_reset else live_inventory)
        memory_already_cleared = False
        if memory_baseline is not None:
            memory_already_cleared = _memory_reset_is_cleared(live_inventory, memory_baseline, prior_reset)
        backup, backup_sha = _prepare_backup_and_restore(run_id)
        restore_db = "eros_check_" + run_id.replace("-", "_")
        try:
            restored_facts = _database_facts(env["EROS_DATABASE_IDENTITY"], restore_db)
            if restored_facts["marker"] != facts["marker"]:
                raise RuntimeError("isolated Eros restore has a different database identity")
            restored_before = _ledger(restore_db)
            if restored_before != before:
                raise RuntimeError("isolated Eros restore SQLx ledger differs from the live database")
            after, pending, migration_stages = _run_migration_chain(
                image, restore_db, env, before, rollback=rollback)
            if memory_baseline is not None:
                if pending:
                    raise RuntimeError("SQLx migrations are not allowed during test memory cleanup")
                restored_memory = _memory_inventory(restore_db)
                _validate_test_memory_inventory(restored_memory, memory_baseline,
                                                cleared=memory_already_cleared)
            _database_facts(env["EROS_DATABASE_IDENTITY"], restore_db)
            if _postgres_identity() != postgres_identity:
                raise RuntimeError("PostgreSQL runtime identity changed during Eros migration rehearsal")
            if rollback and pending:
                raise RuntimeError("target Eros image requires SQLx migrations; image rollback is incompatible")
            plan = {"schema": "gpoint-eros-native-db-plan/v1", "candidateSha": candidate,
                    "image": image, "runId": run_id, "databaseIdentity": facts["marker"],
                    "postgresIdentity": postgres_identity,
                    "clusterSystemIdentifier": facts["clusterSystemIdentifier"],
                    "databaseOwner": facts["owner"], "backupPath": str(backup), "backupSha256": backup_sha,
                    "baselineLedger": before, "baselineLedgerSha256": _ledger_digest(before),
                    "targetLedger": after, "targetLedgerSha256": _ledger_digest(after),
                    "pendingMigrations": pending, "migrationStages": migration_stages,
                    "schemaApply": bool(pending), "rollbackCheck": rollback}
            if memory_baseline is not None:
                plan["testMemoryResetBaseline"] = memory_baseline
            if prior_reset is not None:
                plan["testMemoryResetReceipt"] = prior_reset
            _safe_write_plan(plan_path, plan)
            print("EROS_DATABASE_PREFLIGHT=PASS")
            print("EROS_SQLX_MIGRATION_COUNT=" + str(len(before)))
            print("EROS_SQLX_PENDING=" + str(len(pending)))
            print("EROS_BACKUP_RESTORE=PASS SHA256=" + backup_sha)
            if rollback:
                print("EROS_IMAGE_ROLLBACK_COMPATIBILITY=PASS database_unchanged=true")
            return plan
        finally:
            _drop_database(restore_db)
    plan = _load_plan(plan_path)
    if mode == "validate-test-memory-reset":
        baseline_inventory = _validate_reset_plan(plan, candidate, image, run_id, env)
        current_inventory = _memory_inventory("eros")
        existing_receipt = plan.get("testMemoryResetReceipt")
        if existing_receipt and existing_receipt.get("state") == "COMPLETE":
            _validate_test_memory_inventory(current_inventory, baseline_inventory, cleared=True)
        elif existing_receipt and existing_receipt.get("state") == "PENDING":
            try:
                _validate_test_memory_inventory(current_inventory, baseline_inventory)
            except RuntimeError:
                _validate_test_memory_inventory(current_inventory, baseline_inventory, cleared=True)
        else:
            _validate_test_memory_inventory(current_inventory, baseline_inventory)
        plan["testMemoryResetValidatedAt"] = datetime.now(timezone.utc).isoformat()
        _safe_write_plan(plan_path, plan)
        print("EROS_TEST_MEMORY_RESET_PREFLIGHT=PASS companion=476 journal=11 schema_change=false")
        return plan
    if mode == "reset-test-memory":
        baseline_inventory = _validate_reset_plan(plan, candidate, image, run_id, env)
        if not plan.get("testMemoryResetValidatedAt"):
            raise RuntimeError("test memory reset lacks same-run preflight")
        current_inventory = _memory_inventory("eros")
        receipt_path = _memory_reset_receipt_path(candidate)
        receipt = plan.get("testMemoryResetReceipt")
        if receipt is None:
            _validate_test_memory_inventory(current_inventory, baseline_inventory)
            receipt = {"schema": "gpoint-eros-test-memory-reset/v1", "candidateSha": candidate,
                       "image": image, "state": "PENDING", "runId": run_id,
                       "backupPath": plan["backupPath"], "backupSha256": plan["backupSha256"],
                       "databaseIdentity": plan["databaseIdentity"],
                       "clusterSystemIdentifier": plan["clusterSystemIdentifier"],
                       "baselineLedgerSha256": plan["baselineLedgerSha256"],
                       "baselineInventory": baseline_inventory}
            _safe_write_plan(receipt_path, receipt)
            plan["testMemoryResetReceipt"] = receipt
        else:
            disk_receipt = _load_memory_reset_receipt(candidate, image)
            if disk_receipt != receipt:
                raise RuntimeError("persistent test memory reset receipt differs from this run")
        if receipt["state"] == "COMPLETE":
            _validate_test_memory_inventory(current_inventory, baseline_inventory, cleared=True)
            print("EROS_TEST_MEMORY_RESET=SKIP_ALREADY_CLEARED")
            return plan
        try:
            _validate_test_memory_inventory(current_inventory, baseline_inventory)
        except RuntimeError:
            # A previous attempt may have committed the atomic DELETE but died
            # before marking the receipt complete. Verify live state; never
            # issue DELETE against an already-cleared database again.
            current_inventory = _memory_inventory("eros")
            _validate_test_memory_inventory(current_inventory, baseline_inventory, cleared=True)
        else:
            _clear_test_memory_rows()
        cleared_inventory = _memory_inventory("eros")
        _validate_test_memory_inventory(cleared_inventory, baseline_inventory, cleared=True)
        receipt["state"] = "COMPLETE"
        receipt["clearedTableCounts"] = cleared_inventory["tableCounts"]
        receipt["clearedAt"] = datetime.now(timezone.utc).isoformat()
        _safe_write_plan(receipt_path, receipt)
        plan["testMemoryResetReceipt"] = receipt
        _safe_write_plan(plan_path, plan)
        print("EROS_TEST_MEMORY_RESET=PASS companion=476 journal=11 non_target_rows=unchanged")
        return plan
    if mode == "verify-test-memory-reset":
        baseline_inventory = _validate_reset_plan(plan, candidate, image, run_id, env)
        receipt = plan.get("testMemoryResetReceipt")
        if (not isinstance(receipt, dict) or receipt.get("candidateSha") != candidate
                or receipt.get("image") != image or receipt.get("runId") != run_id
                or receipt.get("backupSha256") != plan.get("backupSha256")):
            raise RuntimeError("exact test memory cleanup receipt is unavailable")
        cleared_inventory = _memory_inventory("eros")
        _validate_test_memory_inventory(cleared_inventory, baseline_inventory, cleared=True)
        if receipt.get("clearedTableCounts") != cleared_inventory.get("tableCounts"):
            raise RuntimeError("Eros non-target rows changed after test memory cleanup")
        print("EROS_TEST_MEMORY_RESET_VERIFY=PASS")
        return plan
    if mode == "verify-unchanged":
        if (plan.get("schema") != "gpoint-eros-native-db-plan/v1" or plan.get("candidateSha") != candidate
                or plan.get("image") != image or plan.get("runId") != run_id
                or plan.get("rollbackCheck") is not True or plan.get("pendingMigrations") != []):
            raise RuntimeError("Eros image rollback compatibility evidence is unavailable")
        env_identity = env["EROS_DATABASE_IDENTITY"]
        if _postgres_identity() != plan.get("postgresIdentity"):
            raise RuntimeError("PostgreSQL runtime identity changed after Eros rollback rehearsal")
        facts = _database_facts(env_identity)
        current = _ledger("eros")
        if (facts["marker"] != plan["databaseIdentity"]
                or facts["clusterSystemIdentifier"] != plan["clusterSystemIdentifier"]
                or current != plan["baselineLedger"]):
            raise RuntimeError("Eros SQLx database changed during image rollback")
        backup = Path(plan.get("backupPath", ""))
        if (backup.parent != BACKUP_DIR or backup.is_symlink()
                or _file_digest(backup) != plan.get("backupSha256")):
            raise RuntimeError("Eros rollback recovery backup changed")
        print("EROS_IMAGE_ROLLBACK_DATABASE_UNCHANGED=PASS")
        return plan
    if (plan.get("schema") != "gpoint-eros-native-db-plan/v1" or plan.get("candidateSha") != candidate
            or plan.get("image") != image or plan.get("runId") != run_id
            or plan.get("rollbackCheck") is True):
        raise RuntimeError("Eros database rehearsal plan identity differs from this candidate")
    backup = Path(plan.get("backupPath", ""))
    if backup.parent != BACKUP_DIR or backup.is_symlink() or _file_digest(backup) != plan.get("backupSha256"):
        raise RuntimeError("exact Eros recovery backup is missing or changed")
    env_identity = env["EROS_DATABASE_IDENTITY"]
    if _postgres_identity() != plan.get("postgresIdentity"):
        raise RuntimeError("PostgreSQL runtime identity changed after Eros migration rehearsal")
    facts = _database_facts(env_identity)
    before = _ledger("eros")
    use_target = mode == "verify-applied"
    expected_ledger = plan["targetLedger"] if use_target else plan["baselineLedger"]
    expected_ledger_digest = plan["targetLedgerSha256"] if use_target else plan["baselineLedgerSha256"]
    if (facts["marker"] != plan["databaseIdentity"]
            or facts["clusterSystemIdentifier"] != plan["clusterSystemIdentifier"]
            or before != expected_ledger or _ledger_digest(before) != expected_ledger_digest):
        raise RuntimeError("Eros database changed after isolated migration rehearsal")
    if mode in {"verify-prepared", "verify-applied"}:
        print("EROS_SQLX_PENDING=" + str(len(plan["pendingMigrations"])))
        print("EROS_DATABASE_" + mode.removeprefix("verify-").upper() + "=PASS")
        return plan
    if plan["pendingMigrations"]:
        expected_stages = [{"image": stage_image, "upstreamSha": upstream_sha}
                           for stage_image, upstream_sha in _migration_stage_images(before, image)]
        planned_stages = [{"image": row.get("image"), "upstreamSha": row.get("upstreamSha")}
                          for row in plan.get("migrationStages", [])]
        if planned_stages != expected_stages:
            raise RuntimeError("Eros migration stage plan differs from the isolated rehearsal")
        after, _, migration_stages = _run_migration_chain(image, "eros", env, before)
        _database_facts(env_identity)
        if (migration_stages != plan.get("migrationStages")
                or after != plan["targetLedger"] or _ledger_digest(after) != plan["targetLedgerSha256"]):
            raise RuntimeError("live Eros SQLx ledger differs from isolated migration rehearsal")
        print("EROS_SCHEMA_APPLY=PASS MIGRATIONS=" + str(len(plan["pendingMigrations"])))
    else:
        _database_facts(env_identity)
        if _ledger("eros") != plan["baselineLedger"]:
            raise RuntimeError("Eros SQLx ledger changed after no-op migration rehearsal")
        print("EROS_SCHEMA_APPLY=SKIP reason=no_pending_migrations")
    print("EROS_DATABASE_POSTCHECK=PASS")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("prepare", "apply", "rollback-check", "verify-unchanged",
                                            "verify-prepared", "verify-applied", "verify-world-writer",
                                            "validate-test-memory-reset", "reset-test-memory",
                                            "verify-test-memory-reset"), required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--test-memory-reset", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "verify-world-writer":
            if not SHA40.fullmatch(args.candidate) or not IMAGE.fullmatch(args.image) or not RUN_ID.fullmatch(args.run_id):
                raise ValueError("exact Eros candidate, digest and run identity are required")
            facts = _database_facts(_read_env()["EROS_DATABASE_IDENTITY"])
            if facts.get("worldWriterExists") is not True:
                raise RuntimeError("Eros world writer is not provisioned")
            print("EROS_WORLD_WRITER_ACL=PASS")
        else:
            lifecycle(args.mode, args.candidate, args.image, args.run_id,
                      rollback=args.mode == "rollback-check",
                      test_memory_reset=args.test_memory_reset)
    except Exception as error:
        print("EROS_DATABASE_LIFECYCLE=BLOCKED reason=" + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
