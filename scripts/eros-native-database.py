#!/usr/bin/env python3
"""Fail-closed SQLx lifecycle gate used by the existing Native Deploy workflow.

The helper runs on the trusted VPS with the selected stock image. It snapshots
the dedicated Eros database, rehearses that image's migrations on an isolated
restore, and applies only the rehearsed delta to production. It never rolls a
database back.
"""
from __future__ import annotations

import argparse
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
    if (facts.get("name") != database or facts.get("owner") != "eros_ddl" or facts.get("allowConnect") is not True
            or facts.get("publicConnect") is not False or facts.get("runtimeConnect") is not True
            or facts.get("runtimeDbCreate") is not False or facts.get("migratorDbCreate") is not False
            or facts.get("runtimeDbTemp") is not True
            or facts.get("migratorConnect") is not True or facts.get("otherConnect") != []
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
    return facts


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


def lifecycle(mode: str, candidate: str, image: str, run_id: str, *, rollback: bool = False) -> dict:
    if not SHA40.fullmatch(candidate or "") or not IMAGE.fullmatch(image or "") or not RUN_ID.fullmatch(run_id or ""):
        raise ValueError("exact Eros candidate, digest and run identity are required")
    if mode not in {"prepare", "apply", "rollback-check", "verify-unchanged"}:
        raise ValueError("unsupported Eros database lifecycle mode")
    env = _read_env()
    plan_path = BACKUP_DIR / f"native-plan-{run_id}.json"
    if mode in {"prepare", "rollback-check"}:
        rollback = mode == "rollback-check"
        postgres_identity = _postgres_identity()
        facts = _database_facts(env["EROS_DATABASE_IDENTITY"])
        before = _ledger("eros")
        backup, backup_sha = _prepare_backup_and_restore(run_id)
        restore_db = "eros_check_" + run_id.replace("-", "_")
        try:
            restored_facts = _database_facts(env["EROS_DATABASE_IDENTITY"], restore_db)
            if restored_facts["marker"] != facts["marker"]:
                raise RuntimeError("isolated Eros restore has a different database identity")
            restored_before = _ledger(restore_db)
            if restored_before != before:
                raise RuntimeError("isolated Eros restore SQLx ledger differs from the live database")
            _run_migrator(image, restore_db, env)
            _database_facts(env["EROS_DATABASE_IDENTITY"], restore_db)
            after = _ledger(restore_db)
            pending = _migration_delta(before, after)
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
                    "pendingMigrations": pending, "schemaApply": bool(pending), "rollbackCheck": rollback}
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
    if (facts["marker"] != plan["databaseIdentity"]
            or facts["clusterSystemIdentifier"] != plan["clusterSystemIdentifier"]
            or before != plan["baselineLedger"]
            or _ledger_digest(before) != plan["baselineLedgerSha256"]):
        raise RuntimeError("Eros database changed after isolated migration rehearsal")
    if plan["pendingMigrations"]:
        _run_migrator(image, "eros", env)
        _database_facts(env_identity)
        after = _ledger("eros")
        if after != plan["targetLedger"] or _ledger_digest(after) != plan["targetLedgerSha256"]:
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
    parser.add_argument("--mode", choices=("prepare", "apply", "rollback-check", "verify-unchanged"), required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        lifecycle(args.mode, args.candidate, args.image, args.run_id,
                  rollback=args.mode == "rollback-check")
    except Exception as error:
        print("EROS_DATABASE_LIFECYCLE=BLOCKED reason=" + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
