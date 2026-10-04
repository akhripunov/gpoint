#!/usr/bin/env python3
"""OWNER-gated temporary LAB flag activation through the serialized lifecycle controller.

Run from the repository root as ``python3 -m scripts.agata.lab_launch_agent``.
The worker, wrapper, credentials, and LaunchAgent identity are never replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from scripts.agata.launch_agent_lifecycle import LaunchAgentController

LABEL = "one.gpoint.agata-codex-worker"
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
APPROVAL = SUPPORT / "lab-execution-approval.json"
ORIGINAL_PLIST = SUPPORT / "lab-activation-original.plist"
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LifecycleController(Protocol):
    def replace_plist(self, *, domain: str, label: str, plist_path: Path, candidate: bytes) -> None: ...


def _private_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_uid == os.getuid() and not (info.st_mode & 0o077)


def _validate_approval(path: Path) -> None:
    if not _private_regular_file(path):
        raise RuntimeError("lab_owner_approval_missing_or_private_file_required")
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("lab_owner_approval_schema_invalid") from exc
    required = {
        "approval_id", "worker_bundle_sha256", "experiment_ids", "configuration_ids",
        "scenario_ids", "max_calls", "expires_at",
    }
    if not isinstance(approval, dict) or set(approval) != required:
        raise RuntimeError("lab_owner_approval_schema_invalid")
    if (not isinstance(approval["approval_id"], str) or not _APPROVAL_ID.fullmatch(approval["approval_id"])
            or not isinstance(approval["worker_bundle_sha256"], str)
            or not _SHA256.fullmatch(approval["worker_bundle_sha256"])
            or any(not isinstance(approval[key], list) or not approval[key] or any(
                not isinstance(item, str) or not _APPROVAL_ID.fullmatch(item) for item in approval[key]
            ) for key in ("experiment_ids", "configuration_ids", "scenario_ids"))
            or isinstance(approval["max_calls"], bool) or not isinstance(approval["max_calls"], int)
            or not 1 <= approval["max_calls"] <= 8):
        raise RuntimeError("lab_owner_approval_scope_invalid")
    try:
        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("lab_owner_approval_expiry_invalid") from exc
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        raise RuntimeError("lab_owner_approval_expired_or_timezone_missing")


def _write_snapshot(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _lab_plist(original: bytes, *, label: str, enabled: bool) -> bytes:
    try:
        document = plistlib.loads(original)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise RuntimeError("lab_worker_plist_invalid") from exc
    if not isinstance(document, dict) or document.get("Label") != label:
        raise RuntimeError("lab_worker_plist_label_mismatch")
    environment = document.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise RuntimeError("lab_worker_plist_environment_invalid")
    current = environment.get("AGATA_LAB_WORKER_ENABLED", "false")
    if enabled and current == "true":
        raise RuntimeError("lab_worker_already_enabled")
    if not enabled and current != "true":
        raise RuntimeError("lab_worker_not_enabled")
    environment["AGATA_LAB_WORKER_ENABLED"] = "true" if enabled else "false"
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def activate_lab(
    *,
    controller: LifecycleController,
    plist_path: Path,
    approval_path: Path,
    state_path: Path,
    domain: str,
    label: str,
) -> None:
    _validate_approval(approval_path)
    if state_path.exists() or state_path.is_symlink():
        raise RuntimeError("lab_activation_snapshot_already_exists")
    if not _private_regular_file(plist_path):
        raise RuntimeError("lab_worker_plist_missing_or_private_file_required")
    original = plist_path.read_bytes()
    candidate = _lab_plist(original, label=label, enabled=True)
    _write_snapshot(state_path, original)
    try:
        controller.replace_plist(domain=domain, label=label, plist_path=plist_path, candidate=candidate)
    except Exception:
        # Keep the exact baseline if the controller could not prove it restored it.
        try:
            restored = plist_path.read_bytes() == original
        except OSError:
            restored = False
        if restored:
            state_path.unlink(missing_ok=True)
        raise


def rollback_lab(
    *,
    controller: LifecycleController,
    plist_path: Path,
    state_path: Path,
    domain: str,
    label: str,
) -> None:
    if not _private_regular_file(state_path):
        raise RuntimeError("lab_activation_snapshot_missing_or_private_file_required")
    if not _private_regular_file(plist_path):
        raise RuntimeError("lab_worker_plist_missing_or_private_file_required")
    original = state_path.read_bytes()
    _lab_plist(plist_path.read_bytes(), label=label, enabled=False)
    # Validate the snapshot's LaunchAgent identity before using its exact bytes.
    _lab_plist(original, label=label, enabled=True)
    controller.replace_plist(domain=domain, label=label, plist_path=plist_path, candidate=original)
    state_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "rollback"))
    parser.add_argument("--apply", action="store_true", help="required to change the existing LaunchAgent")
    args = parser.parse_args()
    if not args.apply:
        raise RuntimeError("owner_activation_approval_required: rerun only after explicit OWNER approval with --apply")
    controller = LaunchAgentController()
    domain = f"gui/{os.getuid()}"
    if args.action == "activate":
        activate_lab(
            controller=controller,
            plist_path=PLIST,
            approval_path=APPROVAL,
            state_path=ORIGINAL_PLIST,
            domain=domain,
            label=LABEL,
        )
    else:
        rollback_lab(
            controller=controller,
            plist_path=PLIST,
            state_path=ORIGINAL_PLIST,
            domain=domain,
            label=LABEL,
        )
    print(f"{LABEL} LAB {args.action} complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
