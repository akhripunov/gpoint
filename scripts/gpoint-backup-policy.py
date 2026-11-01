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
    return manifest


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freshness = subparsers.add_parser("freshness")
    freshness.add_argument("--manifest", type=Path, required=True)
    freshness.add_argument("--max-age-hours", type=int, default=26)
    args = parser.parse_args()
    try:
        manifest = verify_freshness(args.manifest, max_age_hours=args.max_age_hours)
        print("GPOINT_BACKUP_FRESHNESS=PASS COMPLETED_AT=" + manifest["completed_at"])
    except (BackupPolicyError, OSError) as error:
        safe_error = str(error).replace("\n", " ")[:180]
        print(f"GPOINT_BACKUP_POLICY=BLOCKED reason={safe_error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
