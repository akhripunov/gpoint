#!/usr/bin/env python3
"""Validate the most recent encrypted GPoint VPS backup and restore proof."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import hashlib
import stat
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RCLONE_BUCKET_ROOT = re.compile(r"[A-Za-z0-9_-]+:gpoint-backups\Z")
SCHEMA = "gpoint-scheduled-backup/v1"
SCHEDULED_ROOT = Path("/opt/gpoint/backups/scheduled")


class BackupPolicyError(ValueError):
    """A backup configuration or success proof failed closed."""


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise BackupPolicyError("backup success proof must be a private regular file owned by the service user")
        payload = path.read_text(encoding="utf-8")
        value = json.loads(payload)
    except BackupPolicyError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BackupPolicyError("backup success proof is missing or invalid") from None
    if not isinstance(value, dict):
        raise BackupPolicyError("backup success proof has an invalid shape")
    return value


def verify_bunny_evidence(manifest: dict[str, Any]) -> dict[str, Any]:
    """Require the encrypted snapshot and restore receipt to identify one Bunny object set."""
    bunny = manifest.get("bunny")
    if not isinstance(bunny, dict) or bunny.get("schema") != "gpoint-bunny-backup/v1":
        raise BackupPolicyError("Bunny backup evidence is missing or unsupported")
    if bunny.get("status") != "PASS" or bunny.get("bucket") != "gpoint-backups":
        raise BackupPolicyError("Bunny backup did not complete in the approved GPoint zone")
    remote_root = str(bunny.get("remote_root", ""))
    if not RCLONE_BUCKET_ROOT.fullmatch(remote_root):
        raise BackupPolicyError("Bunny remote root is invalid")
    candidate = str(manifest.get("candidate_sha", ""))
    run_id = str(manifest.get("run_id", ""))
    attempt = str(manifest.get("attempt", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or not run_id.isdigit() or not attempt.isdigit():
        raise BackupPolicyError("Bunny snapshot identity is invalid")
    prefix = f"{remote_root}/gpoint/scheduled/{candidate}/{run_id}-{attempt}"
    if bunny.get("snapshot_prefix") != prefix or bunny.get("snapshot") != f"{candidate}/{run_id}-{attempt}":
        raise BackupPolicyError("Bunny objects are not bound to the exact backup snapshot")

    def object_path(value: Any, expected_name: str) -> str:
        path = str(value or "")
        if path != f"{prefix}/{expected_name}":
            raise BackupPolicyError("Bunny object path is outside the exact snapshot")
        return path

    database = manifest.get("database", {})
    media = manifest.get("media", {})
    remote_database = bunny.get("database", {})
    remote_media = bunny.get("media", {})
    if not isinstance(remote_database, dict) or not isinstance(remote_media, dict):
        raise BackupPolicyError("Bunny database or media evidence is missing")
    object_path(remote_database.get("object"), Path(str(database.get("path", ""))).name)
    object_path(remote_media.get("object"), Path(str(media.get("path", ""))).name)
    object_path(remote_media.get("source_manifest_object"), "media-source.json.age")
    for remote, local in ((remote_database, database), (remote_media, media)):
        if (remote.get("sha256") != local.get("sha256") or remote.get("bytes") != local.get("bytes")
                or remote.get("encrypted") is not True or remote.get("readback") != "PASS"
                or remote.get("isolated_restore") != "PASS"):
            raise BackupPolicyError("Bunny object read-back or restore evidence does not match local proof")
    if (remote_media.get("source_manifest_sha256") != media.get("manifest_sha256")
            or not SHA256.fullmatch(str(remote_media.get("source_manifest_ciphertext_sha256", "")))
            or remote_media.get("source_manifest_readback") != "PASS"):
        raise BackupPolicyError("Bunny encrypted media manifest proof is incomplete")
    for key in ("file_count", "directory_count", "total_bytes"):
        if remote_media.get(key) != media.get(key):
            raise BackupPolicyError("Bunny media manifest metadata differs from the restored snapshot")

    restore = bunny.get("restore", {})
    local_restore = manifest.get("restore_evidence", {})
    object_path(restore.get("receipt_object"), "restore.txt")
    if (restore.get("source") != "BUNNY_READBACK" or restore.get("status") != "PASS"
            or restore.get("receipt_sha256") != local_restore.get("sha256")
            or restore.get("receipt_readback") != "PASS"):
        raise BackupPolicyError("Bunny restore receipt is missing or not bound to the local proof")
    object_path(bunny.get("manifest_object"), "manifest.json")
    if (not SHA256.fullmatch(str(bunny.get("manifest_sha256", "")))
            or bunny.get("manifest_readback") != "PASS"):
        raise BackupPolicyError("Bunny snapshot manifest read-back is incomplete")
    return bunny


def verify_freshness(
    path: Path | str,
    *,
    max_age_hours: int = 26,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require a recent successful encrypted upload, read-back, and restore."""
    if not isinstance(max_age_hours, int) or max_age_hours < 1 or max_age_hours > 168:
        raise BackupPolicyError("backup freshness limit is invalid")
    manifest = _read_private_json(Path(path))
    if manifest.get("schema") != SCHEMA or manifest.get("status") != "PASS":
        raise BackupPolicyError("latest scheduled backup is not a successful supported manifest")
    try:
        completed_at = datetime.fromisoformat(manifest["completed_at"])
    except (KeyError, TypeError, ValueError):
        raise BackupPolicyError("backup completion timestamp is invalid") from None
    if completed_at.tzinfo is None:
        raise BackupPolicyError("backup completion timestamp must include a timezone")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise BackupPolicyError("freshness comparison time must include a timezone")
    age = current.astimezone(timezone.utc) - completed_at.astimezone(timezone.utc)
    if age < timedelta(minutes=-5) or age > timedelta(hours=max_age_hours):
        raise BackupPolicyError("last successful scheduled backup is outside the freshness window")
    for component in ("database", "media"):
        proof = manifest.get(component)
        if not isinstance(proof, dict):
            raise BackupPolicyError(f"{component} backup evidence is missing")
        if not SHA256.fullmatch(str(proof.get("sha256", ""))):
            raise BackupPolicyError(f"{component} backup checksum is invalid")
        if proof.get("integrity") != "PASS" or proof.get("isolated_restore") != "PASS":
            raise BackupPolicyError(f"{component} integrity and isolated restore must pass")
        if proof.get("encrypted") is not True:
            raise BackupPolicyError(f"{component} backup encryption proof is missing")
        backup_path = Path(str(proof.get("path", "")))
        if backup_path.parent.resolve() != SCHEDULED_ROOT.resolve():
            raise BackupPolicyError(f"{component} backup path is outside scheduled VPS storage")
        try:
            info = backup_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise BackupPolicyError(f"{component} encrypted backup has unsafe identity")
            digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        except BackupPolicyError:
            raise
        except OSError:
            raise BackupPolicyError(f"{component} encrypted backup is unavailable") from None
        if digest != proof["sha256"]:
            raise BackupPolicyError(f"{component} encrypted backup checksum mismatch")
    restore = manifest.get("restore_evidence")
    if not isinstance(restore, dict) or not SHA256.fullmatch(str(restore.get("sha256", ""))):
        raise BackupPolicyError("isolated restore evidence is missing or malformed")
    restore_path = Path(str(restore.get("path", "")))
    try:
        relative = restore_path.relative_to(SCHEDULED_ROOT / "runs")
        restore_info = restore_path.lstat()
        restore_digest = hashlib.sha256(restore_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        raise BackupPolicyError("isolated restore evidence is unavailable") from None
    if (len(relative.parts) != 2 or not stat.S_ISREG(restore_info.st_mode)
            or restore_info.st_uid != os.geteuid() or stat.S_IMODE(restore_info.st_mode) != 0o600
            or restore_digest != restore["sha256"]):
        raise BackupPolicyError("isolated restore evidence failed its identity or checksum check")
    verify_bunny_evidence(manifest)
    return manifest


def verify_release_backup_evidence(
    path: Path | str,
    *,
    expected_sha256: str,
    max_age_hours: int = 26,
    now: datetime | None = None,
    backup_root: Path = Path("/opt/gpoint/backups"),
    release_logs_root: Path = Path("/opt/gpoint/release-logs"),
) -> dict[str, Any]:
    """Normalize a recent, hash-pinned release backup proof for a test restore."""
    if not isinstance(max_age_hours, int) or max_age_hours < 1 or max_age_hours > 168:
        raise BackupPolicyError("backup freshness limit is invalid")
    if not SHA256.fullmatch(expected_sha256):
        raise BackupPolicyError("release backup evidence checksum is invalid")
    evidence_path = Path(path)
    try:
        evidence_info = evidence_path.lstat()
        if (not stat.S_ISREG(evidence_info.st_mode) or evidence_info.st_uid != os.geteuid()
                or stat.S_IMODE(evidence_info.st_mode) != 0o600):
            raise BackupPolicyError("release backup evidence has unsafe identity")
        evidence_bytes = evidence_path.read_bytes()
    except BackupPolicyError:
        raise
    except OSError:
        raise BackupPolicyError("release backup evidence is unavailable") from None
    evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
    if evidence_digest != expected_sha256:
        raise BackupPolicyError("release backup evidence checksum mismatch")
    evidence_match = re.fullmatch(r"kamal-backup-restore-([0-9]+)-([0-9]+)\.txt", evidence_path.name)
    if not evidence_match:
        raise BackupPolicyError("release backup evidence path is not a supported restore receipt")
    release_sha = evidence_path.parent.name
    if (not re.fullmatch(r"[0-9a-f]{40}", release_sha)
            or evidence_path.parent.resolve() != Path(release_logs_root, release_sha).resolve()):
        raise BackupPolicyError("release backup evidence is outside the exact release log scope")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise BackupPolicyError("freshness comparison time must include a timezone")
    age = current.astimezone(timezone.utc) - datetime.fromtimestamp(
        evidence_info.st_mtime, timezone.utc)
    if age < timedelta(minutes=-5) or age > timedelta(hours=max_age_hours):
        raise BackupPolicyError("verified release backup is outside the freshness window")
    try:
        rows = evidence_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise BackupPolicyError("release backup evidence is not UTF-8") from None
    values: dict[str, str] = {}
    for row in rows:
        key, separator, value = row.partition("=")
        if separator and key in values:
            raise BackupPolicyError("release backup evidence contains duplicate fields")
        if separator:
            values[key] = value
    required = {
        "BACKUP_RESTORE_SCHEMA": "gpoint-kamal-backup-restore/v1",
        "BACKUP_ENCRYPTION": "AGE_X25519",
        "ISOLATED_RESTORE": "PASS",
        "OFFSITE_RESTORE": "PASS",
        "MEDIA_BACKUP_ENCRYPTION": "AGE_X25519",
        "MEDIA_ISOLATED_RESTORE": "PASS",
        "MEDIA_OFFSITE_RESTORE": "PASS",
        "RESTORED_PRISMA_HEAD": "20261015130000_codex_telegram_challenge",
        "RESTORED_AGATA_HEAD": "agata0011",
    }
    if any(values.get(key) != expected for key, expected in required.items()):
        raise BackupPolicyError("release backup restore proof is incomplete or has unexpected migration heads")
    db_path = Path(values.get("BACKUP_PATH", ""))
    match = re.fullmatch(r"gpoint-pre-release-([0-9a-f]{40})-([0-9]+)-([0-9]+)\.dump\.age",
                         db_path.name)
    if (not match or match.group(1) != release_sha
            or match.group(2) != evidence_match.group(1)
            or match.group(3) != evidence_match.group(2)):
        raise BackupPolicyError("release backup path is not a supported GPoint release backup")
    base = f"gpoint-pre-release-{match.group(1)}-{match.group(2)}-{match.group(3)}"
    media_path = Path(values.get("MEDIA_BACKUP_PATH", ""))
    backup_root = Path(backup_root)
    if (db_path.parent.resolve() != backup_root.resolve()
            or db_path != backup_root / f"{base}.dump.age"
            or media_path != backup_root / f"{base}.media.tar.age"):
        raise BackupPolicyError("release backup files are outside the expected VPS backup directory")

    def verify_object(name: str, object_path: Path, sha_key: str, bytes_key: str) -> tuple[str, int]:
        expected_digest = values.get(sha_key, "")
        try:
            expected_bytes = int(values.get(bytes_key, ""))
            info = object_path.lstat()
            if (object_path.parent != backup_root or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise BackupPolicyError(f"{name} release backup has unsafe identity")
            digest = hashlib.sha256(object_path.read_bytes()).hexdigest()
        except BackupPolicyError:
            raise
        except (OSError, ValueError):
            raise BackupPolicyError(f"{name} release backup is unavailable") from None
        if (not SHA256.fullmatch(expected_digest) or digest != expected_digest
                or info.st_size != expected_bytes):
            raise BackupPolicyError(f"{name} release backup checksum mismatch")
        return digest, info.st_size

    db_digest, db_bytes = verify_object("database", db_path, "BACKUP_SHA256", "BACKUP_BYTES")
    media_digest, media_bytes = verify_object(
        "media", media_path, "MEDIA_BACKUP_SHA256", "MEDIA_BACKUP_BYTES")
    for key in ("RESTORED_TABLE_COUNT", "RESTORED_DATABASE_BYTES", "MEDIA_FILE_COUNT",
                "MEDIA_DIRECTORY_COUNT", "MEDIA_TOTAL_BYTES"):
        if not values.get(key, "").isdigit():
            raise BackupPolicyError("release backup metadata is incomplete")
    if int(values["RESTORED_TABLE_COUNT"]) <= 0 or int(values["RESTORED_DATABASE_BYTES"]) <= 0:
        raise BackupPolicyError("release database restore metadata is invalid")
    manifest_sha = values.get("MEDIA_SOURCE_MANIFEST_SHA256", "")
    if not SHA256.fullmatch(manifest_sha):
        raise BackupPolicyError("release media manifest checksum is invalid")
    completed_at = datetime.fromtimestamp(evidence_info.st_mtime, timezone.utc).isoformat()
    return {
        "schema": "gpoint-kamal-backup-restore/v1", "status": "PASS",
        "completed_at": completed_at,
        "database": {"path": str(db_path), "sha256": db_digest, "bytes": db_bytes,
                     "encrypted": True, "integrity": "PASS", "isolated_restore": "PASS",
                     "restored_tables": int(values["RESTORED_TABLE_COUNT"]),
                     "restored_database_bytes": int(values["RESTORED_DATABASE_BYTES"]),
                     "prisma_head": values["RESTORED_PRISMA_HEAD"],
                     "agata_head": values["RESTORED_AGATA_HEAD"]},
        "media": {"path": str(media_path), "sha256": media_digest, "bytes": media_bytes,
                  "encrypted": True, "integrity": "PASS", "isolated_restore": "PASS",
                  "file_count": int(values["MEDIA_FILE_COUNT"]),
                  "directory_count": int(values["MEDIA_DIRECTORY_COUNT"]),
                  "total_bytes": int(values["MEDIA_TOTAL_BYTES"]),
                  "manifest_sha256": manifest_sha},
        "restore_evidence": {"path": str(evidence_path), "sha256": evidence_digest},
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freshness = subparsers.add_parser("freshness")
    freshness.add_argument("--manifest", type=Path, required=True)
    freshness.add_argument("--max-age-hours", type=int, default=26)
    release_evidence = subparsers.add_parser("release-evidence")
    release_evidence.add_argument("--evidence", type=Path, required=True)
    release_evidence.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "freshness":
            manifest = verify_freshness(args.manifest, max_age_hours=args.max_age_hours)
            print("GPOINT_BACKUP_FRESHNESS=PASS COMPLETED_AT=" + manifest["completed_at"])
        else:
            manifest = verify_release_backup_evidence(args.evidence, expected_sha256=args.sha256)
            print("GPOINT_RELEASE_BACKUP_EVIDENCE=PASS")
            for label, value in (
                ("COMPLETED_AT", manifest["completed_at"]),
                ("DATABASE_PATH", manifest["database"]["path"]),
                ("DATABASE_SHA256", manifest["database"]["sha256"]),
                ("DATABASE_BYTES", manifest["database"]["bytes"]),
                ("PRISMA_HEAD", manifest["database"]["prisma_head"]),
                ("AGATA_HEAD", manifest["database"]["agata_head"]),
                ("RESTORED_TABLES", manifest["database"]["restored_tables"]),
                ("RESTORED_DATABASE_BYTES", manifest["database"]["restored_database_bytes"]),
                ("MEDIA_PATH", manifest["media"]["path"]),
                ("MEDIA_SHA256", manifest["media"]["sha256"]),
                ("MEDIA_BYTES", manifest["media"]["bytes"]),
                ("MEDIA_FILE_COUNT", manifest["media"]["file_count"]),
                ("MEDIA_DIRECTORY_COUNT", manifest["media"]["directory_count"]),
                ("MEDIA_TOTAL_BYTES", manifest["media"]["total_bytes"]),
                ("MEDIA_MANIFEST_SHA256", manifest["media"]["manifest_sha256"]),
            ):
                print(f"GPOINT_RELEASE_BACKUP_{label}={value}")
    except (BackupPolicyError, OSError) as error:
        safe_error = str(error).replace("\n", " ")[:180]
        print(f"GPOINT_BACKUP_POLICY=BLOCKED reason={safe_error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
