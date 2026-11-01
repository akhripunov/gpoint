#!/usr/bin/env python3
"""Create and independently restore encrypted GPoint database/media backups on the VPS."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


BIN_DIR = Path(__file__).resolve().parent
AGE_IDENTITY = Path("/opt/gpoint/env/gpoint-backup.agekey")
BACKUP_ROOT = Path("/opt/gpoint/backups")
SCHEDULED_ROOT = BACKUP_ROOT / "scheduled"
MEDIA_ROOT = Path("/opt/gpoint/media")
POSTGRES = "shared_postgres_1"
AGE_RECIPIENT = re.compile(r"age1[0-9a-z]{58}\Z")
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(access[_ -]?key|secret[_ -]?key|session[_ -]?token|password|token|credential|authorization)([=: ]+)[^\s,;]+"
)
SIGNED_URL_FIELD = re.compile(r"(?i)([?&](?:x-amz-credential|x-amz-signature|signature|token)=)[^&\s]+")
URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")
AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
AGE_SECRET = re.compile(r"AGE-SECRET-KEY-[0-9A-Z]+")


class ScheduledBackupError(RuntimeError):
    pass


def _safe_diagnostic(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = " ".join(value.split())
    value = URL_USERINFO.sub(r"\1[REDACTED]@", value)
    value = SIGNED_URL_FIELD.sub(r"\1[REDACTED]", value)
    value = AUTH_HEADER.sub(r"\1 [REDACTED]", value)
    value = SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = AGE_SECRET.sub("[REDACTED_AGE_IDENTITY]", value)
    return value[:500]


def _private_file(path: Path, *, expected_mode: int = 0o600) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ScheduledBackupError("required protected file is unavailable") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != expected_mode):
        raise ScheduledBackupError("required protected file has unsafe ownership or mode")


def _run(args: list[str], *, stage: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduledBackupError(f"{stage} could not complete") from None
    if result.returncode:
        detail = _safe_diagnostic(result.stderr or "")
        suffix = ": " + detail if detail else ""
        raise ScheduledBackupError(f"{stage} failed (exit {result.returncode}){suffix}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _import_policy():
    spec = importlib.util.spec_from_file_location("gpoint_backup_policy", BIN_DIR / "gpoint-backup-policy.py")
    if spec is None or spec.loader is None:
        raise ScheduledBackupError("backup policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_facts() -> tuple[str, str, str, str, dict[str, object]]:
    api_ids = _run(["docker", "ps", "-q", "--filter", "name=^/gpoint-core-api-"],
                   stage="Kamal API inventory").splitlines()
    if len(api_ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", api_ids[0]):
        raise ScheduledBackupError("exactly one running Kamal Core API is required")
    api = _run(["docker", "inspect", "--format",
                '{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels "org.opencontainers.image.revision"}}\t{{json .Mounts}}',
                api_ids[0]], stage="Kamal API identity").split("\t", 3)
    if len(api) != 4 or api[1] != "true" or not SHA40.fullmatch(api[2]):
        raise ScheduledBackupError("Kamal API identity is incomplete")
    try:
        mounts = json.loads(api[3])
    except json.JSONDecodeError:
        raise ScheduledBackupError("Kamal API mount inventory is invalid") from None
    media = [mount for mount in mounts if mount.get("Destination") == "/data/media"]
    if (len(media) != 1 or media[0].get("Type") != "bind"
            or media[0].get("Source") != str(MEDIA_ROOT) or media[0].get("RW") is not True):
        raise ScheduledBackupError("Kamal API does not expose the expected writable media mount")
    kamal_proxy = _run(["docker", "inspect", "--format", "{{.State.Running}}", "kamal-proxy"],
                       stage="Kamal proxy health")
    if kamal_proxy != "true":
        raise ScheduledBackupError("Kamal proxy is not running")
    caddy = _run(["systemctl", "show", "--property=ActiveState", "--value", "caddy"],
                 stage="legacy Caddy state")
    if caddy != "inactive":
        raise ScheduledBackupError("legacy Caddy is not proven inactive")
    pg = _run(["docker", "inspect", "--format",
                '{{.Id}}\t{{.Image}}\t{{.State.Running}}\t{{.State.Health.Status}}\t{{index .Config.Labels "com.docker.compose.project"}}\t{{index .Config.Labels "com.docker.compose.service"}}',
               POSTGRES], stage="PostgreSQL identity").split("\t")
    if len(pg) != 6 or pg[2:] != ["true", "healthy", "shared", "postgres"]:
        raise ScheduledBackupError("shared PostgreSQL identity is invalid")
    psql = ["docker", "exec", POSTGRES, "sh", "-ec",
            'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint -c "$1"', "gpoint-backup"]
    system_id = _run(psql + ["SELECT system_identifier::text FROM pg_control_system()"],
                     stage="PostgreSQL cluster identity")
    prisma = _run(psql + ["SELECT coalesce((SELECT migration_name FROM public._prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY finished_at DESC, migration_name DESC LIMIT 1), 'NONE')"],
                  stage="Prisma head inventory")
    agata = _run(psql + ["SELECT version_num FROM agata.alembic_version"], stage="AGATA head inventory")
    if (not system_id.isdigit() or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+|NONE", prisma)
            or not re.fullmatch(r"[A-Za-z0-9_]+", agata)):
        raise ScheduledBackupError("database migration baseline is invalid")
    inventory = {
        "schema": "gpoint-vps-inventory/v1",
        "containers": [{
            "id": api[0], "name": "gpoint-core-api", "running": True, "role": "core-api",
            "mounts": [{"type": "bind", "source": media[0]["Source"],
                        "destination": media[0]["Destination"], "readWrite": True}],
        }],
    }
    return api[2], pg[0], pg[1], system_id, {"PRISMA_HEAD": prisma, "AGATA_HEAD": agata,
                                               "inventory": inventory, "api_id": api[0]}


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _parse_evidence(path: Path) -> dict[str, str]:
    _private_file(path)
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _atomic_manifest(path: Path, payload: dict[str, object]) -> None:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
        os.fsync(directory)
    finally:
        temp.unlink(missing_ok=True)
        os.close(directory)


def _retain_local(now: float) -> None:
    cutoff = now - 30 * 24 * 60 * 60
    for path in SCHEDULED_ROOT.iterdir():
        try:
            info = path.lstat()
        except OSError:
            continue
        if (stat.S_ISREG(info.st_mode) and path.name.startswith("gpoint-scheduled-")
                and info.st_mtime < cutoff):
            path.unlink()
    runs = SCHEDULED_ROOT / "runs"
    if runs.exists() and not runs.is_symlink() and runs.is_dir():
        for path in runs.iterdir():
            try:
                info = path.lstat()
            except OSError:
                continue
            if (stat.S_ISDIR(info.st_mode)
                    and re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+", path.name)
                    and info.st_mtime < cutoff):
                shutil.rmtree(path)


def _remove_failed_attempt(candidate: str, run_id: str, attempt: str,
                           run_dir: Path, evidence_dir: Path) -> None:
    for suffix in ("dump.age", "media.tar.age"):
        path = SCHEDULED_ROOT / f"gpoint-scheduled-{candidate}-{run_id}-{attempt}.{suffix}"
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == 0:
                path.unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.rmtree(evidence_dir, ignore_errors=True)


def run() -> None:
    migration_rehearsal = os.environ.get("GPOINT_MIGRATION_REHEARSAL") == "1"
    if migration_rehearsal:
        if not SHA40.fullmatch(os.environ.get("GPOINT_MIGRATION_CANDIDATE_SHA", "")):
            raise ScheduledBackupError("migration rehearsal candidate SHA is invalid")
        for name in ("GPOINT_APP_IMAGE", "GPOINT_AGATA_IMAGE"):
            if not re.fullmatch(r"ghcr\.io/akhripunov/gpoint-(?:runtime|agata-runtime)@sha256:[0-9a-f]{64}",
                                os.environ.get(name, "")):
                raise ScheduledBackupError("migration rehearsal image identity is invalid")
        for name in (
            "GPOINT_PRISMA_EROS_QUEUE_SHA256",
            "GPOINT_PRISMA_EROS_MAPPING_SHA256",
            "GPOINT_PRISMA_MESSAGE_STATE_SHA256",
            "GPOINT_AGATA_0012_SHA256",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", os.environ.get(name, "")):
                raise ScheduledBackupError("migration rehearsal checksum input is invalid")
    lock_path = Path("/run/lock/gpoint-scheduled-backup.lock")
    lock_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScheduledBackupError("another GPoint backup is already running") from None

        policy = _import_policy()
        _private_file(AGE_IDENTITY)
        recipient = _run(["age-keygen", "-y", "-i", str(AGE_IDENTITY)], stage="age key identity")
        if not AGE_RECIPIENT.fullmatch(recipient):
            raise ScheduledBackupError("age identity recipient is invalid")
        if migration_rehearsal:
            # A rehearsal's source of truth is the verified encrypted backup.
            # Do not inspect the live Kamal API, PostgreSQL container, network,
            # production environment, or media bind mount in this path.
            evidence_path = os.environ.get("GPOINT_MIGRATION_BACKUP_EVIDENCE", "").strip()
            if evidence_path:
                evidence_sha = os.environ.get("GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256", "")
                try:
                    existing_manifest = policy.verify_release_backup_evidence(
                        Path(evidence_path), expected_sha256=evidence_sha, max_age_hours=26)
                except Exception as error:
                    raise ScheduledBackupError("an existing fresh hash-pinned release backup is required for migration rehearsal") from error
            else:
                latest_path = SCHEDULED_ROOT / "latest.json"
                try:
                    existing_manifest = policy.verify_freshness(latest_path, max_age_hours=26)
                except Exception as error:
                    raise ScheduledBackupError("an existing fresh verified backup is required for migration rehearsal") from error
            database = existing_manifest.get("database", {})
            candidate = os.environ["GPOINT_MIGRATION_CANDIDATE_SHA"]
            prisma_head = database.get("prisma_head")
            agata_head = database.get("agata_head")
            if (prisma_head != "20261015130000_codex_telegram_challenge"
                    or agata_head != "agata0011"):
                raise ScheduledBackupError("existing backup migration heads are not the expected rehearsal baseline")
            postgres_id = "NOT_CONNECTED"
            postgres_image = "pgvector/pgvector:0.8.6-pg16-bookworm@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
            system_id = "NOT_CONNECTED"
            facts = {
                "PRISMA_HEAD": prisma_head,
                "AGATA_HEAD": agata_head,
                "inventory": {"schema": "gpoint-migration-rehearsal-inventory/v1", "containers": []},
                "api_id": "NOT_CONNECTED",
            }
        else:
            candidate, postgres_id, postgres_image, system_id, facts = _source_facts()
        run_id = str(time.time_ns())
        attempt = "1"
        required_directories = (Path("/run/gpoint-kamal"), BACKUP_ROOT)
        if not migration_rehearsal:
            required_directories += (MEDIA_ROOT,)
        for directory in required_directories:
            try:
                info = directory.lstat()
            except OSError:
                raise ScheduledBackupError("required GPoint backup directory is unavailable") from None
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                raise ScheduledBackupError("required GPoint backup directory has unsafe identity")
        try:
            SCHEDULED_ROOT.mkdir(mode=0o700, exist_ok=True)
            scheduled_info = SCHEDULED_ROOT.lstat()
        except OSError:
            raise ScheduledBackupError("scheduled backup directory is unavailable") from None
        if (not stat.S_ISDIR(scheduled_info.st_mode) or scheduled_info.st_uid != 0
                or stat.S_IMODE(scheduled_info.st_mode) != 0o700):
            raise ScheduledBackupError("scheduled backup directory has unsafe identity")
        latest_path = SCHEDULED_ROOT / "latest.json"
        if migration_rehearsal and (existing_manifest["database"].get("prisma_head") != facts["PRISMA_HEAD"]
                                    or existing_manifest["database"].get("agata_head") != facts["AGATA_HEAD"]):
            raise ScheduledBackupError("existing backup migration heads do not match the validated baseline")
        runs_root = SCHEDULED_ROOT / "runs"
        try:
            runs_root.mkdir(mode=0o700, exist_ok=True)
            runs_info = runs_root.lstat()
        except OSError:
            raise ScheduledBackupError("scheduled backup evidence directory is unavailable") from None
        if (not stat.S_ISDIR(runs_info.st_mode) or runs_info.st_uid != 0
                or stat.S_IMODE(runs_info.st_mode) != 0o700):
            raise ScheduledBackupError("scheduled backup evidence directory has unsafe identity")
        run_dir = Path(f"/run/gpoint-kamal/{candidate}-{run_id}-{attempt}")
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        run_evidence_dir = SCHEDULED_ROOT / "runs" / f"{candidate}-{run_id}-{attempt}"
        run_evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        run_evidence_info = run_evidence_dir.lstat()
        if (not stat.S_ISDIR(run_evidence_info.st_mode) or run_evidence_info.st_uid != 0
                or stat.S_IMODE(run_evidence_info.st_mode) != 0o700):
            raise ScheduledBackupError("scheduled backup run evidence directory has unsafe identity")
        inventory_path = run_evidence_dir / "inventory.json"
        baseline_path = run_evidence_dir / "baseline.txt"
        inventory_bytes = json.dumps(facts["inventory"], sort_keys=True, separators=(",", ":")).encode()
        _write_private(inventory_path, inventory_bytes)
        values = {
            "BASELINE_SCHEMA": ("gpoint-migration-rehearsal-baseline/v1" if migration_rehearsal
                                else "gpoint-kamal-cutover-baseline/v1"),
            "CANDIDATE_SHA": candidate, "RUN_ID": run_id, "ATTEMPT": attempt,
            "POSTGRES_CONTAINER_ID": postgres_id, "POSTGRES_IMAGE_ID": postgres_image,
            "POSTGRES_SYSTEM_IDENTIFIER": system_id,
            "API_CONTAINER_ID": facts["api_id"],
            "PRISMA_HEAD": facts["PRISMA_HEAD"], "AGATA_HEAD": facts["AGATA_HEAD"],
            "INVENTORY_FILE": inventory_path,
            "INVENTORY_SHA256": hashlib.sha256(inventory_bytes).hexdigest(),
        }
        baseline_bytes = ("".join(f"{key}={value}\n" for key, value in values.items())).encode()
        _write_private(baseline_path, baseline_bytes)
        baseline_sha = hashlib.sha256(baseline_bytes).hexdigest()
        helper_sources = {
            run_dir / "backup.py": BIN_DIR / "agata-production-migration.py",
            run_dir / "backup-policy.py": BIN_DIR / "gpoint-backup-policy.py",
            run_dir / "media-tree.py": BIN_DIR / "gpoint-media-tree.py",
            run_dir / "restore.sh": BIN_DIR / "verify-gpoint-scheduled-backup-restore.sh",
        }
        for target, source in helper_sources.items():
            try:
                info = source.lstat()
            except OSError:
                raise ScheduledBackupError("installed backup helper set is incomplete") from None
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
                raise ScheduledBackupError("installed backup helper set is incomplete")
            shutil.copyfile(source, target)
            os.chmod(target, 0o700)
        command = ["bash", str(run_dir / "restore.sh"), candidate, run_id, attempt,
                   str(baseline_path), baseline_sha, str(run_dir / "backup.py"), recipient,
                   str(run_dir / "media-tree.py"), str(run_dir / "backup-policy.py")]
        result = None
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, check=False, timeout=6 * 60 * 60)
        except subprocess.TimeoutExpired:
            _remove_failed_attempt(candidate, run_id, attempt, run_dir, run_evidence_dir)
            raise ScheduledBackupError("encrypted VPS backup and isolated restore exceeded six hours") from None
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        for line in result.stdout.splitlines():
            if re.fullmatch(r"GPOINT_[A-Z0-9_]+=(?:PASS|NOT_CHECKED|[0-9]+)", line):
                print(line)
        if result.returncode:
            _remove_failed_attempt(candidate, run_id, attempt, run_dir, run_evidence_dir)
            detail = _safe_diagnostic(result.stderr or "")
            if detail:
                print("GPOINT_BACKUP_DIAGNOSTIC=" + detail)
            raise ScheduledBackupError(f"encrypted VPS backup and isolated restore failed (exit {result.returncode})")
        evidence_path = run_evidence_dir / "restore.txt"
        evidence = _parse_evidence(evidence_path)
        required = {
            "BACKUP_ENCRYPTION": "AGE_X25519", "BACKUP_INTEGRITY": "PASS",
            "ISOLATED_RESTORE": "PASS", "MEDIA_BACKUP_ENCRYPTION": "AGE_X25519",
            "MEDIA_BACKUP_INTEGRITY": "PASS", "MEDIA_ISOLATED_RESTORE": "PASS",
        }
        if any(evidence.get(key) != value for key, value in required.items()):
            _remove_failed_attempt(candidate, run_id, attempt, run_dir, run_evidence_dir)
            raise ScheduledBackupError("isolated restore evidence is incomplete")
        if migration_rehearsal:
            migration_required = {
                "MIGRATION_PARITY": "PASS",
                "MIGRATION_DATA_PRESERVATION": "PASS",
                "MIGRATION_SYNTHETIC_RLS": "PASS",
                "MIGRATION_PRODUCTION_GRANTS_RLS": "NOT_CHECKED",
                "MIGRATION_PRISMA_HEAD": "20261022110000_eros_gpoint_message_state",
                "MIGRATION_AGATA_HEAD": "agata0012",
                "MIGRATION_CANDIDATE_SHA": os.environ["GPOINT_MIGRATION_CANDIDATE_SHA"],
            }
            migration_hashes = (
                "MIGRATION_BASELINE_PRISMA_CHECKSUMS_SHA256",
                "MIGRATION_NEW_PRISMA_CHECKSUMS_SHA256",
                "MIGRATION_PRISMA_EROS_QUEUE_SHA256",
                "MIGRATION_PRISMA_EROS_MAPPING_SHA256",
                "MIGRATION_PRISMA_MESSAGE_STATE_SHA256",
                "MIGRATION_AGATA_0012_SHA256",
                "MIGRATION_TABLE_ROW_COUNTS_SHA256",
                "MIGRATION_AGATA_PROFILE_SEMANTICS_SHA256",
            )
            if (any(evidence.get(key) != value for key, value in migration_required.items())
                    or evidence.get("MIGRATION_BASELINE_PRISMA_COUNT") != "18"
                    or any(not re.fullmatch(r"[0-9a-f]{64}", evidence.get(key, ""))
                           for key in migration_hashes)):
                _remove_failed_attempt(candidate, run_id, attempt, run_dir, run_evidence_dir)
                raise ScheduledBackupError("migration rehearsal evidence is incomplete")
            print("GPOINT_MIGRATION_REHEARSAL=PASS")
            print("GPOINT_MIGRATION_CANDIDATE_SHA=" + evidence["MIGRATION_CANDIDATE_SHA"])
            print("GPOINT_MIGRATION_EVIDENCE=" + str(evidence_path))
            print("GPOINT_MIGRATION_EVIDENCE_SHA256=" + _sha256(evidence_path))
            print("GPOINT_EXISTING_DATABASE_BACKUP_SHA256=" + existing_manifest["database"]["sha256"])
            print("GPOINT_EXISTING_MEDIA_BACKUP_SHA256=" + existing_manifest["media"]["sha256"])
            print("GPOINT_EXISTING_BACKUP_COMPLETED_AT=" + existing_manifest["completed_at"])
            for key in migration_hashes:
                print("GPOINT_" + key + "=" + evidence[key])
            return
        completed_at = datetime.now(timezone.utc).isoformat()
        manifest: dict[str, object] = {
            "schema": "gpoint-scheduled-backup/v1", "status": "PASS",
            "completed_at": completed_at, "candidate_sha": candidate,
            "run_id": run_id, "attempt": attempt,
            "database": {"path": evidence["BACKUP_PATH"], "sha256": evidence["BACKUP_SHA256"],
                         "bytes": int(evidence["BACKUP_BYTES"]), "encrypted": True,
                         "integrity": "PASS", "isolated_restore": "PASS",
                         "restored_tables": int(evidence["RESTORED_TABLE_COUNT"]),
                         "prisma_head": evidence["RESTORED_PRISMA_HEAD"],
                         "agata_head": evidence["RESTORED_AGATA_HEAD"]},
            "media": {"path": evidence["MEDIA_BACKUP_PATH"], "sha256": evidence["MEDIA_BACKUP_SHA256"],
                      "bytes": int(evidence["MEDIA_BACKUP_BYTES"]), "encrypted": True,
                      "integrity": "PASS", "isolated_restore": "PASS",
                      "file_count": int(evidence["MEDIA_FILE_COUNT"]),
                      "directory_count": int(evidence["MEDIA_DIRECTORY_COUNT"]),
                      "manifest_sha256": evidence["MEDIA_SOURCE_MANIFEST_SHA256"]},
            "restore_evidence": {"path": str(evidence_path), "sha256": _sha256(evidence_path)},
        }
        pending_manifest = SCHEDULED_ROOT / f".latest-{run_id}-{attempt}.json"
        _atomic_manifest(pending_manifest, manifest)
        try:
            policy.verify_freshness(pending_manifest, max_age_hours=26)
            os.replace(pending_manifest, latest_path)
        except Exception:
            pending_manifest.unlink(missing_ok=True)
            _remove_failed_attempt(candidate, run_id, attempt, run_dir, run_evidence_dir)
            raise
        _retain_local(time.time())
        print("GPOINT_BACKUP_SCHEDULED=PASS")
        print("GPOINT_BACKUP_MANIFEST=" + str(SCHEDULED_ROOT / "latest.json"))


def main() -> int:
    try:
        run()
    except ScheduledBackupError as error:
        print("GPOINT_BACKUP_SCHEDULED=BLOCKED reason=" + str(error).replace("\n", " ")[:180])
        return 1
    except Exception as error:
        print("GPOINT_BACKUP_SCHEDULED=BLOCKED reason=unexpected " + type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
