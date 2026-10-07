#!/usr/bin/env python3
"""Controlled migration of the local AGATA Worker LaunchAgent runtime.

The plan is read-only. Apply is fail-closed on non-main/dirty source, active
inference, a concurrent transition, invalid credentials/configuration, or any
failed health/restart check. VPS, database, and MAXIBOT are never contacted.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

LABEL = "one.gpoint.agata-codex-worker"
HOME = Path.home()
ROOT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
LEGACY = ROOT
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
TRANSITION_LOCK = HOME / ".config/gpoint/agata-worker-pool-transition.lock"
RELEASE_LOCK = HOME / ".config/gpoint/release-production.lock"
CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
WORKER_DIR = Path(__file__).resolve().parent
ASSETS = ("gpoint_mac_worker.py", "agata_worker_pool.py", "lab_queue.py",
          "lab_fixture_manifest.json", "lab_promptfoo_compat_manifest.json",
          "run_codex_worker.sh", "requirements.txt")
QUEUE_SNAPSHOT_MAX_AGE_SECONDS = 30
QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS = 5
LAUNCHAGENT_UNLOAD_TIMEOUT_SECONDS = 20
QUEUE_SNAPSHOT_SCRIPT = WORKER_DIR.parents[2] / "scripts/agata-worker-queue-snapshot.py"
VPS_RUNNER = HOME / ".local/bin/gpoint-vps-runner"
LIVE_QUEUE_SOURCE = "agata-live-queue-snapshot-v1"
QUEUE_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
QUEUE_SOURCE_RE = re.compile(
    r"^agata-live-queue-snapshot-v1:(gpoint|gsales):release=([0-9a-f]{40})"
    r":api=(sha256:[0-9a-f]{64}):core=(sha256:[0-9a-f]{64})"
    r":tenant=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WORKER_DIR.parents[2], check=True,
                          capture_output=True, text=True, timeout=15).stdout.strip()


def launchctl_loaded() -> bool:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            capture_output=True, text=True, timeout=8, check=False)
    if result.returncode != 0:
        return False
    state = re.search(r"^\s*state = ([^\s]+)\s*$", result.stdout or "", re.MULTILINE)
    return bool(state and state.group(1).lower() in {"running", "waiting"})


def _release_lock_busy() -> bool:
    if not RELEASE_LOCK.exists():
        return False
    descriptor = os.open(RELEASE_LOCK, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def safe_worker_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    fields = ("state", "current_job_present", "last_poll_at", "last_heartbeat_at", "last_error_code")
    result = {key: value.get(key) for key in fields if key in value}
    targets = value.get("targets")
    if isinstance(targets, dict):
        result["targets"] = {name: {key: lane.get(key) for key in fields if key in lane}
                              for name, lane in targets.items()
                              if name in {"GPOINT", "GSALES", "LAB"} and isinstance(lane, dict)}
    return result


def _python312_candidate(path: str | None) -> str | None:
    if not path or not Path(path).is_file() or not os.access(path, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, timeout=5, check=False,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = (int(part) for part in result.stdout.strip().split(".", 1))
    except (ValueError, TypeError):
        return None
    return path if (major, minor) >= (3, 12) else None


def _resolve_python312() -> str:
    """Prefer the interpreter running this controller, then its base, then PATH."""
    executable = sys.executable
    if sys.version_info >= (3, 12) and executable and Path(executable).is_file() and os.access(executable, os.X_OK):
        return executable
    base_executable = getattr(sys, "_base_executable", None)
    base = _python312_candidate(base_executable)
    if base:
        return base
    path_candidate = _python312_candidate(shutil.which("python3.12"))
    if path_candidate:
        return path_candidate
    raise RuntimeError("python_312_missing")


def _running_inference_pids() -> list[int]:
    result = subprocess.run(["ps", "-axo", "pid=,command="], check=True,
                            capture_output=True, text=True, timeout=8)
    found = []
    for line in result.stdout.splitlines():
        lower = line.lower()
        if "gpoint_mac_worker.py execute-slot" in lower:
            try:
                found.append(int(line.strip().split(None, 1)[0]))
            except (ValueError, IndexError):
                continue
    for record_path in (LEGACY / "inference-process.json", ROOT / "state/inference-process.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = record.get("pid")
        if pid is None and record.get("state") == "starting":
            found.append(-1)
        elif isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, 0)
                found.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                found.append(pid)
    return found


def _launchagent_pid() -> tuple[int, str]:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=True,
                            capture_output=True, text=True, timeout=8)
    match = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("launchagent_pid_ambiguous")
    pid = int(match.group(1))
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], check=True,
                             capture_output=True, text=True, timeout=8).stdout.strip()
    if "gpoint_mac_worker.py watch" not in command or not any(
            str(root) in command for root in (LEGACY, ROOT / "runtime")):
        raise RuntimeError("launchagent_process_identity_invalid")
    return pid, command


def _wait_launchagent_unloaded(previous_pid: int | None, *,
                               deadline: float = LAUNCHAGENT_UNLOAD_TIMEOUT_SECONDS) -> bool:
    """Wait for launchd to remove the label and for observed worker PIDs to exit."""
    service_id = f"gui/{os.getuid()}/{LABEL}"
    observed_pids = {previous_pid} if isinstance(previous_pid, int) and previous_pid > 1 else set()
    until = time.monotonic() + deadline
    while True:
        result, command_error = _run_readonly_command(["launchctl", "print", service_id], 5)
        output = result.stdout or ""
        error = result.stderr or ""
        if result.returncode == 0 and f"{LABEL} = {{" in output:
            pid_match = re.search(r"^\s*pid = ([0-9]+)\s*$", output, re.MULTILINE)
            if pid_match:
                observed_pids.add(int(pid_match.group(1)))
            service_absent = False
        elif result.returncode != 0 and f'Could not find service "{LABEL}"' in error:
            service_absent = True
        else:
            raise RuntimeError("worker_launchagent_unload_state_ambiguous") from None

        processes, process_error = _run_readonly_command(["ps", "-axo", "pid=,command="], 5)
        if process_error or processes.returncode != 0:
            raise RuntimeError("worker_launchagent_unload_state_ambiguous") from None
        all_pids = set()
        active_pids = set()
        for line in (processes.stdout or "").splitlines():
            try:
                pid = int(line.strip().split(None, 1)[0])
            except (ValueError, IndexError):
                continue
            all_pids.add(pid)
            if "gpoint_mac_worker.py watch" in line:
                active_pids.add(pid)
                observed_pids.add(pid)

        if service_absent and not active_pids and observed_pids.isdisjoint(all_pids):
            return True
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("worker_launchagent_unload_timeout")
        time.sleep(min(0.25, remaining))


def _pause_idle_launchagent() -> int | None:
    if not launchctl_loaded():
        return None
    pid, _command = _launchagent_pid()
    os.kill(pid, signal.SIGSTOP)
    time.sleep(0.2)
    try:
        if inventory()["active_inference"]:
            raise RuntimeError("active_inference_blocks_transition")
    except Exception:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        raise
    return pid


def inventory() -> dict:
    if LEGACY.is_symlink() or (ROOT.exists() and ROOT.is_symlink()):
        raise RuntimeError("worker_runtime_root_symlink_invalid")
    if not CONFIG.is_file() or CONFIG.is_symlink() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    if not CODEX.is_file():
        raise RuntimeError("codex_cli_missing")
    legacy_status = safe_worker_status(LEGACY / "worker-status.json")
    new_status = safe_worker_status(ROOT / "status/worker-status.json")
    pids = _running_inference_pids()
    lab_running = sum(len(list((queue / "running").glob("*.json")))
                      for queue in (LEGACY / "lab-queue", ROOT / "queues/lab")
                      if (queue / "running").is_dir())
    if PLIST.is_symlink() or (PLIST.exists() and PLIST.stat().st_uid != os.getuid()):
        raise RuntimeError("launchagent_plist_owner_invalid")
    old_plist = PLIST.read_bytes() if PLIST.is_file() else b""
    old_args = []
    if old_plist:
        try:
            old_args = plistlib.loads(old_plist).get("ProgramArguments", [])
        except Exception as exc:
            raise RuntimeError("launchagent_plist_invalid") from exc
    def active_status(status: dict) -> bool:
        if status.get("current_job_present") or status.get("state") == "running":
            return True
        targets = status.get("targets", {})
        return any(isinstance(value, dict) and (value.get("current_job_present") or value.get("state") == "running")
                   for value in targets.values()) if isinstance(targets, dict) else False
    active = bool(pids or lab_running or active_status(legacy_status) or active_status(new_status))
    return {
        "label": LABEL,
        "launchagent_loaded": launchctl_loaded(),
        "production_release_lock_busy": _release_lock_busy(),
        "launchagent_program_arguments": old_args,
        "legacy_runtime_present": LEGACY.exists(),
        "legacy_worker_status": legacy_status,
        "new_worker_status": new_status,
        "active_inference_pids": pids,
        "lab_running_jobs": lab_running,
        "active_inference": active,
        "external_config_present_mode_0600": True,
        "codex_cli_present": True,
        "configured_concurrency": 1,
        "provider_parallel_capability": "NOT_VERIFIED",
    }


def candidate() -> dict:
    head = git("rev-parse", "HEAD")
    main = git("rev-parse", "origin/main")
    try:
        remote_result = git("ls-remote", "--exit-code", "origin", "refs/heads/main")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("origin_main_unavailable") from exc
    remote_lines = remote_result.splitlines()
    if len(remote_lines) != 1:
        raise RuntimeError("origin_main_unavailable")
    remote_fields = remote_lines[0].split()
    if (len(remote_fields) != 2 or remote_fields[1] != "refs/heads/main"
            or not re.fullmatch(r"[0-9a-f]{40}", remote_fields[0])):
        raise RuntimeError("origin_main_unavailable")
    remote_main = remote_fields[0]
    if main != remote_main:
        raise RuntimeError("origin_main_ref_stale")
    if head != remote_main:
        raise RuntimeError("candidate_must_be_origin_main")
    if git("status", "--porcelain"):
        raise RuntimeError("candidate_worktree_dirty")
    assets = {name: sha256(WORKER_DIR / name) for name in ASSETS}
    bundle_hash = hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source_sha": head, "assets": assets, "bundle_sha256": bundle_hash}


def read_only_plan() -> dict:
    return {"mode": "READ_ONLY_PLAN", "candidate": candidate(), "inventory": inventory(),
            "runtime_root": str(ROOT), "legacy_root": str(LEGACY), "mutation_performed": False}


def validate_fresh_production_queue_snapshots(status: dict, *, now: datetime | None = None) -> dict:
    """Validate fresh, content-free queue snapshots without requiring idleness."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or not isinstance(status, dict):
        raise RuntimeError("production_queue_snapshot_invalid")
    pool = status.get("pool")
    busy = pool.get("busy") if isinstance(pool, dict) else None
    pool_authority = status.get("pool_snapshot_authority", "worker_pool_status_v1")
    if pool_authority == "worker_pool_status_v1":
        pool_state_valid = isinstance(pool, dict) and pool.get("state") == "HEALTHY"
    elif pool_authority == "mac_worker_live_idle_inventory_v1":
        inventory = status.get("pool_snapshot_inventory")
        pool_state_valid = (
            isinstance(pool, dict) and pool.get("state") == "IDLE_CONFIRMED"
            and isinstance(inventory, dict)
            and inventory == {
                "configured_concurrency": 1,
                "slot_count": 1,
                "all_slots_idle": True,
                "current_job_present": False,
                "launch_agent_loaded": True,
                "active_inference": False,
            }
            and isinstance(total := pool.get("total"), int) and not isinstance(total, bool)
            and total == 1
            and isinstance(status.get("configured_concurrency"), int)
            and not isinstance(status.get("configured_concurrency"), bool)
            and status.get("configured_concurrency") == 1
            and status.get("current_job_present") is False
            and status.get("launch_agent_loaded") is True
            and isinstance(status.get("slots"), dict)
            and len(status["slots"]) == 1
            and all(isinstance(slot, dict) and slot.get("state") == "IDLE"
                    and slot.get("job_id") is None for slot in status["slots"].values())
        )
    else:
        raise RuntimeError("production_queue_snapshot_invalid")
    total = pool.get("total") if isinstance(pool, dict) else None
    if (not pool_state_valid or status.get("launch_agent_loaded") is not True
            or isinstance(busy, bool) or not isinstance(busy, int) or busy < 0):
        raise RuntimeError("production_queue_snapshot_invalid")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < busy):
        raise RuntimeError("production_queue_snapshot_invalid")
    try:
        pool_captured = datetime.fromisoformat(status.get("pool_snapshot_at", "").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("production_queue_snapshot_invalid") from exc
    if pool_captured.tzinfo is None:
        raise RuntimeError("production_queue_snapshot_invalid")
    pool_age = (current - pool_captured).total_seconds()
    if pool_age > QUEUE_SNAPSHOT_MAX_AGE_SECONDS or pool_age < -QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise RuntimeError("production_queue_snapshot_stale")
    lanes = status.get("lanes")
    if not isinstance(lanes, dict):
        raise RuntimeError("production_queue_snapshot_invalid")
    evidence = {}
    for lane in ("GPOINT", "GSALES"):
        value = lanes.get(lane)
        if not isinstance(value, dict) or value.get("health") != "HEALTHY":
            raise RuntimeError("production_queue_snapshot_invalid")
        if value.get("queue_snapshot_state") != "VERIFIED":
            raise RuntimeError("production_queue_snapshot_invalid")
        api_source = f"agata-worker-queue-status-v1:{lane.lower()}"
        live_source = f"{LIVE_QUEUE_SOURCE}:{lane.lower()}"
        source = value.get("queue_snapshot_source_id")
        if source == live_source:
            _validate_live_queue_snapshot_provenance(value, lane)
        elif source != api_source or value.get("queue_snapshot_provenance") is not None:
            raise RuntimeError("production_queue_snapshot_invalid")
        counts = (value.get("pending_inference_jobs"), value.get("running_inference_jobs"))
        if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
            raise RuntimeError("production_queue_snapshot_invalid")
        try:
            captured = datetime.fromisoformat(value.get("queue_snapshot_at", "").replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("production_queue_snapshot_invalid") from exc
        if captured.tzinfo is None:
            raise RuntimeError("production_queue_snapshot_invalid")
        age = (current - captured).total_seconds()
        if age > QUEUE_SNAPSHOT_MAX_AGE_SECONDS or age < -QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS:
            raise RuntimeError("production_queue_snapshot_stale")
        evidence[lane] = {
            "source_id": value["queue_snapshot_source_id"],
            "snapshot_at": captured.isoformat(),
            "pending_jobs": counts[0],
            "running_jobs": counts[1],
        }
    return {"checked_at": current.isoformat(), "pool_snapshot_at": pool_captured.isoformat(),
            "pool_authority": pool_authority,
            "busy_slots": busy, "lanes": evidence}


def require_fresh_empty_production_queues(status: dict, *, now: datetime | None = None) -> dict:
    """Require both current production queues and all worker slots to be idle."""
    evidence = validate_fresh_production_queue_snapshots(status, now=now)
    if evidence["busy_slots"] != 0:
        raise RuntimeError("worker_slots_busy")
    if any(item["pending_jobs"] or item["running_jobs"] for item in evidence["lanes"].values()):
        raise RuntimeError("production_queue_not_empty")
    return evidence


def _validate_live_queue_snapshot_provenance(value: dict, lane: str) -> dict:
    provenance = value.get("queue_snapshot_provenance")
    if not isinstance(provenance, dict):
        raise RuntimeError("production_queue_snapshot_invalid")
    match = QUEUE_SOURCE_RE.fullmatch(provenance.get("source_id", ""))
    tenant_id = provenance.get("tenant_id")
    visible_tenant_rows = provenance.get("visible_tenant_rows")
    try:
        tenant = UUID(tenant_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("production_queue_snapshot_invalid") from exc
    if (match is None or match.group(1) != lane.lower()
            or provenance.get("snapshot_version") != "agata_live_queue_snapshot_v1"
            or provenance.get("lane") != lane
            or provenance.get("source_system") != lane.lower()
            or not re.fullmatch(r"[0-9a-f]{40}", provenance.get("release_sha", ""))
            or provenance.get("release_sha") != match.group(2)
            or provenance.get("api_build_sha") != match.group(2)
            or provenance.get("core_build_sha") != match.group(2)
            or not QUEUE_IMAGE_RE.fullmatch(provenance.get("api_image_id", ""))
            or provenance.get("api_image_id") != match.group(3)
            or not QUEUE_IMAGE_RE.fullmatch(provenance.get("core_image_id", ""))
            or provenance.get("core_image_id") != match.group(4)
            or tenant.int == 0 or str(tenant) != tenant_id or tenant_id != match.group(5)
            or provenance.get("tenant_identity_verified") is not True
            or provenance.get("transaction_read_only") is not True
            or provenance.get("same_login") is not True
            or provenance.get("role_superuser") is not False
            or provenance.get("role_bypassrls") is not False
            or isinstance(visible_tenant_rows, bool) or visible_tenant_rows != 1
            or not isinstance(provenance.get("database_name"), str)
            or not provenance.get("database_name")
            or value.get("queue_snapshot_runtime_release_sha") != match.group(2)
            or value.get("queue_snapshot_tenant_id") != tenant_id):
        raise RuntimeError("production_queue_snapshot_invalid")
    return provenance


def normalize_live_runtime_queue_snapshot(snapshot: dict, lane: str, expected_tenant_id: str) -> dict:
    """Bind a source-authenticated DB snapshot to one configured worker lane."""
    if lane not in {"GPOINT", "GSALES"} or not isinstance(snapshot, dict):
        raise RuntimeError("production_queue_snapshot_invalid")
    try:
        captured = datetime.fromisoformat(snapshot.get("snapshot_at", "").replace("Z", "+00:00"))
        tenant = UUID(snapshot.get("tenant_id", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("production_queue_snapshot_invalid") from exc
    if captured.tzinfo is None or tenant.int == 0 or str(tenant) != snapshot.get("tenant_id"):
        raise RuntimeError("production_queue_snapshot_invalid")
    if snapshot.get("tenant_id") != expected_tenant_id:
        raise RuntimeError("production_queue_snapshot_invalid")
    counts = (snapshot.get("pending_inference_jobs"), snapshot.get("running_inference_jobs"))
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
        raise RuntimeError("production_queue_snapshot_invalid")
    provenance = {
        key: snapshot.get(key) for key in (
            "snapshot_version", "source_id", "lane", "source_system", "release_sha",
            "api_build_sha", "core_build_sha", "api_image_id", "core_image_id",
            "tenant_id", "tenant_identity_verified", "transaction_read_only", "same_login",
            "role_superuser", "role_bypassrls", "visible_tenant_rows", "database_name",
        )
    }
    result = {
        "health": "HEALTHY",
        "queue_snapshot_state": "VERIFIED",
        "queue_snapshot_source_id": f"{LIVE_QUEUE_SOURCE}:{lane.lower()}",
        "queue_snapshot_at": captured.isoformat(),
        "pending_inference_jobs": counts[0],
        "running_inference_jobs": counts[1],
        "queue_snapshot_authority": "live_runtime_readonly_snapshot_v1",
        "queue_snapshot_runtime_release_sha": snapshot.get("release_sha"),
        "queue_snapshot_tenant_id": snapshot.get("tenant_id"),
        "queue_snapshot_provenance": provenance,
    }
    _validate_live_queue_snapshot_provenance(result, lane)
    return result


def _configured_tenant_id(lane: str, manifest: dict) -> str:
    key = f"{lane}_AGATA_TENANT_ID"
    if not CONFIG.is_file() or CONFIG.is_symlink() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    values = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item and not item.startswith("#") and "=" in item:
            name, value = item.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    tenant_id = values.get(key, "")
    if lane == "GPOINT" and not tenant_id:
        binding = _validated_current_tenant_binding()
        if binding is None:
            binding = _validated_legacy_tenant_binding(manifest)
        tenant_id = binding.get("tenant_id", "") if binding else ""
    try:
        tenant = UUID(tenant_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("worker_tenant_binding_invalid") from exc
    if tenant.int == 0 or str(tenant) != tenant_id:
        raise RuntimeError("worker_tenant_binding_invalid")
    return tenant_id


def _validated_current_tenant_binding() -> dict | None:
    """Validate the durable GPOINT pin against the currently installed runtime."""
    path = ROOT / "config/tenant-bindings.json"
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        file_stat = path.stat()
        value = json.loads(path.read_text(encoding="utf-8"))
        runtime_link = ROOT / "runtime/current"
        if not runtime_link.is_symlink():
            raise ValueError
        runtime = runtime_link.resolve(strict=True)
        runtime_root = (ROOT / "runtime").resolve(strict=True)
        worker = runtime / "gpoint_mac_worker.py"
        source_id = value.get("source_id", "")
        tenant_id = value.get("tenant_id", "")
        captured = datetime.fromisoformat(str(value.get("snapshot_at", "")).replace("Z", "+00:00"))
        tenant = UUID(tenant_id)
        if (file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077
                or set(value) != {"version", "candidate_sha", "worker_sha256", "tenant_id", "source_id", "snapshot_at"}
                or value.get("version") != 1
                or not re.fullmatch(r"[0-9a-f]{40}", value.get("candidate_sha", ""))
                or value.get("candidate_sha") != runtime.name
                or not re.fullmatch(r"[0-9a-f]{64}", value.get("worker_sha256", ""))
                or not worker.is_file() or worker.is_symlink()
                or value.get("worker_sha256") != sha256(worker)
                or not runtime.is_relative_to(runtime_root)
                or not re.fullmatch(r"gpoint-vps:[0-9a-f]{40}:api=sha256:[0-9a-f]{64}:agata=sha256:[0-9a-f]{64}", source_id)
                or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tenant_id)
                or tenant.int == 0 or str(tenant) != tenant_id or captured.tzinfo is None):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("worker_tenant_binding_invalid") from exc
    return value


def _remote_queue_snapshot_command(lane: str) -> str:
    targets = {
        "GPOINT": ("compose-api-1", "compose-agata-api-1", "AGATA_TENANT_ID"),
        "GSALES": ("compose-gsales-api-1", "compose-gsales-agata-api-1", "GSALES_AGATA_TENANT_ID"),
    }
    if lane not in targets or not QUEUE_SNAPSHOT_SCRIPT.is_file():
        raise RuntimeError("authoritative_queue_snapshot_unavailable")
    api, core, tenant_key = targets[lane]
    script = base64.b64encode(QUEUE_SNAPSHOT_SCRIPT.read_bytes()).decode("ascii")
    return f"""set -eu
env_file=/opt/gpoint/env/production.env
release_sha=$(sed -n 's/^RELEASE_SHA=//p' "$env_file" | head -1)
case "$release_sha" in *[!0-9a-f]*|'') exit 41 ;; esac
test "${{#release_sha}}" -eq 40
tenant=$(sed -n 's/^{tenant_key}=//p' "$env_file" | head -1)
api={api}
core={core}
for container in "$api" "$core"; do
  running=$(docker inspect "$container" --format '{{{{.State.Running}}}}')
  health=$(docker inspect "$container" --format '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}')
  image=$(docker inspect "$container" --format '{{{{.Image}}}}')
  revision=$(docker image inspect "$image" --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}')
  test "$running" = true && test "$health" = healthy && test "$revision" = "$release_sha"
done
api_image=$(docker inspect "$api" --format '{{{{.Image}}}}')
core_image=$(docker inspect "$core" --format '{{{{.Image}}}}')
api_sha=$(docker image inspect "$api_image" --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}')
core_sha=$(docker image inspect "$core_image" --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}')
printf '%s' '{script}' | base64 -d | docker exec -i -w /srv/agata \\
  -e SNAPSHOT_LANE={lane} \\
  -e SNAPSHOT_EXPECTED_TENANT_ID="$tenant" \\
  -e SNAPSHOT_RELEASE_SHA="$release_sha" \\
  -e SNAPSHOT_API_BUILD_SHA="$api_sha" \\
  -e SNAPSHOT_CORE_BUILD_SHA="$core_sha" \\
  -e SNAPSHOT_API_IMAGE_ID="$api_image" \\
  -e SNAPSHOT_CORE_IMAGE_ID="$core_image" \\
  "$core" python -"""


def _read_live_runtime_queue_snapshot(lane: str, expected_tenant_id: str) -> dict:
    if not VPS_RUNNER.is_file() or not os.access(VPS_RUNNER, os.X_OK):
        raise RuntimeError("authoritative_queue_snapshot_runner_unavailable")
    env = dict(os.environ)
    env["REMOTE_CMD"] = _remote_queue_snapshot_command(lane)
    env["GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS"] = "25"
    try:
        result = subprocess.run([str(VPS_RUNNER)], capture_output=True, text=True,
                                timeout=30, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("authoritative_queue_snapshot_unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("authoritative_queue_snapshot_failed")
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        snapshot = json.loads(lines[-1])
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("authoritative_queue_snapshot_invalid") from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError("authoritative_queue_snapshot_invalid")
    if snapshot.get("preflight_error"):
        raise RuntimeError("authoritative_queue_snapshot_failed")
    return normalize_live_runtime_queue_snapshot(snapshot, lane, expected_tenant_id)


def _refresh_local_pool_evidence(status: dict) -> None:
    """Record local idle inventory separately from health and production queue state."""
    pool = status.get("pool")
    slots = status.get("slots")
    lanes = status.get("lanes")
    concurrency = status.get("configured_concurrency")
    total = pool.get("total") if isinstance(pool, dict) else None
    if (not isinstance(pool, dict) or isinstance(pool.get("busy"), bool) or pool.get("busy") != 0
            or isinstance(concurrency, bool) or concurrency != 1
            or isinstance(total, bool) or total != 1
            or not isinstance(slots, dict) or len(slots) != 1
            or any(not isinstance(slot, dict) or slot.get("state") != "IDLE"
                   or slot.get("job_id") is not None
                                for slot in slots.values())
            or not isinstance(lanes, dict) or status.get("current_job_present") is not False
            or status.get("launch_agent_loaded") is not True):
        raise RuntimeError("production_queue_status_unavailable")
    for lane in ("GPOINT", "GSALES"):
        value = lanes.get(lane)
        if not isinstance(value, dict):
            raise RuntimeError("production_queue_status_unavailable")
        if value.get("configured", True) and value.get("health") != "HEALTHY":
            raise RuntimeError("production_queue_status_unavailable")
    local = inventory()
    if (local.get("launchagent_loaded") is not True or local.get("production_release_lock_busy")
            or local.get("active_inference") is not False):
        raise RuntimeError("production_queue_status_unavailable")
    _launchagent_pid()
    pool["state"] = "IDLE_CONFIRMED"
    status["pool_snapshot_at"] = datetime.now(UTC).isoformat()
    status["pool_snapshot_authority"] = "mac_worker_live_idle_inventory_v1"
    status["pool_snapshot_inventory"] = {
        "configured_concurrency": 1,
        "slot_count": 1,
        "all_slots_idle": True,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "active_inference": False,
    }


def _read_production_queue_status(manifest: dict | None = None) -> dict:
    """Read status and bootstrap queue evidence only for explicit endpoint 404 version skew."""
    runtime_link = ROOT / "runtime/current"
    try:
        if not runtime_link.is_symlink():
            raise RuntimeError("installed_worker_runtime_unavailable")
        runtime = runtime_link.resolve(strict=True)
        runtime_root = (ROOT / "runtime").resolve(strict=True)
        if not runtime.is_relative_to(runtime_root):
            raise RuntimeError("installed_worker_runtime_invalid")
        worker = WORKER_DIR / "gpoint_mac_worker.py"
        python = runtime / ".venv/bin/python3.12"
        if (not worker.is_file() or worker.is_symlink()
                or not python.is_file() or not os.access(python, os.X_OK)):
            raise RuntimeError("installed_worker_runtime_unavailable")
    except OSError as exc:
        raise RuntimeError("installed_worker_runtime_unavailable") from exc
    try:
        result = subprocess.run(
            [str(python), str(worker), "status"], capture_output=True,
            text=True, timeout=35, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("production_queue_status_unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("production_queue_status_unavailable")
    try:
        status = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("production_queue_status_unavailable") from exc
    lanes = status.get("lanes") if isinstance(status, dict) else None
    if not isinstance(lanes, dict):
        raise RuntimeError("production_queue_status_unavailable")
    configured_lanes = [lane for lane in ("GPOINT", "GSALES")
                        if isinstance(lanes.get(lane), dict) and lanes[lane].get("configured", True)]
    if not configured_lanes:
        raise RuntimeError("production_queue_status_unavailable")
    if all(lanes[lane].get("queue_snapshot_state") == "VERIFIED" for lane in configured_lanes):
        return status
    if any(lanes[lane].get("health") != "HEALTHY"
           or lanes[lane].get("queue_snapshot_error_code") != "worker_gateway_http_404"
           for lane in configured_lanes):
        raise RuntimeError("production_queue_status_unavailable")
    _refresh_local_pool_evidence(status)
    candidate_manifest = manifest or candidate()
    for lane in configured_lanes:
        expected_tenant = _configured_tenant_id(lane, candidate_manifest)
        snapshot = _read_live_runtime_queue_snapshot(lane, expected_tenant)
        lanes[lane].update(snapshot)
    return status


def _save_original_launchagent(rollback_tree: Path) -> bytes:
    if not PLIST.is_file() or PLIST.is_symlink():
        raise RuntimeError("launchagent_plist_missing_or_invalid")
    original = PLIST.read_bytes()
    try:
        value = plistlib.loads(original)
    except Exception as exc:
        raise RuntimeError("launchagent_plist_invalid") from exc
    expected_arguments = (
        [str(LEGACY / "run_codex_worker.sh"), "watch"],
        [str(ROOT / "runtime/current/run_codex_worker.sh"), "watch"],
    )
    if value.get("Label") != LABEL or value.get("ProgramArguments") not in expected_arguments:
        raise RuntimeError("launchagent_identity_invalid")
    temporary = rollback_tree / f".original-launchagent-{os.getpid()}.tmp"
    temporary.write_bytes(original)
    temporary.chmod(0o600)
    os.replace(temporary, rollback_tree / "original-launchagent.plist")
    return original


def prepare() -> dict:
    """Stage candidate and verify rollback evidence without stopping the worker."""
    manifest = candidate()
    before = inventory()
    if before["production_release_lock_busy"]:
        raise RuntimeError("production_release_lock_busy")
    if before["active_inference"]:
        raise RuntimeError("active_inference_blocks_transition")
    expected_program = str(LEGACY / "run_codex_worker.sh")
    current_program = str(ROOT / "runtime/current/run_codex_worker.sh")
    if before["launchagent_program_arguments"] not in (
            [expected_program, "watch"], [current_program, "watch"]):
        raise RuntimeError("launchagent_identity_invalid")
    staged_runtime = _stage_runtime(manifest)
    startup_check = _validate_candidate_startup(staged_runtime, manifest)
    rollback_tree = _ensure_rollback_tree(manifest["source_sha"])
    original_plist = _save_original_launchagent(rollback_tree)
    backup = rollback_tree / "legacy-runtime"
    last_error = None
    for _attempt in range(3):
        try:
            _prepare_legacy_snapshot(backup)
            break
        except (RuntimeError, OSError) as exc:
            # The live worker atomically replaces small status files while the
            # snapshot is copied; copytree may report that transient as
            # FileNotFoundError/shutil.Error rather than RuntimeError.
            last_error = exc
    else:
        raise RuntimeError("rollback_snapshot_not_stable") from last_error
    # _prepare_legacy_snapshot verifies its completed copy against the live
    # tree at capture time. The running legacy worker may legitimately advance
    # status/log files immediately afterward, so a second comparison here
    # would reject a sound snapshot due only to that concurrent activity.
    if not backup.is_dir() or (rollback_tree / "original-launchagent.plist").read_bytes() != original_plist:
        raise RuntimeError("rollback_snapshot_verification_failed")
    _verify_staged_runtime(staged_runtime, manifest)
    return {
        "state": "PREPARED", "source_sha": manifest["source_sha"],
        "bundle_sha256": manifest["bundle_sha256"], "staged_runtime": str(staged_runtime),
        "rollback_snapshot": str(backup), "original_plist_sha256": hashlib.sha256(original_plist).hexdigest(),
        "startup_check": startup_check,
        "active_inference": False, "launchagent_stopped": False,
        "runtime_current_changed": False,
    }


def _atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _rollback_snapshot_base() -> Path:
    return ROOT.parent / ".agata-codex-worker-rollback"


def _runtime_staging_base() -> Path:
    return ROOT.parent / ".agata-codex-worker-staging"


def _ensure_rollback_tree(source_sha: str) -> Path:
    """Create a private sibling snapshot tree so it survives in-place restore."""
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("rollback_source_sha_invalid")
    base = _rollback_snapshot_base()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.is_symlink() or base.stat().st_uid != os.getuid():
        raise RuntimeError("worker_rollback_directory_invalid")
    base.chmod(0o700)
    rollback = base / source_sha
    try:
        # mkdir publishes the entire empty rollback root in one filesystem
        # operation. Snapshot files are written only after this succeeds.
        rollback.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if rollback.is_symlink() or not rollback.is_dir() or rollback.stat().st_uid != os.getuid():
        raise RuntimeError("worker_rollback_directory_invalid")
    if rollback.stat().st_mode & 0o077:
        raise RuntimeError("worker_rollback_directory_permissions_invalid")
    return rollback


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _redact_command_output(value: str | bytes | None) -> str:
    """Keep command diagnostics while withholding values from secret env lines."""
    secret_names = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")
    lines = []
    for line in _output_text(value).splitlines():
        upper = line.upper()
        if not any(name in upper for name in secret_names):
            lines.append(line)
        elif "=>" in line:
            lines.append(f"{line.split('=>', 1)[0].rstrip()} => <redacted>")
        elif "=" in line:
            lines.append(f"{line.split('=', 1)[0].rstrip()}=<redacted>")
        else:
            argument = re.search(r"(?i)((?:--)?[\w.-]*(?:token|secret|password|api[_-]?key|private[_-]?key)[\w.-]*\s+)\S+", line)
            lines.append(f"{line[:argument.start(0)]}{argument.group(1)}<redacted>" if argument else "<redacted secret-bearing output>")
    return "\n".join(lines)


def _diagnostic_base() -> Path:
    return ROOT.parent / ".agata-codex-worker-diagnostics"


def _create_diagnostic_attempt(source_sha: str, attempt_id: str | None = None) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("diagnostic_source_sha_invalid")
    attempt_id = attempt_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", attempt_id):
        raise RuntimeError("diagnostic_attempt_id_invalid")
    base = _diagnostic_base()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if base.is_symlink() or base.stat().st_uid != os.getuid():
        raise RuntimeError("diagnostic_directory_invalid")
    base.chmod(0o700)
    source_dir = base / source_sha
    source_dir.mkdir(mode=0o700, exist_ok=True)
    if source_dir.is_symlink() or source_dir.stat().st_uid != os.getuid():
        raise RuntimeError("diagnostic_directory_invalid")
    source_dir.chmod(0o700)
    attempt_dir = source_dir / attempt_id
    attempt_dir.mkdir(mode=0o700)
    if attempt_dir.is_symlink() or attempt_dir.stat().st_uid != os.getuid():
        raise RuntimeError("diagnostic_directory_invalid")
    for name in ("candidate.stdout.log", "candidate.stderr.log"):
        path = attempt_dir / name
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        path.chmod(0o600)
    return attempt_dir


def _diagnostic_secret_values(extra: list[str] | None = None) -> list[str]:
    values = set(value for value in (extra or []) if isinstance(value, str) and len(value) >= 4)
    secret_key = re.compile(r"(?i)(token|secret|password|api[_-]?key|private[_-]?key|credential|auth)")
    for key, value in os.environ.items():
        if secret_key.search(key) and value:
            values.add(value)
    try:
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if secret_key.search(key) and value.strip():
                values.add(value.strip().strip('"').strip("'"))
    except OSError:
        pass
    return sorted(values, key=len, reverse=True)


def _redact_diagnostic_text(value: str | bytes | None, secrets: list[str] | None = None) -> str:
    text = _output_text(value)
    for secret in _diagnostic_secret_values(secrets):
        text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)((?:[\w.-]*(?:token|secret|password|api[_-]?key|private[_-]?key)[\w.-]*)\s*(?:=>|=|:)\s*)[^\s,;]+",
        r"\1<redacted>", text,
    )
    return _redact_command_output(text)


def _safe_error_code(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    message = str(exc)
    safe_codes = {
        "worker_config_permissions_invalid", "worker_gateway_or_auth_unconfigured",
        "worker_gateway_url_invalid", "worker_pool_config_key_invalid",
        "worker_pool_must_start_at_n1_unverified", "worker_poll_interval_invalid",
        "worker_root_must_be_absolute", "worker_target_unconfigured",
        "worker_targets_must_be_distinct", "worker_tenant_binding_invalid",
        "worker_tenants_must_be_distinct", "worker_concurrency_invalid",
        "parallel_provider_capability_invalid", "parallel_provider_capability_not_verified",
        "worker_path_owner_or_symlink_invalid", "worker_path_permissions_invalid",
        "worker_path_parent_not_writable", "worker_runtime_asset_missing",
        "worker_startup_check_contract_invalid",
    }
    if message in safe_codes:
        return message
    return "worker_initialization_failed"


def _write_diagnostic_evidence(
    directory: Path,
    *,
    manifest: dict,
    attempt_id: str,
    rendered_plist: bytes,
    launchctl_print: str = "",
    process_evidence: dict | None = None,
    command_evidence: dict | None = None,
    error: BaseException | None = None,
    secrets: list[str] | None = None,
    stdout: str | bytes | None = None,
    stderr: str | bytes | None = None,
    started_at: str | None = None,
) -> Path:
    secrets = _diagnostic_secret_values(secrets)
    log_text = {}
    for name, supplied in (("candidate.stdout.log", stdout), ("candidate.stderr.log", stderr)):
        path = directory / name
        raw = _output_text(supplied) if supplied is not None else (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        safe = _redact_diagnostic_text(raw, secrets)
        path.write_text(safe + ("\n" if safe and not safe.endswith("\n") else ""), encoding="utf-8")
        path.chmod(0o600)
        log_text[name.removesuffix(".log")] = safe

    def redact_object(value):
        if isinstance(value, str):
            return _redact_diagnostic_text(value, secrets)
        if isinstance(value, dict):
            return {str(key): redact_object(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_object(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return None

    safe_launchctl = _redact_diagnostic_text(launchctl_print, secrets)
    exit_match = re.search(r"last exit code\s*=\s*(\d+)", safe_launchctl)
    evidence = {
        "kind": "AGATA_WORKER_CANDIDATE_DIAGNOSTICS",
        "attempt_id": attempt_id,
        "source_sha": manifest.get("source_sha"),
        "bundle_sha256": manifest.get("bundle_sha256"),
        "started_at": started_at,
        "captured_at": datetime.now(UTC).isoformat(),
        "rendered_plist_sha256": hashlib.sha256(rendered_plist).hexdigest(),
        "stdout": log_text["candidate.stdout"],
        "stderr": log_text["candidate.stderr"],
        "launchctl_print": safe_launchctl,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        "process_evidence": redact_object(process_evidence or {}),
        "command_evidence": redact_object(command_evidence or {}),
        "error": ({"type": type(error).__name__, "code": _safe_error_code(error)} if error else None),
    }
    path = directory / "candidate-launch.json"
    _atomic_json(path, evidence)
    return path


def _launchagent_environment() -> dict[str, str]:
    """Return the same explicit non-secret environment rendered into the plist."""
    return {
        "HOME": str(HOME),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "GPOINT_CODEX_CLI": str(CODEX),
        "GPOINT_AGATA_WORKER_CONFIG": str(CONFIG),
        "GPOINT_AGATA_WORKER_SUPPORT": str(ROOT),
        "AGATA_WORKER_POOL_CONFIG": str(ROOT / "config/pool.env"),
    }


def _config_declares_key(key: str) -> bool:
    """Inspect only config key names so startup preflight never logs values."""
    if not CONFIG.exists():
        return False
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise RuntimeError("worker_config_permissions_invalid")
    try:
        stat = CONFIG.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("worker_config_permissions_invalid")
        return any(
            line.split("=", 1)[0].strip() == key
            for line in CONFIG.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
    except OSError as exc:
        raise RuntimeError("worker_config_permissions_invalid") from exc


def _validated_legacy_tenant_binding(manifest: dict) -> dict | None:
    """Return only a source-verified legacy binding eligible for migration."""
    source = LEGACY / "tenant-bindings.json"
    destination = ROOT / "config/tenant-bindings.json"
    if destination.exists() or not source.exists():
        return None
    try:
        if source.is_symlink() or not source.is_file():
            raise ValueError
        stat = source.stat()
        value = json.loads(source.read_text(encoding="utf-8"))
        captured = datetime.fromisoformat(str(value.get("snapshot_at", "")).replace("Z", "+00:00"))
        tenant = UUID(value.get("tenant_id", ""))
        source_id = value.get("source_id", "")
        if (stat.st_uid != os.getuid() or stat.st_mode & 0o077
                or set(value) != {"version", "candidate_sha", "worker_sha256", "tenant_id", "source_id", "snapshot_at"}
                or value.get("version") != 1
                or value.get("worker_sha256") != sha256(LEGACY / "gpoint_mac_worker.py")
                or not isinstance(value.get("candidate_sha"), str) or len(value["candidate_sha"]) != 40
                or tenant.int == 0 or str(tenant) != value.get("tenant_id")
                or captured.tzinfo is None
                or not re.fullmatch(r"gpoint-vps:[0-9a-f]{40}:api=sha256:[0-9a-f]{64}:agata=sha256:[0-9a-f]{64}", source_id)):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("legacy_tenant_binding_invalid") from exc
    value["candidate_sha"] = manifest["source_sha"]
    value["worker_sha256"] = manifest["assets"]["gpoint_mac_worker.py"]
    return value


def _candidate_startup_environment(manifest: dict) -> dict[str, str]:
    """Validate an eligible legacy pin in memory without writing live config."""
    environment = _launchagent_environment()
    if _config_declares_key("GPOINT_AGATA_TENANT_ID"):
        return environment
    binding = _validated_current_tenant_binding()
    if binding is None:
        binding = _validated_legacy_tenant_binding(manifest)
    if binding:
        environment["GPOINT_AGATA_TENANT_ID"] = binding["tenant_id"]
    return environment


def _validate_candidate_startup(staged_runtime: Path, manifest: dict) -> dict:
    attempt_id = uuid.uuid4().hex
    diagnostics = _create_diagnostic_attempt(manifest["source_sha"], attempt_id)
    python = staged_runtime / ".venv/bin/python3.12"
    worker = staged_runtime / "gpoint_mac_worker.py"
    command = [str(python), str(worker), "validate-startup"]
    started_at = datetime.now(UTC).isoformat()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                check=False, env=_candidate_startup_environment(manifest))
        stdout, stderr = result.stdout, result.stderr
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, returncode = _output_text(exc.stdout), _output_text(exc.stderr), 124
    try:
        status = json.loads(stdout) if returncode == 0 else {}
    except ValueError:
        status = {}
    lanes = status.get("lanes") if isinstance(status, dict) else None
    contract_valid = (
        status.get("state") == "PASS"
        and status.get("configured_concurrency") == 1
        and status.get("provider_parallel_capability") == "NOT_VERIFIED"
        and isinstance(lanes, dict)
        and all(isinstance(lanes.get(lane), dict) and lanes[lane].get("configured") is True
                for lane in ("GPOINT", "GSALES", "LAB"))
        and status.get("claim_requests") == 0
        and status.get("inference_requests") == 0
        and status.get("runtime_current_changed") is False
        and status.get("launchagent_changed") is False
    )
    failure = None if returncode == 0 and contract_valid else RuntimeError(
        "worker_startup_check_contract_invalid" if returncode == 0 else
        stderr.strip() or "worker_startup_check_failed"
    )
    evidence_path = _write_diagnostic_evidence(
        diagnostics, manifest=manifest, attempt_id=attempt_id, rendered_plist=b"",
        command_evidence={"command": command, "returncode": returncode},
        error=failure,
        stdout=stdout, stderr=stderr, started_at=started_at,
    )
    if failure:
        code = _safe_error_code(failure) or "worker_startup_check_failed"
        raise RuntimeError(f"worker_startup_check_failed:{code}:diagnostics={evidence_path}")
    return {"state": "PASS", "result": status, "diagnostics": str(evidence_path)}


def _run_readonly_command(command: list[str], timeout: float) -> tuple[subprocess.CompletedProcess, str | None]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False), None
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, _output_text(exc.stdout), _output_text(exc.stderr)), "timeout"
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", type(exc).__name__), type(exc).__name__


def _runtime_candidate_identity(expected_source_sha: str) -> dict:
    current = ROOT / "runtime/current"
    expected = ROOT / "runtime" / expected_source_sha
    try:
        resolved_current = current.resolve(strict=True)
        resolved_expected = expected.resolve(strict=True)
        manifest_path = resolved_current / "bundle-manifest.json"
        if (not expected.is_dir() or expected.is_symlink() or manifest_path.is_symlink()
                or any((resolved_current / name).is_symlink() or not (resolved_current / name).is_file()
                       for name in ASSETS)):
            raise ValueError("runtime_asset_path_invalid")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_assets = {name: sha256(resolved_current / name) for name in ASSETS}
        bundle_hash = hashlib.sha256(json.dumps(
            actual_assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest_assets = manifest.get("assets") if isinstance(manifest, dict) else None
        passed = (current.is_symlink() and resolved_current == resolved_expected
                  and manifest.get("source_sha") == expected_source_sha
                  and manifest_assets == actual_assets
                  and manifest.get("bundle_sha256") == bundle_hash)
        return {"passed": passed, "expected_source_sha": expected_source_sha,
                "runtime_source_sha": resolved_current.name,
                "resolved_current": str(resolved_current), "bundle_sha256": bundle_hash,
                "manifest_source_sha": manifest.get("source_sha"),
                "asset_parity": manifest_assets == actual_assets}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"passed": False, "expected_source_sha": expected_source_sha,
                "runtime_source_sha": None, "resolved_current": None,
                "bundle_sha256": None, "manifest_source_sha": None, "asset_parity": False}


def _restart_state_snapshot(expected_source_sha: str, command_timeout: float = 8.0) -> dict:
    """Capture one read-only snapshot of service, process, and lane readiness."""
    service_id = f"gui/{os.getuid()}/{LABEL}"
    command_errors = {}
    service_command = ["launchctl", "print", service_id]
    service_result, command_errors["launchctl_print"] = _run_readonly_command(service_command, command_timeout)
    service_output = service_result.stdout or ""
    pid_match = re.search(r"^\s*pid = ([0-9]+)\s*$", service_output, re.MULTILINE)
    pid = int(pid_match.group(1)) if pid_match else None
    pid_exists = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            pid_exists = True
        except ProcessLookupError:
            pass
        except PermissionError:
            pid_exists = True
    process_output = ""
    if pid is not None and pid_exists:
        process_result, command_errors["process"] = _run_readonly_command(
            ["ps", "-p", str(pid), "-o", "command="], command_timeout)
        process_output = process_result.stdout.strip()
    runtime_identity = _runtime_candidate_identity(expected_source_sha)
    runtime = ROOT / "runtime/current"
    expected_runtime = ROOT / "runtime" / expected_source_sha
    process_identity = bool(
        pid_exists and runtime_identity.get("passed") is True
        and str(expected_runtime) in process_output
        and "gpoint_mac_worker.py watch" in process_output
    )
    status_result, command_errors["worker_status"] = _run_readonly_command(
        [str(runtime / ".venv/bin/python3.12"), str(runtime / "gpoint_mac_worker.py"), "status"],
        command_timeout)
    try:
        status = json.loads(status_result.stdout)
    except (ValueError, TypeError):
        status = {}
    lanes = status.get("lanes", {}) if isinstance(status.get("lanes"), dict) else {}
    lane_status = {lane: value for lane, value in lanes.items() if isinstance(value, dict)}
    lab_approval = ROOT / "config/lab-execution-approval.json"
    lab_state = lane_status.get("LAB", {})
    lab_gate = lab_state.get("inference_gate")
    permanent_lane_closed = (
        lab_state.get("state") == "AVAILABLE"
        and lab_state.get("presence") == "PERMANENT"
        and lab_gate in {"CLOSED_NO_APPROVAL", "CLOSED_EXPIRED", "CLOSED_CONSUMED"}
    )
    legacy_lane_closed = (
        (lab_state.get("state") == "AVAILABLE" and lab_gate == "APPROVAL_REQUIRED")
        or (lab_state.get("state") == "DISABLED" and lab_gate == "CLOSED_DISABLED")
    )
    lab_gate_closed = not lab_approval.exists() and not lab_approval.is_symlink() and (
        permanent_lane_closed or legacy_lane_closed
    )

    process_inventory, command_errors["process_inventory"] = _run_readonly_command(
        ["ps", "-axo", "pid=,command="], command_timeout)
    worker_processes = []
    for line in (process_inventory.stdout or "").splitlines():
        try:
            worker_pid = int(line.strip().split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        if "gpoint_mac_worker.py watch" in line:
            worker_processes.append({"pid": worker_pid,
                                     "command": _redact_command_output(line.strip())})
    active_pids = _running_inference_pids()
    no_current_job = not status.get("current_job_present", False) and not any(
        isinstance(value, dict) and value.get("current_job_present")
        for value in lanes.values()
    )
    checks = {
        "service": (service_result.returncode == 0 and f"{LABEL} = {{" in service_output
                    and "state = running" in service_output),
        "pid": pid_exists,
        "process_identity": process_identity,
        "runtime_parity": runtime_identity.get("passed") is True,
        "status": status_result.returncode == 0 and isinstance(status, dict),
        "concurrency": status.get("configured_concurrency") == 1,
        "gpoint": lane_status.get("GPOINT", {}).get("health") == "HEALTHY",
        "gsales": lane_status.get("GSALES", {}).get("health") == "HEALTHY",
        "lab_lane": "LAB" in lanes,
        "lab_approval_fail_closed": lab_gate_closed,
        "no_active_inference": not active_pids and no_current_job,
        "no_duplicate_worker": len(worker_processes) == 1 and worker_processes[0]["pid"] == pid,
    }
    safe_status = {
        "configured_concurrency": status.get("configured_concurrency"),
        "pool": status.get("pool"),
        "lanes": {lane: {key: value for key, value in state.items()
                          if key in {"health", "state", "current_job_present", "approval_required",
                                     "approval_state", "inference_gate", "last_error_code"}}
        for lane, state in lane_status.items() if lane in {"GPOINT", "GSALES", "LAB"}},
    }
    return {
        "passed": all(checks.values()), "checks": checks, "pid": pid,
        "active_inference_pids": active_pids, "worker_processes": worker_processes,
        "command_errors": command_errors,
        "launchctl_print": _redact_command_output(service_output),
        "launchctl_stderr": _redact_command_output(service_result.stderr),
        "process_command": _redact_command_output(process_output),
        "worker_status": safe_status,
        "runtime_identity": runtime_identity,
        "runtime_source_sha": runtime_identity.get("runtime_source_sha"),
    }


def _adjudicate_restart_state(expected_source_sha: str, deadline: float = 30.0) -> dict:
    """Boundedly recheck factual runtime state after an ambiguous restart command."""
    until = time.monotonic() + deadline
    attempts = []
    while time.monotonic() < until:
        remaining = max(1.0, until - time.monotonic())
        try:
            snapshot = _restart_state_snapshot(expected_source_sha, command_timeout=min(8.0, remaining))
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            snapshot = {"passed": False, "error": type(exc).__name__}
        attempts.append(snapshot)
        if snapshot.get("passed"):
            return {"passed": True, "status": snapshot.get("worker_status", {}), "attempts": attempts}
        time.sleep(min(2.0, max(0.0, until - time.monotonic())))
    return {"passed": False, "status": attempts[-1].get("worker_status", {}) if attempts else {},
            "attempts": attempts}


def _kickstart_and_verify(expected_source_sha: str) -> dict:
    """Run restart and decide from bounded factual evidence, not CLI timing."""
    command = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        command_evidence = {
            "outcome": "COMPLETED", "returncode": result.returncode, "timeout_seconds": 20,
            "stdout": _redact_command_output(result.stdout),
            "stderr": _redact_command_output(result.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        command_evidence = {
            "outcome": "AMBIGUOUS_RESTART_COMMAND_RESULT", "timeout_seconds": 20,
            "stdout": _redact_command_output(exc.stdout),
            "stderr": _redact_command_output(exc.stderr),
        }
    except OSError as exc:
        command_evidence = {"outcome": "COMMAND_LAUNCH_ERROR", "timeout_seconds": 20,
                            "error": type(exc).__name__}
    adjudication = _adjudicate_restart_state(expected_source_sha)
    return {"verification": "PASS" if adjudication.get("passed") else "ROLLBACK",
            "command": command_evidence, "adjudication": adjudication}


def _remove_legacy_after_verification(*, health_passed: bool, restart_evidence: dict) -> None:
    if not health_passed or restart_evidence.get("verification") != "PASS":
        raise RuntimeError("legacy_cleanup_requires_full_verification")
    # The old flat worker files share the canonical Application Support root
    # with pool state. Keep them in place; LaunchAgent now uses runtime/current.
    if LEGACY != ROOT:
        _safe_remove_tree(LEGACY)


def _stage_runtime(manifest: dict) -> Path:
    release = ROOT / "runtime" / manifest["source_sha"]
    if release.exists():
        _verify_staged_runtime(release, manifest)
        return release
    staged_release = _runtime_staging_base() / manifest["source_sha"]
    if staged_release.exists():
        _verify_staged_runtime(staged_release, manifest)
        return staged_release
    staging_base = _runtime_staging_base()
    staging_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if staging_base.is_symlink() or staging_base.stat().st_uid != os.getuid():
        raise RuntimeError("worker_staging_directory_invalid")
    staging_base.chmod(0o700)
    stage = staged_release.with_name(f".{staged_release.name}.staging-{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, mode=0o700)
    for name in ASSETS:
        shutil.copy2(WORKER_DIR / name, stage / name)
    # Launchd executes the wrapper directly from ProgramArguments, so preserve
    # an executable mode even if a checkout or archive lost the Git mode bit.
    (stage / "run_codex_worker.sh").chmod(0o700)
    python = _resolve_python312()
    stage_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    subprocess.run([python, "-m", "venv", str(stage / ".venv")], check=True,
                   capture_output=True, timeout=60, env=stage_env)
    subprocess.run([str(stage / ".venv/bin/python3.12"), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(stage / "requirements.txt")], check=True, timeout=180, env=stage_env)
    for name in ASSETS:
        if sha256(stage / name) != manifest["assets"][name]:
            raise RuntimeError("staged_runtime_asset_mismatch")
    (stage / "bundle-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    (stage / "bundle-manifest.json").chmod(0o600)
    _verify_staged_runtime(stage, manifest)
    os.replace(stage, staged_release)
    return staged_release


def _install_staged_runtime(staged_release: Path, manifest: dict) -> Path:
    release = ROOT / "runtime" / manifest["source_sha"]
    release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if release.exists():
        _verify_staged_runtime(release, manifest)
        return release
    _verify_staged_runtime(staged_release, manifest)
    os.replace(staged_release, release)
    _verify_staged_runtime(release, manifest)
    return release


def _verify_staged_runtime(runtime: Path, manifest: dict) -> None:
    manifest_path = runtime / "bundle-manifest.json"
    python = runtime / ".venv/bin/python3.12"
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if recorded != manifest or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError
        subprocess.run([str(python), "-c", "import sys,httpx,certifi; assert sys.version_info >= (3,12)"],
                       check=True, capture_output=True, timeout=10,
                       env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError("staged_runtime_environment_invalid") from exc


def _migrate_tenant_binding(manifest: dict) -> None:
    destination = ROOT / "config/tenant-bindings.json"
    if destination.is_symlink():
        raise RuntimeError("worker_tenant_binding_invalid")
    if destination.exists():
        binding = _validated_current_tenant_binding()
        if binding is None:
            raise RuntimeError("worker_tenant_binding_invalid")
    else:
        binding = _validated_legacy_tenant_binding(manifest)
        if binding is None:
            return
    if (not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_sha", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("assets", {}).get("gpoint_mac_worker.py", ""))):
        raise RuntimeError("worker_tenant_binding_invalid")
    binding = dict(binding)
    binding["candidate_sha"] = manifest["source_sha"]
    binding["worker_sha256"] = manifest["assets"]["gpoint_mac_worker.py"]
    _atomic_json(destination, binding)


def _render_plist(runtime: Path, diagnostics_directory: Path | None = None) -> bytes:
    wrapper = str(runtime / "run_codex_worker.sh")
    log_root = diagnostics_directory or ROOT / "logs"
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [wrapper, "watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(runtime),
        "StandardOutPath": str(log_root / "candidate.stdout.log"),
        "StandardErrorPath": str(log_root / "candidate.stderr.log"),
        "EnvironmentVariables": _launchagent_environment(),
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _safe_remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _same_tree(left: Path, right: Path) -> bool:
    if not left.is_dir() or left.is_symlink() or not right.is_dir() or right.is_symlink():
        return False

    def inventory_tree(root: Path) -> dict[str, tuple[str, str | None]]:
        result = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in (*dirs, *files):
                item = Path(directory) / name
                rel = str(item.relative_to(root))
                if item.is_symlink():
                    result[rel] = ("symlink", os.readlink(item))
                elif item.is_dir():
                    result[rel] = ("dir", None)
                else:
                    # Compare all bytes in memory, but never store or print
                    # hashes of potentially secret-bearing files.
                    result[rel] = (sha256(item), None)
        return result

    return inventory_tree(left) == inventory_tree(right)


def _restore_legacy_snapshot(backup: Path) -> None:
    previous = backup.with_name(".legacy-runtime-previous")
    if backup.is_symlink() or not backup.is_dir():
        if previous.is_symlink() or not previous.is_dir():
            raise RuntimeError("rollback_snapshot_missing")
        backup = previous
    if LEGACY.exists() and _same_tree(LEGACY, backup):
        return
    _safe_remove_tree(LEGACY)
    shutil.copytree(backup, LEGACY, symlinks=True)
    LEGACY.chmod(0o700)
    if not _same_tree(LEGACY, backup):
        raise RuntimeError("rollback_legacy_snapshot_mismatch")


def _prepare_legacy_snapshot(backup: Path) -> Path:
    """Atomically refresh the rollback copy from the stopped live legacy runtime."""
    if not LEGACY.is_dir() or LEGACY.is_symlink():
        raise RuntimeError("legacy_runtime_missing")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.parent.chmod(0o700)
    previous = backup.with_name(".legacy-runtime-previous")
    stage = backup.with_name(f".legacy-runtime-staging-{os.getpid()}")
    _safe_remove_tree(stage)
    # Recover an interrupted two-rename swap before creating the next snapshot.
    if not backup.exists() and previous.exists():
        os.replace(previous, backup)
    shutil.copytree(LEGACY, stage, symlinks=True, copy_function=shutil.copy2)
    for directory, _dirs, files in os.walk(stage, followlinks=False):
        Path(directory).chmod(0o700)
        for filename in files:
            item = Path(directory) / filename
            if not item.is_symlink():
                item.chmod(0o600 if filename.endswith((".env", ".json")) else 0o700 if os.access(item, os.X_OK) else 0o600)
    _safe_remove_tree(previous)
    if backup.exists():
        os.replace(backup, previous)
    try:
        os.replace(stage, backup)
    except Exception:
        if not backup.exists() and previous.exists():
            os.replace(previous, backup)
        raise
    if not _same_tree(LEGACY, backup):
        _safe_remove_tree(backup)
        if previous.exists():
            os.replace(previous, backup)
        raise RuntimeError("rollback_snapshot_refresh_mismatch")
    _safe_remove_tree(previous)
    return backup


def _health_check(deadline: float = 60.0) -> dict:
    python = ROOT / "runtime/current/.venv/bin/python3.12"
    worker = ROOT / "runtime/current/gpoint_mac_worker.py"
    last = {}
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if not launchctl_loaded():
            time.sleep(2)
            continue
        result = subprocess.run([str(python), str(worker), "status"], capture_output=True,
                                text=True, timeout=15, check=False)
        try:
            last = json.loads(result.stdout)
        except ValueError:
            last = {}
        lanes = last.get("lanes", {}) if isinstance(last.get("lanes"), dict) else {}
        production_healthy = all(lanes.get(lane, {}).get("health") == "HEALTHY" for lane in ("GPOINT", "GSALES"))
        if result.returncode == 0 and last.get("pool") and lanes and production_healthy:
            try:
                last["production_queue_snapshot"] = validate_fresh_production_queue_snapshots(last)
            except RuntimeError:
                time.sleep(2)
                continue
            return last
        time.sleep(2)
    raise RuntimeError("new_worker_health_check_failed")


def _wait_for_successful_polls(baseline: dict, deadline: float = 90.0) -> dict:
    status_path = ROOT / "status/worker-status.json"
    previous = baseline.get("targets", {}) if isinstance(baseline.get("targets"), dict) else {}
    until = time.monotonic() + deadline
    latest = {}
    while time.monotonic() < until:
        latest = safe_worker_status(status_path)
        targets = latest.get("targets", {}) if isinstance(latest.get("targets"), dict) else {}
        checks = {}
        for lane in ("GPOINT", "GSALES"):
            before = previous.get(lane, {}) if isinstance(previous.get(lane), dict) else {}
            after = targets.get(lane, {}) if isinstance(targets.get(lane), dict) else {}
            old_poll = before.get("last_poll_at")
            new_poll = after.get("last_poll_at")
            try:
                progressed = bool(new_poll and (not old_poll or datetime.fromisoformat(
                    str(new_poll).replace("Z", "+00:00")) > datetime.fromisoformat(
                    str(old_poll).replace("Z", "+00:00"))))
            except (ValueError, TypeError):
                progressed = False
            checks[lane] = {"poll_advanced": progressed,
                            "stale_error_cleared": after.get("last_error_code") in (None, "")}
        if all(value["poll_advanced"] and value["stale_error_cleared"] for value in checks.values()):
            return {"passed": True, "lanes": checks}
        time.sleep(min(2.0, max(0.0, until - time.monotonic())))
    return {"passed": False, "lanes": checks if "checks" in locals() else {},
            "last_poll_at": latest.get("last_poll_at")}


def _network_state_sha256() -> str:
    observations = []
    for command in (("/usr/sbin/scutil", "--nwi"), ("/sbin/route", "-n", "get", "default")):
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=True)
        observations.append(result.stdout)
    return hashlib.sha256("\n".join(observations).encode()).hexdigest()


def _ensure_pool_config() -> None:
    pool_config = ROOT / "config/pool.env"
    if not pool_config.exists():
        pool_config.write_text("AGATA_WORKER_CONCURRENCY=1\nAGATA_CODEX_PARALLEL_CAPABILITY=NOT_VERIFIED\nAGATA_WORKER_POLL_SECONDS=3\n")
        pool_config.chmod(0o600)
        return
    if pool_config.is_symlink() or pool_config.stat().st_uid != os.getuid() or pool_config.stat().st_mode & 0o077:
        raise RuntimeError("worker_pool_config_permissions_invalid")
    values = {}
    for line in pool_config.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item and not item.startswith("#") and "=" in item:
            key, value = item.split("=", 1)
            if key not in {"AGATA_WORKER_CONCURRENCY", "AGATA_CODEX_PARALLEL_CAPABILITY", "AGATA_WORKER_POLL_SECONDS"}:
                raise RuntimeError("worker_pool_config_key_invalid")
            values[key] = value.strip().strip('"').strip("'")
    if values.get("AGATA_WORKER_CONCURRENCY", "1") != "1" or values.get("AGATA_CODEX_PARALLEL_CAPABILITY", "NOT_VERIFIED") != "NOT_VERIFIED":
        raise RuntimeError("worker_pool_must_start_at_n1_unverified")


def apply() -> dict:
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("worker_transition_already_running") from exc
    try:
        # Repeat all safety checks at the point of mutation.
        manifest = candidate()
        before = inventory()
        if before["production_release_lock_busy"]:
            raise RuntimeError("production_release_lock_busy")
        if before["active_inference"]:
            raise RuntimeError("active_inference_blocks_transition")
        if before.get("launchagent_loaded") is not True:
            raise RuntimeError("worker_launchagent_not_loaded")
        queue_evidence = require_fresh_empty_production_queues(_read_production_queue_status())
        network_before = _network_state_sha256()
        if not ROOT.is_dir() or ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid():
            raise RuntimeError("worker_runtime_root_owner_invalid")
        rollback_tree = _ensure_rollback_tree(manifest["source_sha"])
        staged_release = _stage_runtime(manifest)
        startup_check = _validate_candidate_startup(staged_release, manifest)
        attempt_id = uuid.uuid4().hex
        attempt_started_at = datetime.now(UTC).isoformat()
        diagnostics_directory = _create_diagnostic_attempt(manifest["source_sha"], attempt_id)
        backup = rollback_tree / "legacy-runtime"
        if PLIST.is_file():
            _save_original_launchagent(rollback_tree)
        original_plist = PLIST.read_bytes() if PLIST.is_file() else None
        target_link = ROOT / "runtime/current"
        prior_link_target = os.readlink(target_link) if target_link.is_symlink() else None
        state_dir = ROOT / "state"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state_dir.is_symlink() or state_dir.stat().st_uid != os.getuid():
            raise RuntimeError("worker_state_directory_permissions_invalid")
        maintenance_marker = ROOT / "state/maintenance-stop"
        next_plist = _render_plist(target_link, diagnostics_directory)
        switch_started = False
        restart_evidence = None
        try:
            # Freeze the verified LaunchAgent and repeat the idle check while
            # it cannot claim another job. This closes the preflight/bootout race.
            marker_fd = os.open(maintenance_marker, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(marker_fd)
            maintenance_marker.chmod(0o600)
            paused_pid = _pause_idle_launchagent()
            if paused_pid is None:
                raise RuntimeError("worker_launchagent_unloaded_during_transition")
            try:
                # The worker cannot claim another job while paused. Re-read
                # the authoritative queue immediately before bootout. Queue
                # producers remain independent; any later enqueue stays durable.
                queue_evidence = require_fresh_empty_production_queues(_read_production_queue_status())
            except Exception:
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                raise
            switch_started = True
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=20)
            finally:
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            _wait_launchagent_unloaded(paused_pid)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _running_inference_pids():
                time.sleep(1)
            if _running_inference_pids():
                raise RuntimeError("worker_process_did_not_stop")
            # Snapshot only while the old process is stopped, so queue/status
            # state cannot change between rollback capture and the switch.
            maintenance_marker.unlink(missing_ok=True)
            backup = _prepare_legacy_snapshot(backup)
            marker_fd = os.open(maintenance_marker, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(marker_fd)
            maintenance_marker.chmod(0o600)
            for name in ("runtime", "config", "state", "locks", "receipts", "status", "logs", "queues", "slots"):
                directory = ROOT / name
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if directory.is_symlink() or directory.stat().st_uid != os.getuid():
                    raise RuntimeError("worker_runtime_directory_owner_invalid")
                directory.chmod(0o700)
            _ensure_pool_config()
            release = _install_staged_runtime(staged_release, manifest)
            # Migrate only queue/status state. Secret credentials stay in their
            # existing external mode-600 config; the LAB approval is rechecked
            # against the new bundle and remains fail-closed if stale.
            migrations = (("lab-queue", ROOT / "queues/lab"),
                          ("worker-status.json", ROOT / "status/worker-status.json"),
                          ("lab-approval-usage", ROOT / "state/lab-approval-usage"))
            for source_name, destination in migrations:
                source = LEGACY / source_name
                if source.exists() and not destination.exists():
                    shutil.copytree(source, destination, dirs_exist_ok=True) if source.is_dir() else shutil.copy2(source, destination)
            _migrate_tenant_binding(manifest)
            poll_baseline = safe_worker_status(ROOT / "status/worker-status.json")
            switch_started = True
            next_link = ROOT / "runtime/.current-next"
            _safe_remove_tree(next_link)
            next_link.symlink_to(release.name)
            os.replace(next_link, target_link)
            temp = PLIST.with_suffix(".plist.new")
            temp.write_bytes(next_plist)
            temp.chmod(0o600)
            os.replace(temp, PLIST)
            maintenance_marker.unlink(missing_ok=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=20)
            # One bounded restart command owns both success and timeout
            # adjudication. Do not fail on CLI timing before checking the live PID.
            restart_evidence = _kickstart_and_verify(manifest["source_sha"])
            if restart_evidence["verification"] != "PASS":
                raise RuntimeError("restart_verification_failed")
            restarted = restart_evidence["adjudication"].get("status", {})
            status = restarted
            if restarted.get("configured_concurrency") != 1:
                raise RuntimeError("restart_verification_failed")
            if any(not isinstance(restarted.get("lanes", {}).get(lane), dict) for lane in ("GPOINT", "GSALES", "LAB")):
                raise RuntimeError("lane_status_missing")
            poll_evidence = _wait_for_successful_polls(poll_baseline)
            if not poll_evidence["passed"]:
                raise RuntimeError("successful_worker_polls_not_verified")
            network_after = _network_state_sha256()
            if network_before != network_after:
                raise RuntimeError("network_state_changed_during_transition")
            attempts = restart_evidence.get("adjudication", {}).get("attempts") or [{}]
            diagnostic_receipt = _write_diagnostic_evidence(
                diagnostics_directory, manifest=manifest, attempt_id=attempt_id,
                rendered_plist=next_plist, launchctl_print=attempts[-1].get("launchctl_print", ""),
                process_evidence=attempts[-1], command_evidence=restart_evidence.get("command", {}),
                started_at=attempt_started_at,
            )
            # Legacy runtime is removed only after health and restart pass.
            _remove_legacy_after_verification(health_passed=bool(status.get("lanes"))
                                              and all(status.get("lanes", {}).get(lane, {}).get("health") == "HEALTHY"
                                                      for lane in ("GPOINT", "GSALES")),
                                              restart_evidence=restart_evidence)
            evidence = {
                "state": "COMPLETE", "at": datetime.now(UTC).isoformat(), "label": LABEL,
                "source_sha": manifest["source_sha"], "bundle_sha256": manifest["bundle_sha256"],
                "configured_concurrency": 1, "provider_parallel_capability": "NOT_VERIFIED",
                "health_check": status, "restart_check": restarted,
                "fresh_poll_check": poll_evidence,
                "restart_evidence": restart_evidence,
                "legacy_runtime_removed": LEGACY != ROOT and not LEGACY.exists(),
                "canonical_runtime_path": str(ROOT),
                "network_state_sha256_before": network_before,
                "network_state_sha256_after": network_after,
                "network_configuration_writes_performed": False,
                "production_queue_snapshot": queue_evidence,
                "rollback_snapshot": str(backup),
                "startup_check": startup_check,
                "diagnostics": str(diagnostic_receipt),
                "original_plist_sha256": hashlib.sha256(original_plist or b"").hexdigest(),
            }
            _atomic_json(ROOT / "receipts/runtime-migration.json", evidence)
            return evidence
        except Exception as failure:
            if not switch_started:
                maintenance_marker.unlink(missing_ok=True)
                raise
            diagnostic_receipt = None
            try:
                attempts = (restart_evidence or {}).get("adjudication", {}).get("attempts") or [{}]
                launchctl_result, _ = _run_readonly_command(
                    ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], 8,
                )
                diagnostic_receipt = _write_diagnostic_evidence(
                    diagnostics_directory, manifest=manifest, attempt_id=attempt_id,
                    rendered_plist=next_plist,
                    launchctl_print=attempts[-1].get("launchctl_print") or launchctl_result.stdout,
                    process_evidence=attempts[-1],
                    command_evidence=(restart_evidence or {}).get("command", {}), error=failure,
                    started_at=attempt_started_at,
                )
            except Exception:
                # Diagnostic capture must never prevent a safety rollback.
                pass
            # Restore old service and its files from the private snapshot.
            legacy_restored = False
            launchagent_restored = False
            rollback_error_code = None
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=15, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _wait_launchagent_unloaded(None)
                if backup.exists():
                    _restore_legacy_snapshot(backup)
                elif not LEGACY.exists():
                    raise RuntimeError("rollback_snapshot_missing")
                legacy_restored = LEGACY.exists()
                target_link = ROOT / "runtime/current"
                if prior_link_target is None:
                    _safe_remove_tree(target_link)
                else:
                    temporary_link = ROOT / "runtime/.current-rollback"
                    _safe_remove_tree(temporary_link)
                    temporary_link.symlink_to(prior_link_target)
                    os.replace(temporary_link, target_link)
                maintenance_marker.unlink(missing_ok=True)
                if original_plist is not None:
                    PLIST.write_bytes(original_plist)
                    PLIST.chmod(0o600)
                    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], timeout=20, check=True)
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], timeout=15, check=True)
                    if PLIST.read_bytes() != original_plist:
                        raise RuntimeError("rollback_launchagent_plist_mismatch")
                    if not launchctl_loaded():
                        raise RuntimeError("rollback_launchagent_not_loaded")
                    launchagent_restored = PLIST.read_bytes() == original_plist and launchctl_loaded()
                    if not launchagent_restored:
                        raise RuntimeError("rollback_launchagent_not_loaded")
            except Exception as rollback_failure:
                rollback_error_code = _safe_error_code(rollback_failure)
                launchagent_restored = bool(
                    original_plist is not None and PLIST.is_file()
                    and PLIST.read_bytes() == original_plist and launchctl_loaded()
                )
            _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", {
                "state": "ROLLED_BACK" if legacy_restored and launchagent_restored else "ROLLBACK_INCOMPLETE",
                "at": datetime.now(UTC).isoformat(),
                "source_sha": manifest["source_sha"], "legacy_restored": legacy_restored,
                "launchagent_restored": launchagent_restored,
                "rollback_error_code": rollback_error_code,
                "rollback_snapshot": str(backup),
                "diagnostics": str(diagnostic_receipt) if diagnostic_receipt else str(diagnostics_directory),
                "startup_check": startup_check,
                "restart_evidence": restart_evidence,
            })
            raise
    finally:
        os.close(fd)


def rollback() -> dict:
    snapshots = _rollback_snapshot_base()
    candidates = []
    if snapshots.is_dir() and not snapshots.is_symlink():
        candidates = [entry for entry in snapshots.iterdir()
                      if entry.is_dir() and not entry.is_symlink()
                      and (entry / "legacy-runtime").is_dir()
                      and (entry / "original-launchagent.plist").is_file()]
    if not candidates:
        raise RuntimeError("rollback_snapshot_missing")
    snapshot_root = max(candidates, key=lambda entry: entry.stat().st_mtime)
    backup = snapshot_root / "legacy-runtime"
    original_plist_file = snapshot_root / "original-launchagent.plist"
    previous_backup = backup.with_name(".legacy-runtime-previous")
    if ((backup.is_symlink() or not backup.is_dir()) and
            (previous_backup.is_symlink() or not previous_backup.is_dir())) or not original_plist_file.is_file():
        raise RuntimeError("rollback_snapshot_missing")
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("worker_transition_already_running") from exc
    try:
        before = inventory()
        if before["production_release_lock_busy"]:
            raise RuntimeError("production_release_lock_busy")
        if before["active_inference"]:
            raise RuntimeError("active_inference_blocks_rollback")
        paused_pid = _pause_idle_launchagent()
        if paused_pid is not None:
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=20)
            finally:
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        _wait_launchagent_unloaded(paused_pid)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _running_inference_pids():
            time.sleep(1)
        if _running_inference_pids():
            raise RuntimeError("worker_process_did_not_stop")
        _restore_legacy_snapshot(backup)
        original_plist = original_plist_file.read_bytes()
        PLIST.write_bytes(original_plist)
        PLIST.chmod(0o600)
        (ROOT / "state/maintenance-stop").unlink(missing_ok=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=20)
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=15)
        if not launchctl_loaded():
            raise RuntimeError("rollback_launchagent_not_loaded")
        if PLIST.read_bytes() != original_plist:
            raise RuntimeError("rollback_launchagent_plist_mismatch")
        evidence = {
            "state": "ROLLED_BACK", "at": datetime.now(UTC).isoformat(), "label": LABEL,
            "legacy_runtime_restored": LEGACY.exists(), "launchagent_loaded": True,
            "restored_plist_sha256": hashlib.sha256(original_plist).hexdigest(),
            "rollback_snapshot": str(backup),
        }
        _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", evidence)
        return evidence
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "prepare", "apply", "rollback"))
    args = parser.parse_args()
    result = (read_only_plan() if args.action == "plan" else prepare() if args.action == "prepare"
              else rollback() if args.action == "rollback" else apply())
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
