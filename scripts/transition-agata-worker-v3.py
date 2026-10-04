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
import plistlib
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
RESTORE_AFTER_SECONDS = 220
MIN_TTL_SECONDS = 420
POLL_DEADLINE_SECONDS = 60
HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
SUPPORT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
WORKER = SUPPORT / "gpoint_mac_worker.py"
HELPER = SUPPORT / "lab_queue.py"
WRAPPER = SUPPORT / "run_codex_worker.sh"
SOURCES = {
    "worker": REPO / "services/agata/worker/gpoint_mac_worker.py",
    "helper": REPO / "services/agata/worker/lab_queue.py",
    "wrapper": REPO / "services/agata/worker/run_codex_worker.sh",
}
TARGETS = {"worker": WORKER, "helper": HELPER, "wrapper": WRAPPER, "plist": PLIST}
STATE_ROOT = SUPPORT / "v3-transition-recovery"
TRANSITION_LOCK = HOME / ".config/gpoint/unified-worker-transition.lock"
UTC = timezone.utc


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


def _configured_lanes() -> tuple[str, ...]:
    if not CONFIG.is_file() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    # Read only key names and whether each value is non-empty. Never return/log a value.
    present: set[str] = set()
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#") and "=" in text:
            key, value = text.split("=", 1)
            if value.strip().strip('"').strip("'"):
                present.add(key.strip())
    if not {"GPOINT_AGATA_WORKER_URL", "GPOINT_AGATA_WORKER_TOKEN", "GPOINT_AGATA_TENANT_ID"}.issubset(present):
        raise RuntimeError("gpoint_worker_config_incomplete")
    lanes = ["GPOINT"]
    if {"GSALES_AGATA_WORKER_URL", "GSALES_AGATA_WORKER_TOKEN"}.issubset(present):
        if "GSALES_AGATA_TENANT_ID" not in present:
            raise RuntimeError("gsales_worker_config_incomplete")
        lanes.append("GSALES")
    elif ("GSALES_AGATA_WORKER_URL" in present) != ("GSALES_AGATA_WORKER_TOKEN" in present):
        raise RuntimeError("gsales_worker_config_incomplete")
    return tuple(lanes)


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
    if snapshot.get("pending_inference_jobs") != 0 or snapshot.get("running_inference_jobs") != 0:
        raise RuntimeError("gpoint_queue_not_empty")
    if snapshot.get("effective_inference_ttl_seconds", 0) < MIN_TTL_SECONDS:
        raise RuntimeError("inference_ttl_below_transition_budget")
    return snapshot


def validate_queue_snapshots(gpoint: dict, gsales: dict | None, lanes: tuple[str, ...]) -> dict:
    snapshots = {"GPOINT": gpoint}
    if "GSALES" in lanes:
        if not isinstance(gsales, dict):
            raise RuntimeError("gsales_authoritative_queue_snapshot_required")
        if (gsales.get("target") != "GSALES" or gsales.get("pending_inference_jobs") != 0
                or gsales.get("running_inference_jobs") != 0
                or gsales.get("effective_inference_ttl_seconds", 0) < MIN_TTL_SECONDS
                or not isinstance(gsales.get("source_id"), str) or not gsales.get("source_id")
                or not isinstance(gsales.get("snapshot_at"), str) or not gsales.get("snapshot_at")
                or gsales.get("transaction_read_only") is not True
                or gsales.get("same_login") is not True
                or gsales.get("role_superuser") is not False
                or gsales.get("role_bypassrls") is not False):
            raise RuntimeError("gsales_authoritative_queue_snapshot_invalid")
        try:
            observed = datetime.fromisoformat(gsales["snapshot_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("gsales_authoritative_queue_snapshot_invalid") from exc
        age = (datetime.now(UTC) - observed).total_seconds()
        if observed.tzinfo is None or age < -1 or age > 5:
            raise RuntimeError("gsales_authoritative_queue_snapshot_stale")
        snapshots["GSALES"] = gsales
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


def _bootstrap() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True, capture_output=True, timeout=15)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True, capture_output=True, timeout=15)


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


def restore_v2(state_path: Path, *, timeout: float = 0.0) -> bool:
    lock_fd = _state_lock(state_path, timeout=timeout)
    if lock_fd is None:
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("complete") or state.get("restore_confirmed"):
            return False
        backup = Path(state["backup_dir"])
        if loaded():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False, capture_output=True, timeout=10)
        for name, target in TARGETS.items():
            source = backup / name
            expected = state["original_hashes"].get(name)
            if expected is None:
                target.unlink(missing_ok=True)
            else:
                if not source.is_file() or digest(source) != expected:
                    raise RuntimeError("v2_backup_hash_invalid")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _atomic_copy(source, target, 0o600 if name == "plist" else 0o700)
                if digest(target) != expected:
                    raise RuntimeError("v2_restore_hash_mismatch")
        _bootstrap()
        _wait_for_poll(state["before_status"], tuple(state["v2_lanes"]), time.monotonic() + POLL_DEADLINE_SECONDS)
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
        if active:
            # Let an already claimed user generation finish before attempting rollback.
            time.sleep(1)
            continue
        if not parent_alive or now >= state["restore_at"]:
            try:
                restore_v2(state_path, timeout=0.5)
            except Exception:
                pass
            latest = json.loads(state_path.read_text(encoding="utf-8"))
            if latest.get("restore_confirmed"):
                return 0
        if now >= state["outage_deadline"]:
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
            (backup / name).chmod(0o600 if name == "plist" else 0o700)
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
    if "GSALES" in lanes:
        if gsales_snapshot_path is None:
            raise RuntimeError("gsales_authoritative_queue_snapshot_required")
        gsales = json.loads(gsales_snapshot_path.read_text(encoding="utf-8"))
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
        gsales = json.loads(gsales_snapshot_path.read_text(encoding="utf-8")) if "GSALES" in lanes else None
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
        state["restore_at"] = switch_started + RESTORE_AFTER_SECONDS
        state["outage_deadline"] = outage_deadline
        state["bootout_attempted"] = True
        _write_state(state_path, state)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, capture_output=True, timeout=10)
        _prove_global_lock_free()
        if time.monotonic() >= outage_deadline:
            raise RuntimeError("worker_transition_deadline_exceeded")
        for name in ("helper", "worker", "wrapper"):
            source, target = SOURCES[name], TARGETS[name]
            _atomic_copy(source, target, 0o700)
            if digest(target) != candidate["assets"][name]:
                raise RuntimeError("candidate_asset_hash_mismatch")
        _bootstrap()
        _wait_for_poll(current, lanes, min(outage_deadline, time.monotonic() + POLL_DEADLINE_SECONDS))
        if time.monotonic() >= outage_deadline:
            raise RuntimeError("worker_transition_deadline_exceeded")
        if CONFIG.stat().st_ino != config_stat.st_ino or CONFIG.stat().st_mtime_ns != config_stat.st_mtime_ns:
            raise RuntimeError("worker_config_metadata_changed")
        if not _mark_complete(state_path):
            raise RuntimeError("transition_state_lock_busy")
        watchdog_proc.wait(timeout=5)
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
