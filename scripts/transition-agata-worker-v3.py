#!/usr/bin/env python3
"""One-time, OWNER-gated V2 -> Unified V3 LaunchAgent transition with rollback watchdog.

The only supported mutation path is `--apply` plus a separate owner approval
file. This file is prepared and tested in this task but never executed against
the installed worker here.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LABEL = "one.gpoint.agata-codex-worker"
MAX_OUTAGE_SECONDS = 300
RESTORE_AFTER_SECONDS = 180
RESTORE_BUDGET_SECONDS = 150
MIN_TTL_SECONDS = 420
POLL_DEADLINE_SECONDS = 60
HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
SUPPORT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
WORKER = SUPPORT / "gpoint_mac_worker.py"
HELPER = SUPPORT / "lab_queue.py"
FIXTURE_MANIFEST = SUPPORT / "lab_fixture_manifest.json"
WRAPPER = SUPPORT / "run_codex_worker.sh"
TENANT_BINDINGS = SUPPORT / "tenant-bindings.json"
SOURCES = {
    "worker": REPO / "services/agata/worker/gpoint_mac_worker.py",
    "helper": REPO / "services/agata/worker/lab_queue.py",
    "fixture_manifest": REPO / "services/agata/worker/lab_fixture_manifest.json",
    "wrapper": REPO / "services/agata/worker/run_codex_worker.sh",
}
TARGETS = {"worker": WORKER, "helper": HELPER, "fixture_manifest": FIXTURE_MANIFEST,
           "wrapper": WRAPPER, "plist": PLIST, "tenant_bindings": TENANT_BINDINGS}
STATE_ROOT = SUPPORT / "v3-transition-recovery"
TRANSITION_LOCK = HOME / ".config/gpoint/unified-worker-transition.lock"
UTC = timezone.utc
LEGACY_GPOINT_GATEWAY_URL = "https://gpoint.amiora.one/api/agata-worker"


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_output(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def loaded() -> bool:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
    return result.returncode == 0


def read_status() -> dict:
    try:
        value = json.loads((SUPPORT / "worker-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("worker_status_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("worker_status_invalid")
    return value


def _lane_poll(status: dict, lane: str) -> dict:
    if lane == "GPOINT":
        return status
    targets = status.get("targets", {})
    return targets.get(lane, {}) if isinstance(targets, dict) else {}


def authenticated_poll_confirmed(current: dict, before: dict, lanes: tuple[str, ...]) -> bool:
    for lane in lanes:
        after_lane, before_lane = _lane_poll(current, lane), _lane_poll(before, lane)
        if (not after_lane.get("last_poll_at") or after_lane.get("last_poll_at") == before_lane.get("last_poll_at")
                or after_lane.get("state") not in {"idle", "running"}
                or after_lane.get("last_error_code")):
            return False
    return True


def _worker_config_values() -> dict[str, str]:
    if not CONFIG.is_file() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    # Read values only in memory to recognize the verified legacy GPOINT
    # endpoint. Never return/log them; credentials remain untouched.
    values: dict[str, str] = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#") and "=" in text:
            key, value = text.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _configured_lanes() -> tuple[str, ...]:
    values = _worker_config_values()
    if not values.get("GPOINT_AGATA_WORKER_URL") or not values.get("GPOINT_AGATA_WORKER_TOKEN"):
        raise RuntimeError("gpoint_worker_config_incomplete")
    if (not values.get("GPOINT_AGATA_TENANT_ID")
            and values["GPOINT_AGATA_WORKER_URL"] != LEGACY_GPOINT_GATEWAY_URL):
        raise RuntimeError("gpoint_worker_config_incomplete")
    if values.get("GPOINT_AGATA_TENANT_ID"):
        try:
            tenant_id = uuid.UUID(values["GPOINT_AGATA_TENANT_ID"])
        except ValueError as exc:
            raise RuntimeError("gpoint_worker_config_incomplete") from exc
        if tenant_id.int == 0 or str(tenant_id) != values["GPOINT_AGATA_TENANT_ID"]:
            raise RuntimeError("gpoint_worker_config_incomplete")
    lanes = ["GPOINT"]
    gsales_keys = ("GSALES_AGATA_WORKER_URL", "GSALES_AGATA_WORKER_TOKEN", "GSALES_AGATA_TENANT_ID")
    if all(values.get(key) for key in gsales_keys[:2]):
        if not values.get(gsales_keys[2]):
            raise RuntimeError("gsales_worker_config_incomplete")
        lanes.append("GSALES")
    elif any(values.get(key) for key in gsales_keys):
        raise RuntimeError("gsales_worker_config_incomplete")
    return tuple(lanes)


def _validate_configured_gpoint_binding(snapshot: dict) -> None:
    configured = _worker_config_values().get("GPOINT_AGATA_TENANT_ID")
    if configured and configured != snapshot.get("tenant_id"):
        raise RuntimeError("gpoint_worker_tenant_mismatch")


def _gpoint_snapshot() -> dict:
    helper_path = REPO / "scripts/run-gsales-one-shot-real-codex.py"
    spec_name = "gpoint_transition_queue_preflight"
    import importlib.util
    spec = importlib.util.spec_from_file_location(spec_name, helper_path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError("gpoint_queue_preflight_unavailable")
    spec.loader.exec_module(module)
    snapshot = module.queue_snapshot()
    module.validate_queue_snapshot(snapshot)
    _validate_configured_gpoint_binding(snapshot)
    if snapshot.get("pending_inference_jobs") != 0 or snapshot.get("running_inference_jobs") != 0:
        raise RuntimeError("gpoint_queue_not_empty")
    if snapshot.get("effective_inference_ttl_seconds", 0) < MIN_TTL_SECONDS:
        raise RuntimeError("inference_ttl_below_transition_budget")
    return snapshot


def _stage_gpoint_tenant_binding(snapshot: dict, candidate: dict) -> None:
    """Pin V3 to the tenant proven by the current trusted GPOINT snapshot."""
    tenant_id = snapshot.get("tenant_id")
    try:
        parsed_tenant = uuid.UUID(tenant_id)
        captured_at = datetime.fromisoformat(str(snapshot.get("snapshot_at", "")).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("gpoint_tenant_binding_snapshot_invalid") from exc
    source_id = snapshot.get("source_id")
    release_sha = snapshot.get("release_sha")
    api_image = snapshot.get("api_image_id")
    agata_image = snapshot.get("agata_image_id")
    candidate_sha = candidate.get("candidate_sha")
    worker_sha = candidate.get("assets", {}).get("worker")
    if (snapshot.get("source_system") != "gpoint" or snapshot.get("tenant_identity_verified") is not True
            or snapshot.get("transaction_read_only") is not True or snapshot.get("same_login") is not True
            or snapshot.get("role_superuser") is not False or snapshot.get("role_bypassrls") is not False
            or parsed_tenant.int == 0 or str(parsed_tenant) != tenant_id or captured_at.tzinfo is None
            or snapshot.get("pending_inference_jobs") != 0 or snapshot.get("running_inference_jobs") != 0
            or snapshot.get("effective_inference_ttl_seconds", 0) < MIN_TTL_SECONDS
            or not isinstance(release_sha, str) or len(release_sha) != 40
            or snapshot.get("api_build_sha") != release_sha or snapshot.get("agata_build_sha") != release_sha
            or not isinstance(api_image, str) or not api_image.startswith("sha256:")
            or not isinstance(agata_image, str) or not agata_image.startswith("sha256:")
            or source_id != f"gpoint-vps:{release_sha}:api={api_image}:agata={agata_image}"
            or not isinstance(candidate_sha, str) or len(candidate_sha) != 40
            or not isinstance(worker_sha, str) or len(worker_sha) != 64):
        raise RuntimeError("gpoint_tenant_binding_snapshot_invalid")
    value = {
        "version": 1,
        "candidate_sha": candidate_sha,
        "worker_sha256": worker_sha,
        "tenant_id": tenant_id,
        "source_id": source_id,
        "snapshot_at": captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    TENANT_BINDINGS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tenant-bindings-", dir=TENANT_BINDINGS.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, TENANT_BINDINGS)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_queue_snapshots(gpoint: dict, gsales: dict | None, lanes: tuple[str, ...]) -> dict:
    snapshots = {"GPOINT": gpoint}
    if "GSALES" in lanes:
        # A caller-supplied JSON file cannot establish the live GSALES runtime,
        # tenant, image identity, or database query provenance. Fail closed
        # until a source-authenticated GSALES runtime probe is implemented.
        raise RuntimeError("gsales_authoritative_queue_source_unavailable")
    return snapshots


def candidate_manifest(candidate_sha: str) -> dict:
    if len(candidate_sha) != 40 or candidate_sha.lower() != candidate_sha or any(c not in "0123456789abcdef" for c in candidate_sha):
        raise RuntimeError("candidate_sha_invalid")
    head = git_output("rev-parse", "HEAD")
    if head != candidate_sha:
        raise RuntimeError("candidate_sha_mismatch")
    if git_output("status", "--porcelain"):
        raise RuntimeError("candidate_worktree_dirty")
    files = {name: digest(path) for name, path in SOURCES.items()}
    return {"candidate_sha": head, "assets": files}


def safe_status(status: dict) -> dict:
    fields = ("state", "last_poll_at", "last_heartbeat_at", "current_job_present", "last_error_code", "updated_at")
    result = {key: status.get(key) for key in fields}
    targets = status.get("targets")
    if isinstance(targets, dict):
        result["targets"] = {
            name: {key: value.get(key) for key in fields}
            for name, value in targets.items() if name in {"GPOINT", "GSALES", "LAB"} and isinstance(value, dict)
        }
    return result


def plan(candidate_sha: str) -> dict:
    candidate = candidate_manifest(candidate_sha)
    current = {}
    for name, path in TARGETS.items():
        current[name] = digest(path) if path.is_file() else None
    lanes = _configured_lanes()
    state = read_status()
    return {
        "mode": "READ_ONLY_PLAN",
        "candidate": candidate,
        "installed_v2_asset_sha256": current,
        "launchagent_loaded": loaded(),
        "configured_lanes": list(lanes),
        "config_present_mode_0600": CONFIG.is_file() and CONFIG.stat().st_mode & 0o077 == 0,
        "worker_status": safe_status(state),
        "queue_preflight": "REQUIRED_AGAIN_IMMEDIATELY_BEFORE_APPROVED_SWITCH",
        "mutation_performed": False,
    }


def _approval(path: Path, candidate_sha: str, original_worker_sha: str) -> dict:
    try:
        stat = path.stat()
        if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("owner_approval_file_missing_or_invalid") from exc
    expected = {"approval_id", "scope", "candidate_sha", "v2_worker_sha256", "expires_at", "max_outage_seconds"}
    if (not isinstance(value, dict) or set(value) != expected or value.get("scope") != "install-unified-agata-worker-v3"
            or value.get("candidate_sha") != candidate_sha or value.get("v2_worker_sha256") != original_worker_sha
            or value.get("max_outage_seconds") != MAX_OUTAGE_SECONDS
            or not isinstance(value.get("approval_id"), str) or not value["approval_id"].isalnum()):
        raise RuntimeError("owner_approval_scope_mismatch")
    try:
        expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise RuntimeError("owner_approval_expiry_invalid") from exc
    if expires.tzinfo is None or expires <= datetime.now(UTC):
        raise RuntimeError("owner_approval_expired")
    return value


def _atomic_copy(source: Path, destination: Path, mode: int = 0o700) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            os.fchmod(output.fileno(), mode)
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_state(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".transition-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _state_lock(path: Path, timeout: float = 0.0):
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _remaining_seconds(deadline: float | None) -> float:
    return float("inf") if deadline is None else max(0.0, deadline - time.monotonic())


def _bounded_timeout(seconds: float, deadline: float | None) -> float:
    value = min(seconds, _remaining_seconds(deadline))
    if value <= 0:
        raise RuntimeError("worker_transition_deadline_exceeded")
    return value


def _bootstrap(deadline: float | None = None) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True, capture_output=True,
                   timeout=_bounded_timeout(15, deadline))
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True, capture_output=True,
                   timeout=_bounded_timeout(15, deadline))


def _wait_for_poll(before: dict, lanes: tuple[str, ...], deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            current = read_status()
        except RuntimeError:
            time.sleep(1)
            continue
        if loaded() and authenticated_poll_confirmed(current, before, lanes):
            return
        time.sleep(1)
    raise RuntimeError("fresh_authenticated_poll_not_confirmed")


def _terminate_recorded_inference(deadline: float | None = None) -> None:
    record_path = SUPPORT / "inference-process.json"
    try:
        stat = record_path.lstat()
        if record_path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("inference_process_record_permissions_invalid")
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise RuntimeError("inference_process_record_invalid") from exc
    pid, pgid = record.get("pid"), record.get("pgid")
    command = record.get("command", "")
    if Path(str(command)).name != "codex":
        raise RuntimeError("inference_process_record_invalid")
    if record.get("state") == "starting" and pid is None and pgid is None:
        # The worker writes this atomic intent record before Popen. If it was
        # killed in the Popen/return window, locate only the process holding the
        # inherited global-lock descriptor, then still verify its executable.
        try:
            holders = subprocess.run(["lsof", "-t", str(SUPPORT / "worker.lock")],
                                     capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("inference_process_identity_unavailable") from exc
        candidates = []
        for value in holders.stdout.splitlines():
            if not value.isdigit() or int(value) <= 1:
                continue
            try:
                observed = subprocess.run(["ps", "-p", value, "-o", "command="],
                                          capture_output=True, text=True, timeout=2,
                                          check=False).stdout.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("inference_process_identity_unavailable") from exc
            if observed and observed.split(None, 1)[0] == command:
                candidates.append(value)
        if len(candidates) != 1:
            if not candidates:
                record_path.unlink(missing_ok=True)
                return
            raise RuntimeError("inference_process_identity_ambiguous")
        pid = pgid = int(candidates[0])
    elif (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1
          or isinstance(pgid, bool) or not isinstance(pgid, int) or pgid != pid
          or record.get("state") != "running"):
        raise RuntimeError("inference_process_record_invalid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or pgid != pid:
        raise RuntimeError("inference_process_record_invalid")
    try:
        observed = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                  capture_output=True, text=True, timeout=2, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("inference_process_identity_unavailable") from exc
    if not observed:
        record_path.unlink(missing_ok=True)
        return
    if observed.split(None, 1)[0] != command:
        raise RuntimeError("inference_process_identity_mismatch")
    try:
        os.killpg(pgid, 15)
    except ProcessLookupError:
        record_path.unlink(missing_ok=True)
        return
    stop_deadline = min(time.monotonic() + 3, deadline if deadline is not None else float("inf"))
    while time.monotonic() < stop_deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            record_path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, 9)
    except ProcessLookupError:
        pass
    kill_deadline = min(time.monotonic() + 2, deadline if deadline is not None else float("inf"))
    while time.monotonic() < kill_deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            record_path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    raise RuntimeError("inference_process_did_not_stop")


def restore_v2(state_path: Path, *, timeout: float = 0.0) -> bool:
    lock_fd = _state_lock(state_path, timeout=timeout)
    if lock_fd is None:
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("complete") or state.get("restore_confirmed"):
            return False
        outage_deadline = state.get("outage_deadline")
        if not isinstance(outage_deadline, (int, float)):
            outage_deadline = None
        backup = Path(state["backup_dir"])
        if loaded():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False,
                           capture_output=True, timeout=_bounded_timeout(10, outage_deadline))
        _terminate_recorded_inference(outage_deadline)
        _prove_global_lock_free(timeout=_bounded_timeout(10, outage_deadline))
        for name, target in TARGETS.items():
            source = backup / name
            expected = state["original_hashes"].get(name)
            if expected is None:
                target.unlink(missing_ok=True)
            else:
                if not source.is_file() or digest(source) != expected:
                    raise RuntimeError("v2_backup_hash_invalid")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _atomic_copy(source, target, 0o600 if name in {"plist", "tenant_bindings"} else 0o700)
                if digest(target) != expected:
                    raise RuntimeError("v2_restore_hash_mismatch")
        _bootstrap(outage_deadline)
        _wait_for_poll(state["before_status"], tuple(state["v2_lanes"]),
                       min(outage_deadline or float("inf"), time.monotonic() + POLL_DEADLINE_SECONDS))
        state["restore_confirmed"] = True
        state["restored_at"] = time.time()
        _write_state(state_path, state)
        return True
    finally:
        os.close(lock_fd)


def _candidate_assets_match(state: dict) -> bool:
    try:
        return all(digest(TARGETS[name]) == value for name, value in state["candidate_assets"].items())
    except (KeyError, OSError):
        return False


def _watchdog_restore_due(active: bool, parent_alive: bool, now: float, restore_at: float) -> bool:
    return not parent_alive or (now >= restore_at)


def _mark_complete(state_path: Path) -> bool:
    lock_fd = _state_lock(state_path, timeout=0.5)
    if lock_fd is None:
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("restore_confirmed"):
            return False
        state["complete"] = True
        state["v3_poll_confirmed_at"] = time.time()
        _write_state(state_path, state)
        return True
    finally:
        os.close(lock_fd)


def watchdog(state_path: Path) -> int:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["watchdog_pid"] = os.getpid()
    state["watchdog_ready"] = True
    _write_state(state_path, state)
    while True:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 2
        if state.get("complete") or state.get("restore_confirmed"):
            return 0
        parent = state.get("parent_pid")
        try:
            os.kill(parent, 0)
            parent_alive = True
        except (ProcessLookupError, PermissionError, TypeError):
            parent_alive = False
        now = time.monotonic()
        if not state.get("bootout_attempted"):
            if not parent_alive:
                state["complete"] = True
                _write_state(state_path, state)
                return 0
            time.sleep(1)
            continue
        try:
            current = read_status()
        except RuntimeError:
            current = {}
        if loaded() and _candidate_assets_match(state) and authenticated_poll_confirmed(
            current, state["before_status"], tuple(state["lanes"]),
        ):
            if _mark_complete(state_path):
                return 0
            time.sleep(0.1)
            continue
        active = any(
            _lane_poll(current, lane).get("current_job_present") is True
            for lane in state["lanes"]
        )
        if active and not _watchdog_restore_due(active, parent_alive, now, state["restore_at"]):
            time.sleep(1)
            continue
        if _watchdog_restore_due(active, parent_alive, now, state["restore_at"]):
            try:
                restore_v2(state_path, timeout=0.5)
            except Exception as exc:
                state["restore_failure_code"] = (
                    str(exc) if str(exc).replace("_", "").isalnum() else "v2_restore_failed"
                )
                state["last_restore_attempt_at"] = time.time()
                _write_state(state_path, state)
            latest = json.loads(state_path.read_text(encoding="utf-8"))
            if latest.get("restore_confirmed"):
                return 0
        if now >= state["outage_deadline"]:
            state["watchdog_failure_code"] = "outage_deadline_exceeded"
            _write_state(state_path, state)
            return 1
        time.sleep(1)


def _prove_global_lock_free(timeout: float = 10.0) -> None:
    path = SUPPORT / "worker.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("global_worker_lock_busy") from None
                time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
    finally:
        os.close(fd)


def _backup_state(candidate_sha: str, candidate_assets: dict, lanes: tuple[str, ...], approval: dict, before_status: dict) -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    if STATE_ROOT.stat().st_uid != os.getuid() or STATE_ROOT.stat().st_mode & 0o077:
        raise RuntimeError("recovery_directory_permissions_invalid")
    backup = STATE_ROOT / uuid.uuid4().hex
    backup.mkdir(mode=0o700)
    original_hashes = {}
    for name, target in TARGETS.items():
        if target.is_file():
            original_hashes[name] = digest(target)
            shutil.copyfile(target, backup / name)
            (backup / name).chmod(0o600 if name in {"plist", "tenant_bindings"} else 0o700)
        else:
            original_hashes[name] = None
    state_path = backup / "state.json"
    state = {
        "version": 1,
        "approval_id": approval["approval_id"],
        "candidate_sha": candidate_sha,
        "candidate_assets": candidate_assets,
        "backup_dir": str(backup),
        "original_hashes": original_hashes,
        "before_status": safe_status(before_status),
        "lanes": list(lanes),
        "v2_lanes": ["GPOINT"],
        "parent_pid": os.getpid(),
        "started": None,
        "restore_at": None,
        "outage_deadline": None,
        "bootout_attempted": False,
        "complete": False,
        "restore_confirmed": False,
    }
    _write_state(state_path, state)
    return state_path


def _start_watchdog(state_path: Path) -> subprocess.Popen:
    environment = {"HOME": str(HOME), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", str(state_path)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # Watchdog liveness is checked independently from the worker agent.
        if proc.poll() is not None:
            raise RuntimeError("recovery_watchdog_start_failed")
        if state.get("watchdog_pid") == proc.pid and state.get("watchdog_ready") is True:
            return proc
        time.sleep(0.05)
    proc.terminate()
    raise RuntimeError("recovery_watchdog_not_ready")


def apply(candidate_sha: str, approval_path: Path, gsales_snapshot_path: Path | None) -> None:
    candidate = candidate_manifest(candidate_sha)
    lanes = _configured_lanes()
    if "GSALES" in lanes:
        raise RuntimeError("gsales_authoritative_queue_source_unavailable")
    if not loaded():
        raise RuntimeError("launchagent_not_loaded")
    before = read_status()
    if before.get("state") != "idle" or before.get("current_job_present") is True:
        raise RuntimeError("worker_not_idle")
    config_stat = CONFIG.stat()
    original_worker_sha = digest(WORKER)
    approval = _approval(approval_path, candidate_sha, original_worker_sha)
    gpoint = _gpoint_snapshot()
    gsales = None
    snapshots = validate_queue_snapshots(gpoint, gsales, lanes)
    state_path = _backup_state(candidate_sha, candidate["assets"], lanes, approval, before)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["queue_snapshot_ids"] = {name: snapshot.get("source_id") for name, snapshot in snapshots.items()}
    state["queue_snapshot_times"] = {name: snapshot.get("snapshot_at") for name, snapshot in snapshots.items()}
    _write_state(state_path, state)
    watchdog_proc = _start_watchdog(state_path)
    lock_fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("transition_lock_busy") from exc
        # Fresh authoritative queue and local-idle recheck immediately before bootout.
        gpoint = _gpoint_snapshot()
        gsales = None
        snapshots = validate_queue_snapshots(gpoint, gsales, lanes)
        current = read_status()
        if not loaded() or current.get("state") != "idle" or current.get("current_job_present") is True:
            raise RuntimeError("worker_changed_after_queue_preflight")
        if digest(WORKER) != original_worker_sha:
            raise RuntimeError("installed_worker_hash_changed")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["before_status"] = safe_status(current)
        state["queue_snapshot_ids"] = {name: snapshot.get("source_id") for name, snapshot in snapshots.items()}
        state["queue_snapshot_times"] = {name: snapshot.get("snapshot_at") for name, snapshot in snapshots.items()}
        switch_started = time.monotonic()
        outage_deadline = switch_started + MAX_OUTAGE_SECONDS
        state["started"] = switch_started
        state["restore_at"] = min(switch_started + RESTORE_AFTER_SECONDS,
                                   outage_deadline - RESTORE_BUDGET_SECONDS)
        state["outage_deadline"] = outage_deadline
        state["bootout_attempted"] = True
        _write_state(state_path, state)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, capture_output=True, timeout=10)
        _prove_global_lock_free(timeout=_bounded_timeout(10, outage_deadline))
        if time.monotonic() >= outage_deadline:
            raise RuntimeError("worker_transition_deadline_exceeded")
        for name in ("fixture_manifest", "helper", "worker", "wrapper"):
            source, target = SOURCES[name], TARGETS[name]
            _atomic_copy(source, target, 0o700)
            if digest(target) != candidate["assets"][name]:
                raise RuntimeError("candidate_asset_hash_mismatch")
        _stage_gpoint_tenant_binding(snapshots["GPOINT"], candidate)
        _bootstrap(outage_deadline)
        _wait_for_poll(current, lanes, min(outage_deadline, time.monotonic() + POLL_DEADLINE_SECONDS))
        if time.monotonic() >= outage_deadline:
            raise RuntimeError("worker_transition_deadline_exceeded")
        if CONFIG.stat().st_ino != config_stat.st_ino or CONFIG.stat().st_mtime_ns != config_stat.st_mtime_ns:
            raise RuntimeError("worker_config_metadata_changed")
        if not _mark_complete(state_path):
            raise RuntimeError("transition_state_lock_busy")
        if watchdog_proc.wait(timeout=5) != 0:
            raise RuntimeError("recovery_watchdog_failed")
    except BaseException:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("bootout_attempted") and not state.get("complete"):
            try:
                current = read_status()
            except RuntimeError:
                current = {}
            if not any(_lane_poll(current, lane).get("current_job_present") is True for lane in lanes):
                restore_v2(state_path, timeout=2)
        raise
    finally:
        os.close(lock_fd)
    print(json.dumps({"status": "V3_INSTALLED_AND_AUTHENTICATED", "candidate_sha": candidate_sha,
                      "lanes": lanes, "queue_snapshot_ids": {name: value.get("source_id") for name, value in snapshots.items()},
                      "credentials_changed": False, "worker_text_logged": False}, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", help="exact clean 40-character candidate SHA")
    parser.add_argument("--preflight-only", action="store_true", help="read-only, local artifact plan; no queue claims or installation")
    parser.add_argument("--apply", action="store_true", help="perform one owner-approved V2 to V3 transition")
    parser.add_argument("--owner-approval-file", type=Path)
    parser.add_argument("--gsales-queue-snapshot", type=Path)
    parser.add_argument("--watchdog", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        return watchdog(args.watchdog)
    if args.candidate is None or args.preflight_only == args.apply:
        parser.error("choose exactly one of --preflight-only or --apply and provide --candidate")
    if args.preflight_only:
        print(json.dumps(plan(args.candidate), separators=(",", ":")))
        return 0
    if args.owner_approval_file is None:
        parser.error("--apply requires a separate --owner-approval-file")
    apply(args.candidate, args.owner_approval_file, args.gsales_queue_snapshot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        message = str(exc)
        print(message if message.replace("_", "").isalnum() else type(exc).__name__.lower(), file=sys.stderr)
        raise SystemExit(1)
