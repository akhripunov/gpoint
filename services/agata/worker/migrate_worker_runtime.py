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
QUEUE_SNAPSHOT_MAX_RETRIES = 2
QUEUE_SNAPSHOT_REQUEST_TIMEOUT_SECONDS = 25
QUEUE_SNAPSHOT_PREFLIGHT_DEADLINE_SECONDS = 90
QUEUE_SNAPSHOT_RETRY_DELAYS_SECONDS = (0.25, 0.5)
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
SAFE_DIAGNOSTIC_ERROR_CODES = frozenset({
    "worker_claim_invalid", "worker_claim_target_mismatch", "worker_config_permissions_invalid",
    "worker_gateway_auth_rejected", "worker_gateway_http_404", "worker_gateway_network_error",
    "worker_gateway_or_auth_unconfigured", "worker_gateway_response_invalid", "worker_gateway_transport_error",
    "worker_gateway_url_invalid", "worker_pool_config_key_invalid", "worker_target_invalid",
    "worker_target_unconfigured", "worker_tenant_binding_invalid", "worker_tenants_must_be_distinct",
    "worker_targets_must_be_distinct",
})


class PreflightFailure(RuntimeError):
    """A fail-closed preflight result with safe, non-payload diagnostics."""

    SAFE_FIELDS = {"runner_exit_code", "worker_status_exit_code", "timeout_seconds", "snapshot_age_seconds"}

    def __init__(self, reason_code: str, *, stage: str, lane: str | None = None,
                 safe_fields: dict | None = None, legacy_error: str | None = None,
                 error_category: str | None = None, exit_class: str | None = None,
                 attempts: list[dict] | None = None):
        super().__init__(legacy_error or reason_code)
        self.reason_code = reason_code
        self.stage = stage
        self.lane = lane if lane in {"GPOINT", "GSALES", "LAB"} else None
        self.safe_fields = {
            key: value for key, value in (safe_fields or {}).items()
            if key in self.SAFE_FIELDS and isinstance(value, int) and not isinstance(value, bool)
        }
        self.safe_fields.update({
            key: value for key, value in (safe_fields or {}).items()
            if key in {"health_error_code", "queue_snapshot_error_code"}
            and isinstance(value, str) and value in SAFE_DIAGNOSTIC_ERROR_CODES
        })
        self.receipt_written = False
        inferred_category = "unknown"
        inferred_exit_class = "unknown"
        if any(token in reason_code for token in ("identity", "provenance", "tenant_binding", "release")):
            inferred_category, inferred_exit_class = "provenance", "identity_provenance"
        elif "queue" in reason_code or "snapshot" in reason_code:
            inferred_category, inferred_exit_class = "queue", "queue_state"
        elif "health" in reason_code or "lane" in reason_code:
            inferred_category, inferred_exit_class = "health", "local_preflight"
        elif any(token in reason_code for token in ("lock", "pool", "slot", "worker", "launchagent", "candidate")):
            inferred_category, inferred_exit_class = "local", "local_preflight"
        self.error_category = error_category if error_category in {
            "transport", "runner", "identity", "provenance", "queue", "health", "local", "unknown"
        } else inferred_category
        self.exit_class = exit_class if exit_class in {
            "transient_transport", "permanent_transport", "identity_provenance", "queue_state",
            "local_preflight", "unknown",
        } else inferred_exit_class
        self.attempts = list(attempts or [])


def _safe_queue_attempts(attempts: list[dict]) -> list[dict]:
    """Keep only fixed, non-sensitive fields from snapshot attempt receipts."""
    result = []
    for item in attempts:
        if not isinstance(item, dict) or item.get("lane") not in {"GPOINT", "GSALES"}:
            continue
        entry = {
            "lane": item["lane"],
            "attempt": item.get("attempt") if isinstance(item.get("attempt"), int) else None,
            "outcome": item.get("outcome") if item.get("outcome") in {
                "RESPONSE", "RETRYABLE_FAILURE", "BLOCKED_FAILURE", "DEADLINE_EXCEEDED"
            } else "BLOCKED_FAILURE",
            "reason_code": item.get("reason_code") if isinstance(item.get("reason_code"), str)
            and re.fullmatch(r"[a-z0-9_]{1,80}", item["reason_code"]) else "unknown",
            "error_category": item.get("error_category") if item.get("error_category") in {
                "transport", "runner", "identity", "provenance", "queue", "health", "local", "unknown"
            } else "unknown",
            "exit_class": item.get("exit_class") if item.get("exit_class") in {
                "transient_transport", "permanent_transport", "identity_provenance", "queue_state",
                "local_preflight", "unknown",
            } else "unknown",
            "started_at": item.get("started_at") if isinstance(item.get("started_at"), str) else None,
            "finished_at": item.get("finished_at") if isinstance(item.get("finished_at"), str) else None,
        }
        result.append(entry)
    return result


class QueueSnapshotRetryBudget:
    """One bounded retry allowance and deadline shared across all production lanes."""

    def __init__(self, *, deadline_seconds: float = QUEUE_SNAPSHOT_PREFLIGHT_DEADLINE_SECONDS):
        self.deadline_monotonic = time.monotonic() + deadline_seconds
        self.retries_remaining = QUEUE_SNAPSHOT_MAX_RETRIES
        self.attempts: list[dict] = []

    def request_timeout_seconds(self) -> int:
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining < 1:
            raise PreflightFailure(
                "authoritative_snapshot_deadline_exceeded", stage="queue_snapshot",
                error_category="transport", exit_class="transient_transport",
            )
        return max(1, min(QUEUE_SNAPSHOT_REQUEST_TIMEOUT_SECONDS, int(remaining)))

    def record(self, lane: str, outcome: str, failure: PreflightFailure | None = None,
               *, started_at: str | None = None) -> None:
        self.attempts.append({
            "lane": lane,
            "attempt": 1 + sum(entry["lane"] == lane for entry in self.attempts),
            "outcome": outcome,
            "reason_code": failure.reason_code if failure else "snapshot_received",
            "error_category": failure.error_category if failure else "queue",
            "exit_class": failure.exit_class if failure else "queue_state",
            "started_at": started_at or getattr(failure, "attempt_started_at", None) or datetime.now(UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        })


def _write_preflight_receipt(stage: str, failure: PreflightFailure | None = None,
                             *, outcome: str | None = None,
                             summary: dict | None = None,
                             attempts: list[dict] | None = None,
                             started_at: str | None = None) -> Path:
    """Persist only allowlisted preflight metadata, never raw status or command output."""
    receipts = ROOT / "receipts"
    if ROOT.is_symlink() or (ROOT.exists() and ROOT.stat().st_uid != os.getuid()):
        raise RuntimeError("preflight_receipt_storage_unavailable")
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (receipts.is_symlink() or receipts.stat().st_uid != os.getuid()
            or receipts.stat().st_mode & 0o077):
        raise RuntimeError("preflight_receipt_storage_unavailable")
    receipt = {
        "schema_version": 2,
        "started_at": started_at or datetime.now(UTC).isoformat(),
        "stage": stage,
        "outcome": outcome or ("BLOCKED" if failure else "PASS"),
    }
    if failure:
        receipt["reason_code"] = failure.reason_code
        receipt["failure_stage"] = failure.stage
        receipt["error_category"] = failure.error_category
        receipt["exit_class"] = failure.exit_class
        if failure.lane:
            receipt["lane"] = failure.lane
        receipt.update(failure.safe_fields)
        attempts = attempts if attempts is not None else failure.attempts
    if summary:
        allowed_summary = {
            "candidate_verified", "launchagent_loaded", "active_inference_absent",
            "locks_free", "queue_snapshots_verified", "queues_empty", "lane_health_healthy",
        }
        receipt.update({key: value for key, value in summary.items()
                        if key in allowed_summary and isinstance(value, bool)})
    receipt["attempts"] = _safe_queue_attempts(attempts or [])
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    receipt["at"] = receipt["finished_at"]  # compatibility with existing receipt readers
    path = receipts / f"preflight-{uuid.uuid4().hex}.json"
    _atomic_json(path, receipt, mode=0o600)
    return path


def _record_preflight(stage: str, failure: PreflightFailure | None = None,
                      *, summary: dict | None = None,
                      attempts: list[dict] | None = None,
                      started_at: str | None = None) -> bool:
    try:
        _write_preflight_receipt(stage, failure, summary=summary, attempts=attempts,
                                 started_at=started_at)
        if failure:
            failure.receipt_written = True
        return True
    except (OSError, RuntimeError):
        if failure:
            failure.receipt_written = False
        return False


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
    if git("status", "--porcelain", "--untracked-files=all"):
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
        raise PreflightFailure("queue_snapshot_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    pool = status.get("pool")
    busy = pool.get("busy") if isinstance(pool, dict) else None
    pool_authority = status.get("pool_snapshot_authority", "worker_pool_status_v1")
    if pool_authority == "worker_pool_status_v1":
        pool_state_valid = isinstance(pool, dict) and pool.get("state") == "HEALTHY"
    elif pool_authority == "mac_worker_live_idle_inventory_v1":
        inventory = status.get("pool_snapshot_inventory")
        slots = status.get("slots")
        slot_count = len(slots) if isinstance(slots, dict) else None
        pool_state_valid = (
            isinstance(pool, dict) and pool.get("state") == "IDLE_CONFIRMED"
            and isinstance(inventory, dict)
            and inventory == {
                "configured_concurrency": 1,
                "slot_count": slot_count,
                "all_slots_idle": True,
                "current_job_present": False,
                "launch_agent_loaded": True,
                "active_inference": False,
            }
            and isinstance(total := pool.get("total"), int) and not isinstance(total, bool)
            and total == slot_count and slot_count in {1, 3}
            and isinstance(status.get("configured_concurrency"), int)
            and not isinstance(status.get("configured_concurrency"), bool)
            and status.get("configured_concurrency") == 1
            and status.get("current_job_present") is False
            and status.get("launch_agent_loaded") is True
            and isinstance(slots, dict)
            and set(slots) in ({"slot-01"}, {"slot-01", "slot-02", "slot-03"})
            and all(isinstance(slot, dict) and slot.get("state") == "IDLE"
                    and slot.get("job_id") is None for slot in slots.values())
        )
    else:
        raise PreflightFailure("pool_evidence_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_snapshot_invalid")
    total = pool.get("total") if isinstance(pool, dict) else None
    if (not pool_state_valid or status.get("launch_agent_loaded") is not True
            or isinstance(busy, bool) or not isinstance(busy, int) or busy < 0):
        raise PreflightFailure("local_pool_not_idle", stage="local_pool_validation",
                               legacy_error="production_queue_snapshot_invalid")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < busy):
        raise PreflightFailure("local_pool_status_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_snapshot_invalid")
    try:
        pool_captured = datetime.fromisoformat(status.get("pool_snapshot_at", "").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise PreflightFailure("pool_snapshot_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid") from exc
    if pool_captured.tzinfo is None:
        raise PreflightFailure("pool_snapshot_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    pool_age = (current - pool_captured).total_seconds()
    if pool_age > QUEUE_SNAPSHOT_MAX_AGE_SECONDS or pool_age < -QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise PreflightFailure("queue_snapshot_stale", stage="queue_validation",
                               safe_fields={"snapshot_age_seconds": int(pool_age)},
                               legacy_error="production_queue_snapshot_stale")
    lanes = status.get("lanes")
    if not isinstance(lanes, dict):
        raise PreflightFailure("worker_status_schema_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    evidence = {}
    for lane in ("GPOINT", "GSALES"):
        value = lanes.get(lane)
        if not isinstance(value, dict):
            raise PreflightFailure("worker_status_schema_invalid", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_snapshot_invalid")
        if value.get("health") != "HEALTHY":
            raise PreflightFailure("lane_health_invalid", stage="lane_health_validation", lane=lane,
                                   legacy_error="production_queue_snapshot_invalid")
        if value.get("queue_snapshot_state") != "VERIFIED":
            raise PreflightFailure("authoritative_queue_snapshot_unavailable", stage="queue_validation",
                                   lane=lane, legacy_error="production_queue_snapshot_invalid")
        api_source = f"agata-worker-queue-status-v1:{lane.lower()}"
        live_source = f"{LIVE_QUEUE_SOURCE}:{lane.lower()}"
        source = value.get("queue_snapshot_source_id")
        if source == live_source:
            try:
                _validate_live_queue_snapshot_provenance(value, lane)
            except RuntimeError as exc:
                raise PreflightFailure("queue_snapshot_provenance_invalid", stage="queue_validation",
                                       lane=lane, legacy_error="production_queue_snapshot_invalid") from exc
        elif source != api_source or value.get("queue_snapshot_provenance") is not None:
            raise PreflightFailure("queue_snapshot_provenance_invalid", stage="queue_validation",
                                   lane=lane, legacy_error="production_queue_snapshot_invalid")
        counts = (value.get("pending_inference_jobs"), value.get("running_inference_jobs"))
        if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
            raise PreflightFailure("queue_snapshot_counts_invalid", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_snapshot_invalid")
        try:
            captured = datetime.fromisoformat(value.get("queue_snapshot_at", "").replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise PreflightFailure("queue_snapshot_invalid", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_snapshot_invalid") from exc
        if captured.tzinfo is None:
            raise PreflightFailure("queue_snapshot_invalid", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_snapshot_invalid")
        age = (current - captured).total_seconds()
        if age > QUEUE_SNAPSHOT_MAX_AGE_SECONDS or age < -QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS:
            raise PreflightFailure("queue_snapshot_stale", stage="queue_validation", lane=lane,
                                   safe_fields={"snapshot_age_seconds": int(age)},
                                   legacy_error="production_queue_snapshot_stale")
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
        raise PreflightFailure("local_pool_not_idle", stage="queue_validation",
                               legacy_error="worker_slots_busy")
    for lane, item in evidence["lanes"].items():
        if item["pending_jobs"] or item["running_jobs"]:
            raise PreflightFailure("production_queue_not_empty", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_not_empty")
    return evidence


def _validate_live_queue_snapshot_provenance(value: dict, lane: str) -> dict:
    provenance = value.get("queue_snapshot_provenance")
    if not isinstance(provenance, dict):
        raise PreflightFailure("queue_snapshot_provenance_invalid", stage="queue_provenance", lane=lane,
                               legacy_error="production_queue_snapshot_invalid")
    match = QUEUE_SOURCE_RE.fullmatch(provenance.get("source_id", ""))
    tenant_id = provenance.get("tenant_id")
    visible_tenant_rows = provenance.get("visible_tenant_rows")
    try:
        tenant = UUID(tenant_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PreflightFailure("queue_snapshot_provenance_invalid", stage="queue_provenance", lane=lane,
                               legacy_error="production_queue_snapshot_invalid") from exc
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
        raise PreflightFailure("queue_snapshot_provenance_invalid", stage="queue_provenance", lane=lane,
                               legacy_error="production_queue_snapshot_invalid")
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


def _read_live_runtime_queue_snapshot_once(lane: str, expected_tenant_id: str, *, timeout_seconds: int) -> dict:
    if not VPS_RUNNER.is_file() or not os.access(VPS_RUNNER, os.X_OK):
        raise PreflightFailure("authoritative_vps_runner_unavailable", stage="authoritative_vps_snapshot",
                               lane=lane, legacy_error="authoritative_queue_snapshot_runner_unavailable",
                               error_category="runner", exit_class="local_preflight")
    env = dict(os.environ)
    env["REMOTE_CMD"] = _remote_queue_snapshot_command(lane)
    env["GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS"] = str(max(1, min(25, timeout_seconds)))
    try:
        attempt_directory = tempfile.TemporaryDirectory(prefix="agata-queue-preflight-")
    except OSError as exc:
        raise PreflightFailure("authoritative_vps_diagnostic_channel_unavailable",
                               stage="authoritative_vps_snapshot", lane=lane,
                               error_category="runner", exit_class="local_preflight") from exc
    with attempt_directory as attempt_dir:
        attempt_status = Path(attempt_dir) / "transport-status"
        env["GPOINT_REMOTE_ATTEMPT_STATUS_FILE"] = str(attempt_status)
        try:
            result = subprocess.run([str(VPS_RUNNER)], capture_output=True, text=True,
                                    timeout=timeout_seconds, check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            marker = _runner_attempt_status(attempt_status)
            transient_reason = _proven_pre_exec_transient_reason(marker, exc.stderr)
            if transient_reason:
                raise PreflightFailure(transient_reason, stage="authoritative_vps_snapshot",
                                       lane=lane, safe_fields={"timeout_seconds": timeout_seconds},
                                       legacy_error="authoritative_queue_snapshot_unavailable",
                                       error_category="transport", exit_class="transient_transport") from exc
            raise PreflightFailure("authoritative_vps_runner_deadline_exceeded",
                                   stage="authoritative_vps_snapshot", lane=lane,
                                   safe_fields={"timeout_seconds": timeout_seconds},
                                   legacy_error="authoritative_queue_snapshot_unavailable",
                                   error_category="runner", exit_class="unknown") from exc
        except OSError as exc:
            raise PreflightFailure("authoritative_vps_runner_launch_failed", stage="authoritative_vps_snapshot",
                                   lane=lane, legacy_error="authoritative_queue_snapshot_unavailable",
                                   error_category="runner", exit_class="local_preflight") from exc
        except subprocess.SubprocessError as exc:
            raise PreflightFailure("authoritative_vps_runner_execution_failed", stage="authoritative_vps_snapshot",
                                   lane=lane, legacy_error="authoritative_queue_snapshot_unavailable",
                                   error_category="runner", exit_class="local_preflight") from exc
        marker = _runner_attempt_status(attempt_status)
    transient_reason = _proven_pre_exec_transient_reason(marker, result.stderr)
    if transient_reason:
        raise PreflightFailure(transient_reason, stage="authoritative_vps_snapshot",
                               lane=lane, safe_fields={"runner_exit_code": result.returncode},
                               legacy_error="authoritative_queue_snapshot_failed",
                               error_category="transport", exit_class="transient_transport")
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    snapshot = None
    if lines:
        try:
            snapshot = json.loads(lines[-1])
        except (TypeError, ValueError):
            snapshot = None
    if isinstance(snapshot, dict) and snapshot.get("preflight_error"):
        code = snapshot.get("preflight_error")
        remote_reasons = {
            "runtime_lane_identity_mismatch": "remote_tenant_or_lane_identity_mismatch",
            "runtime_release_identity_invalid": "remote_release_or_image_identity_mismatch",
            "queue_snapshot_trust_boundary_invalid": "remote_read_only_db_snapshot_contract_failed",
            "readonly_snapshot_blocked": "remote_read_only_snapshot_failed",
        }
        reason = remote_reasons.get(code, "remote_read_only_snapshot_failed")
        raise PreflightFailure(reason, stage="remote_snapshot", lane=lane,
                               safe_fields={"runner_exit_code": result.returncode},
                               error_category="identity" if "identity" in reason else "provenance",
                               exit_class="identity_provenance")
    if result.returncode != 0:
        error_text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
        if result.returncode == 124:
            reason = "authoritative_vps_runner_deadline_exceeded"
            category, exit_class = "runner", "unknown"
        elif result.returncode == 78:
            reason = "vps_runner_local_configuration_invalid"
            category, exit_class = "runner", "local_preflight"
        elif "permission denied" in error_text or "authentication failed" in error_text:
            reason = "ssh_authentication_failed"
            category, exit_class = "transport", "permanent_transport"
        elif "host key verification failed" in error_text or "remote host identification has changed" in error_text:
            reason = "ssh_host_identity_failed"
            category, exit_class = "identity", "identity_provenance"
        elif any(marker in error_text for marker in ("connect timed out", "connection timed out", "operation timed out")):
            reason = "ssh_transport_timeout_unconfirmed"
            category, exit_class = "transport", "unknown"
        elif any(marker in error_text for marker in (
                "connection reset", "connection closed", "connection lost", "broken pipe")):
            reason = "ssh_transport_interrupted_unconfirmed"
            category, exit_class = "transport", "unknown"
        elif any(marker in error_text for marker in (
                "could not resolve hostname", "network is unreachable", "no route to host", "connection refused")):
            reason = "ssh_transport_unavailable"
            category, exit_class = "transport", "permanent_transport"
        elif any(marker in error_text for marker in (
                "banner exchange", "kex_exchange_identification", "ssh_exchange_identification")):
            reason = "ssh_handshake_failed"
            category, exit_class = "transport", "permanent_transport"
        else:
            reason = "authoritative_vps_runner_nonzero_exit"
            category, exit_class = "runner", "unknown"
        raise PreflightFailure(reason, stage="authoritative_vps_snapshot", lane=lane,
                               safe_fields={"runner_exit_code": result.returncode},
                               legacy_error="authoritative_queue_snapshot_failed",
                               error_category=category, exit_class=exit_class)
    if not lines or not isinstance(snapshot, dict):
        raise PreflightFailure("authoritative_vps_snapshot_invalid_json", stage="remote_snapshot", lane=lane,
                               legacy_error="authoritative_queue_snapshot_invalid",
                               error_category="provenance", exit_class="identity_provenance")
    if not isinstance(snapshot, dict):
        raise PreflightFailure("authoritative_vps_snapshot_invalid_schema", stage="remote_snapshot", lane=lane,
                               legacy_error="authoritative_queue_snapshot_invalid")
    try:
        return normalize_live_runtime_queue_snapshot(snapshot, lane, expected_tenant_id)
    except PreflightFailure:
        raise
    except RuntimeError as exc:
        reason = "queue_snapshot_identity_mismatch" if str(exc) == "production_queue_snapshot_invalid" else "queue_snapshot_invalid"
        raise PreflightFailure(reason, stage="queue_provenance", lane=lane,
                               legacy_error="authoritative_queue_snapshot_invalid",
                               error_category="provenance", exit_class="identity_provenance") from exc


def _read_live_runtime_queue_snapshot(lane: str, expected_tenant_id: str, *,
                                      retry_budget: QueueSnapshotRetryBudget | None = None) -> dict:
    budget = retry_budget or QueueSnapshotRetryBudget()
    while True:
        attempt_started_at = datetime.now(UTC).isoformat()
        try:
            timeout_seconds = budget.request_timeout_seconds()
        except PreflightFailure as exc:
            exc.attempts = list(budget.attempts)
            raise
        try:
            snapshot = _read_live_runtime_queue_snapshot_once(
                lane, expected_tenant_id, timeout_seconds=timeout_seconds,
            )
        except PreflightFailure as exc:
            exc.attempt_started_at = attempt_started_at
            retryable = exc.exit_class == "transient_transport"
            if not retryable:
                budget.record(lane, "BLOCKED_FAILURE", exc, started_at=attempt_started_at)
                exc.attempts = list(budget.attempts)
                raise
            if budget.retries_remaining <= 0:
                budget.record(lane, "BLOCKED_FAILURE", exc, started_at=attempt_started_at)
                exc.attempts = list(budget.attempts)
                raise
            budget.retries_remaining -= 1
            budget.record(lane, "RETRYABLE_FAILURE", exc, started_at=attempt_started_at)
            delay_index = QUEUE_SNAPSHOT_MAX_RETRIES - budget.retries_remaining - 1
            delay = QUEUE_SNAPSHOT_RETRY_DELAYS_SECONDS[min(delay_index, len(QUEUE_SNAPSHOT_RETRY_DELAYS_SECONDS) - 1)]
            remaining = budget.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                deadline_failure = PreflightFailure(
                    "authoritative_snapshot_deadline_exceeded", stage="queue_snapshot", lane=lane,
                    error_category="transport", exit_class="transient_transport",
                    attempts=list(budget.attempts),
                )
                raise deadline_failure from exc
            time.sleep(min(delay, remaining))
            continue
        budget.record(lane, "RESPONSE", started_at=attempt_started_at)
        return snapshot


def _live_snapshot_fallback_allowed(lane_status: dict) -> bool:
    """Allow independent VPS queue evidence only for verified legacy transport cases."""
    if not isinstance(lane_status, dict):
        return False
    health = lane_status.get("health")
    health_error = lane_status.get("health_error_code")
    queue_error = lane_status.get("queue_snapshot_error_code")
    if lane_status.get("queue_snapshot_state") == "VERIFIED":
        return health == "HEALTHY" and health_error is None and queue_error is None
    # This code is emitted by the installed Worker only for HTTP/DNS/connect/read
    # transport exceptions. Authentication and response/data failures have distinct
    # codes and must never be converted into permission to use the fallback.
    if not isinstance(queue_error, str) or queue_error not in {
            "worker_gateway_http_404", "worker_gateway_network_error"}:
        return False
    if health == "HEALTHY":
        return health_error is None
    if health != "DEGRADED":
        return False
    if queue_error == "worker_gateway_http_404":
        return health_error == "worker_gateway_network_error"
    return health_error == "worker_gateway_network_error"


def _safe_lane_error_fields(lane_status: dict) -> dict[str, str]:
    """Expose only fixed Worker error codes in local preflight receipts."""
    if not isinstance(lane_status, dict):
        return {}
    safe = {}
    for key in ("health_error_code", "queue_snapshot_error_code"):
        value = lane_status.get(key)
        if isinstance(value, str) and value in SAFE_DIAGNOSTIC_ERROR_CODES:
            safe[key] = value
    return safe


def _refresh_local_pool_evidence(status: dict) -> None:
    """Record local idle inventory separately from health and production queue state."""
    pool = status.get("pool")
    slots = status.get("slots")
    lanes = status.get("lanes")
    concurrency = status.get("configured_concurrency")
    total = pool.get("total") if isinstance(pool, dict) else None
    slot_ids = set(slots) if isinstance(slots, dict) else set()
    canonical_slot_sets = ({"slot-01"}, {"slot-01", "slot-02", "slot-03"})
    if not isinstance(pool, dict) or isinstance(pool.get("busy"), bool) or not isinstance(pool.get("busy"), int):
        raise PreflightFailure("local_pool_status_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    if pool.get("busy") != 0 or status.get("current_job_present") is not False:
        raise PreflightFailure("local_pool_not_idle", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    if isinstance(concurrency, bool) or concurrency != 1 or isinstance(total, bool) or total not in {1, 3}:
        raise PreflightFailure("local_pool_configuration_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    if not isinstance(slots, dict) or slot_ids not in canonical_slot_sets or total != len(slots):
        raise PreflightFailure("local_slot_inventory_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    for slot_id, slot in slots.items():
        if not isinstance(slot, dict):
            raise PreflightFailure("local_slot_inventory_invalid", stage="local_pool_validation",
                                   lane=None, legacy_error="production_queue_status_unavailable")
        if slot.get("state") != "IDLE" or slot.get("job_id") is not None:
            slot_lane = slot.get("lane")
            lane = slot_lane if slot_lane in {"GPOINT", "GSALES", "LAB"} else None
            raise PreflightFailure("local_slot_not_idle", stage="local_pool_validation", lane=lane,
                                   legacy_error="production_queue_status_unavailable")
    if not isinstance(lanes, dict):
        raise PreflightFailure("worker_status_schema_invalid", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    if status.get("launch_agent_loaded") is not True:
        raise PreflightFailure("launchagent_not_loaded", stage="local_pool_validation",
                               legacy_error="production_queue_status_unavailable")
    for lane in ("GPOINT", "GSALES"):
        value = lanes.get(lane)
        if not isinstance(value, dict):
            raise PreflightFailure("worker_status_schema_invalid", stage="local_pool_validation", lane=lane,
                                   legacy_error="production_queue_status_unavailable")
        if not value.get("configured", True):
            continue
        if value.get("health") not in {"HEALTHY", "DEGRADED"}:
            raise PreflightFailure("lane_health_invalid", stage="lane_health_validation", lane=lane,
                                   legacy_error="production_queue_status_unavailable")
        if not _live_snapshot_fallback_allowed(value):
            queue_error = value.get("queue_snapshot_error_code")
            if value.get("health") != "HEALTHY" and queue_error is not None:
                raise PreflightFailure("lane_health_queue_error_combination_disallowed",
                                       stage="lane_health_validation", lane=lane,
                                       safe_fields=_safe_lane_error_fields(value),
                                       legacy_error="production_queue_status_unavailable")
            if value.get("health") != "HEALTHY":
                raise PreflightFailure("lane_health_invalid", stage="lane_health_validation", lane=lane,
                                       safe_fields=_safe_lane_error_fields(value),
                                       legacy_error="production_queue_status_unavailable")
            raise PreflightFailure("queue_snapshot_error_not_allowlisted",
                                   stage="queue_snapshot", lane=lane,
                                   safe_fields=_safe_lane_error_fields(value),
                                   legacy_error="production_queue_status_unavailable",
                                   error_category="queue", exit_class="unknown")
    try:
        local = inventory()
    except Exception as exc:
        raise PreflightFailure("worker_local_inventory_unavailable", stage="local_inventory",
                               legacy_error="production_queue_status_unavailable") from exc
    if local.get("launchagent_loaded") is not True:
        raise PreflightFailure("launchagent_not_loaded", stage="local_inventory",
                               legacy_error="production_queue_status_unavailable")
    if local.get("production_release_lock_busy"):
        raise PreflightFailure("production_release_lock_busy", stage="lock_validation",
                               legacy_error="production_queue_status_unavailable")
    if local.get("active_inference") is not False:
        raise PreflightFailure("active_inference_present", stage="local_inventory",
                               legacy_error="production_queue_status_unavailable")
    try:
        _launchagent_pid()
    except Exception as exc:
        raise PreflightFailure("launchagent_process_identity_invalid", stage="local_inventory",
                               legacy_error="production_queue_status_unavailable") from exc
    pool["state"] = "IDLE_CONFIRMED"
    status["pool_snapshot_at"] = datetime.now(UTC).isoformat()
    status["pool_snapshot_authority"] = "mac_worker_live_idle_inventory_v1"
    status["pool_snapshot_inventory"] = {
        "configured_concurrency": 1,
        "slot_count": len(slots),
        "all_slots_idle": True,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "active_inference": False,
    }


def _read_production_queue_status(manifest: dict | None = None, *,
                                  retry_budget: QueueSnapshotRetryBudget | None = None) -> dict:
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
        raise PreflightFailure("installed_worker_runtime_unavailable", stage="worker_status") from exc
    try:
        result = subprocess.run(
            [str(python), str(worker), "status"], capture_output=True,
            text=True, timeout=35, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightFailure("worker_status_timeout", stage="worker_status",
                               safe_fields={"timeout_seconds": 35},
                               legacy_error="production_queue_status_unavailable") from exc
    except OSError as exc:
        raise PreflightFailure("worker_status_launch_failed", stage="worker_status",
                               legacy_error="production_queue_status_unavailable") from exc
    except subprocess.SubprocessError as exc:
        raise PreflightFailure("worker_status_execution_failed", stage="worker_status",
                               legacy_error="production_queue_status_unavailable") from exc
    if result.returncode != 0:
        raise PreflightFailure("worker_status_nonzero_exit", stage="worker_status",
                               safe_fields={"worker_status_exit_code": result.returncode},
                               legacy_error="production_queue_status_unavailable")
    try:
        status = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise PreflightFailure("worker_status_invalid_json", stage="worker_status",
                               legacy_error="production_queue_status_unavailable") from exc
    lanes = status.get("lanes") if isinstance(status, dict) else None
    if not isinstance(lanes, dict):
        raise PreflightFailure("worker_status_schema_invalid", stage="worker_status",
                               legacy_error="production_queue_status_unavailable")
    configured_lanes = [lane for lane in ("GPOINT", "GSALES")
                        if isinstance(lanes.get(lane), dict) and lanes[lane].get("configured", True)]
    if not configured_lanes:
        raise PreflightFailure("no_configured_production_lanes", stage="worker_status")
    if all(lanes[lane].get("queue_snapshot_state") == "VERIFIED" for lane in configured_lanes):
        return status
    unresolved_lanes = [lane for lane in configured_lanes
                        if lanes[lane].get("queue_snapshot_state") != "VERIFIED"]
    if not unresolved_lanes:
        raise PreflightFailure("authoritative_queue_snapshot_unavailable", stage="queue_snapshot")
    for lane in unresolved_lanes:
        lane_status = lanes[lane]
        health = lane_status.get("health")
        queue_error = lane_status.get("queue_snapshot_error_code")
        if health not in {"HEALTHY", "DEGRADED"}:
            raise PreflightFailure("lane_health_invalid", stage="lane_health_validation", lane=lane)
        if not _live_snapshot_fallback_allowed(lane_status):
            if health != "HEALTHY" and queue_error == "worker_gateway_http_404":
                raise PreflightFailure("lane_health_queue_error_combination_disallowed",
                                       stage="lane_health_validation", lane=lane,
                                       safe_fields=_safe_lane_error_fields(lane_status),
                                       legacy_error="production_queue_status_unavailable")
            if health != "HEALTHY":
                reason = ("lane_health_queue_error_combination_disallowed" if queue_error is not None
                          else "lane_health_invalid")
                raise PreflightFailure(reason, stage="lane_health_validation", lane=lane,
                                       safe_fields=_safe_lane_error_fields(lane_status),
                                       legacy_error="production_queue_status_unavailable")
            raise PreflightFailure("queue_snapshot_error_not_allowlisted",
                                   stage="queue_snapshot", lane=lane,
                                   safe_fields=_safe_lane_error_fields(lane_status),
                                   legacy_error="production_queue_status_unavailable",
                                   error_category="queue", exit_class="unknown")
    _refresh_local_pool_evidence(status)
    try:
        candidate_manifest = manifest or candidate()
    except Exception as exc:
        raise PreflightFailure("candidate_verification_failed", stage="origin_verification") from exc
    budget = retry_budget or QueueSnapshotRetryBudget()
    for lane in configured_lanes:
        try:
            expected_tenant = _configured_tenant_id(lane, candidate_manifest)
        except Exception as exc:
            raise PreflightFailure("tenant_binding_invalid", stage="tenant_validation", lane=lane) from exc
        snapshot = _read_live_runtime_queue_snapshot(lane, expected_tenant, retry_budget=budget)
        lanes[lane].update(snapshot)
    return status


def _runner_attempt_status(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() and not path.is_symlink() else ""
    except OSError:
        return ""


def _proven_pre_exec_transient_reason(marker: str, stderr: str | bytes | None) -> str | None:
    if marker != "PRE_EXEC_TRANSPORT_FAILURE:255":
        return None
    diagnostic = _output_text(stderr).lower()
    if any(token in diagnostic for token in ("connect timed out", "connection timed out", "operation timed out")):
        return "ssh_pre_exec_transport_timeout"
    if any(token in diagnostic for token in (
            "connection reset by peer", "connection closed by", "connection lost", "broken pipe")):
        return "ssh_pre_exec_transport_interrupted"
    if "banner exchange" in diagnostic or re.search(r"kex_exchange_identification.*(closed|reset|timeout)", diagnostic):
        return "ssh_pre_exec_handshake_interrupted"
    return None


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


def _replace_lane_runtime_pointer(root: Path, lane: str, source_sha: str) -> str | None:
    """Atomically point one lane at an immutable installed runtime release."""
    if lane not in {"GPOINT", "GSALES", "LAB"} or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("worker_lane_runtime_identity_invalid")
    root = Path(root)
    runtime_root = root / "runtime"
    slots_root = runtime_root / "slots"
    lane_dir = slots_root / lane
    release = runtime_root / source_sha
    for directory in (runtime_root, slots_root, lane_dir):
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700)
        if (directory.is_symlink() or not directory.is_dir()
                or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
            raise RuntimeError("worker_lane_runtime_path_invalid")
    if (release.is_symlink() or not release.is_dir() or release.parent != runtime_root
            or release.stat().st_uid != os.getuid() or release.stat().st_mode & 0o077):
        raise RuntimeError("worker_lane_runtime_release_invalid")
    current = lane_dir / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise RuntimeError("worker_lane_runtime_pointer_invalid")
    if previous is not None:
        try:
            resolved_previous = current.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("worker_lane_runtime_pointer_invalid") from exc
        if resolved_previous.parent != runtime_root.resolve(strict=True):
            raise RuntimeError("worker_lane_runtime_pointer_invalid")
    temporary = lane_dir / ".current-next"
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_dir() and not temporary.is_symlink():
            raise RuntimeError("worker_lane_runtime_pointer_invalid")
        temporary.unlink()
    temporary.symlink_to(os.path.relpath(release, lane_dir))
    os.replace(temporary, current)
    if current.resolve(strict=True) != release.resolve(strict=True):
        if previous is None:
            current.unlink(missing_ok=True)
        else:
            rollback = lane_dir / ".current-rollback"
            rollback.unlink(missing_ok=True)
            rollback.symlink_to(previous)
            os.replace(rollback, current)
        raise RuntimeError("worker_lane_runtime_switch_verification_failed")
    return previous


def _switch_all_lane_runtime_pointers(root: Path, source_sha: str) -> dict[str, str | None]:
    """Switch all slots as part of the already-paused full-pool transition."""
    changed: dict[str, str | None] = {}
    try:
        for lane in ("GPOINT", "GSALES", "LAB"):
            changed[lane] = _replace_lane_runtime_pointer(root, lane, source_sha)
    except Exception:
        for lane, previous in reversed(list(changed.items())):
            current = Path(root) / "runtime/slots" / lane / "current"
            if previous is None:
                current.unlink(missing_ok=True)
            else:
                rollback = current.parent / ".current-rollback"
                rollback.unlink(missing_ok=True)
                rollback.symlink_to(previous)
                os.replace(rollback, current)
        raise
    return changed


def _restore_lane_runtime_pointer(root: Path, lane: str, previous: str | None) -> None:
    """Restore and verify one lane pointer after an incomplete switch."""
    lane_dir = Path(root) / "runtime/slots" / lane
    current = lane_dir / "current"
    if previous is None:
        current.unlink(missing_ok=True)
        if current.exists() or current.is_symlink():
            raise RuntimeError("worker_lane_runtime_rollback_failed")
        return
    rollback = lane_dir / ".current-rollback"
    rollback.unlink(missing_ok=True)
    rollback.symlink_to(previous)
    os.replace(rollback, current)
    try:
        if current.resolve(strict=True) != (lane_dir / previous).resolve(strict=True):
            raise RuntimeError("worker_lane_runtime_rollback_failed")
    except OSError as exc:
        raise RuntimeError("worker_lane_runtime_rollback_failed") from exc


def install_lane_slot(lane: str) -> dict:
    """Update one lane child runtime without stopping the shared supervisor."""
    if lane not in {"GPOINT", "GSALES", "LAB"}:
        raise RuntimeError("worker_lane_runtime_identity_invalid")
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    transition_fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(transition_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(transition_fd)
        raise RuntimeError("worker_transition_already_running") from exc
    try:
        if _release_lock_busy():
            raise RuntimeError("production_release_lock_busy")
        if (ROOT / "state/maintenance-stop").exists():
            raise RuntimeError("worker_runtime_maintenance_active")
        current_supervisor = ROOT / "runtime/current/agata_worker_pool.py"
        try:
            supports_lane_pointers = "def slot_runtime_current(" in current_supervisor.read_text(encoding="utf-8")
        except OSError:
            supports_lane_pointers = False
        if not supports_lane_pointers:
            # A lane-only update must never silently turn into a pool-wide
            # supervisor/LaunchAgent transition. The one-time legacy bootstrap
            # is the explicit full-pool `apply` action, with its own idle,
            # authoritative queue, lock, health, and rollback gates.
            raise RuntimeError("worker_pool_bootstrap_required_use_full_transition_apply")
        manifest = candidate()
        queue_preflight = None
        if lane in {"GPOINT", "GSALES"}:
            status = _read_production_queue_status(manifest)
            queue_preflight = require_fresh_empty_production_queues(status)
        staged = _stage_runtime(manifest)
        release = _install_staged_runtime(staged, manifest)
        _verify_staged_runtime(release, manifest)
        slot_id = {"GPOINT": "slot-01", "GSALES": "slot-02", "LAB": "slot-03"}[lane]
        slots_root = ROOT / "slots"
        if not slots_root.exists():
            slots_root.mkdir(mode=0o700)
        if (slots_root.is_symlink() or not slots_root.is_dir()
                or slots_root.stat().st_uid != os.getuid() or slots_root.stat().st_mode & 0o077):
            raise RuntimeError("worker_lane_slot_lock_path_invalid")
        slot_directory = slots_root / slot_id
        slot_directory.mkdir(exist_ok=True, mode=0o700)
        slot_lock_path = slot_directory / "ownership.lock"
        if (slot_directory.is_symlink() or not slot_directory.is_dir()
                or slot_directory.stat().st_uid != os.getuid()
                or slot_directory.stat().st_mode & 0o077 or slot_lock_path.is_symlink()):
            raise RuntimeError("worker_lane_slot_lock_path_invalid")
        slot_fd = os.open(slot_lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(slot_fd, 0o600)
            try:
                fcntl.flock(slot_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("worker_lane_slot_busy") from exc
            previous = _replace_lane_runtime_pointer(ROOT, lane, manifest["source_sha"])
            attempt_id = uuid.uuid4().hex
            receipt = {
                "state": "COMPLETE", "scope": f"{lane}_SLOT_ONLY", "lane": lane,
                "attempt_id": attempt_id,
                "source_sha": manifest["source_sha"], "bundle_sha256": manifest["bundle_sha256"],
                "previous_pointer": previous, "current_pointer": manifest["source_sha"],
                "other_lane_slots_changed": False, "launchagent_changed": False,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            if queue_preflight is not None:
                receipt["queue_preflight"] = queue_preflight
            try:
                _atomic_json(ROOT / "receipts" / f"lane-slot-runtime-switch-{lane.lower()}-{attempt_id}.json", receipt)
            except Exception:
                _restore_lane_runtime_pointer(ROOT, lane, previous)
                raise
            return receipt
        finally:
            os.close(slot_fd)
    finally:
        os.close(transition_fd)


def install_lab_slot() -> dict:
    """Compatibility entry point for updating only the local LAB slot."""
    return install_lane_slot("LAB")


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


def _transition_lock_is_busy() -> bool:
    if not TRANSITION_LOCK.exists():
        return False
    try:
        descriptor = os.open(TRANSITION_LOCK, os.O_RDONLY)
    except OSError as exc:
        raise PreflightFailure("transition_lock_check_failed", stage="lock_validation") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _run_safety_preflight(stage: str, *, manifest: dict | None = None,
                          transition_lock_already_held: bool = False) -> dict:
    """Run the same fail-closed health/queue gate used by apply and record a safe receipt."""
    started_at = datetime.now(UTC).isoformat()
    retry_budget = QueueSnapshotRetryBudget()
    try:
        if not transition_lock_already_held and _transition_lock_is_busy():
            raise PreflightFailure("transition_lock_busy", stage="lock_validation")
        try:
            candidate_manifest = manifest or candidate()
        except Exception as exc:
            code = str(exc)
            if code not in {"origin_main_unavailable", "origin_main_ref_stale", "candidate_must_be_origin_main",
                            "candidate_worktree_dirty"}:
                code = "candidate_verification_failed"
            raise PreflightFailure(code, stage="origin_verification") from exc
        try:
            before = inventory()
        except Exception as exc:
            raise PreflightFailure("worker_local_inventory_unavailable", stage="local_inventory") from exc
        if before.get("production_release_lock_busy"):
            raise PreflightFailure("production_release_lock_busy", stage="lock_validation")
        if before.get("active_inference") is not False:
            raise PreflightFailure("active_inference_present", stage="local_inventory",
                                   legacy_error="active_inference_blocks_transition")
        if before.get("launchagent_loaded") is not True:
            raise PreflightFailure("launchagent_not_loaded", stage="local_inventory",
                                   legacy_error="worker_launchagent_not_loaded")
        status = _read_production_queue_status(candidate_manifest, retry_budget=retry_budget)
        try:
            evidence = require_fresh_empty_production_queues(status)
        except PreflightFailure as exc:
            exc.attempts = list(retry_budget.attempts)
            raise
        summary = {
            "candidate_verified": True,
            "launchagent_loaded": True,
            "active_inference_absent": True,
            "locks_free": True,
            "queue_snapshots_verified": True,
            "queues_empty": True,
            "lane_health_healthy": True,
        }
        _record_preflight(stage, summary=summary, attempts=retry_budget.attempts, started_at=started_at)
        return {"manifest": candidate_manifest, "inventory": before, "status": status, "queue_evidence": evidence}
    except PreflightFailure as exc:
        if not exc.attempts:
            exc.attempts = list(retry_budget.attempts)
        _record_preflight(stage, exc, attempts=retry_budget.attempts, started_at=started_at)
        raise
    except RuntimeError as exc:
        code = str(exc)
        mappings = {
            "production_queue_snapshot_invalid": "queue_snapshot_invalid",
            "production_queue_snapshot_stale": "queue_snapshot_stale",
            "worker_slots_busy": "local_pool_not_idle",
            "production_queue_not_empty": "production_queue_not_empty",
            "production_queue_status_unavailable": "authoritative_queue_snapshot_unavailable",
        }
        failure = PreflightFailure(mappings.get(code, "preflight_validation_failed"), stage="preflight_validation",
                                   legacy_error=code)
        _record_preflight(stage, failure, attempts=retry_budget.attempts, started_at=started_at)
        raise failure from exc


def read_only_preflight() -> dict:
    """Run one read-only installation gate and return only sanitized summaries."""
    preflight = _run_safety_preflight("READ_ONLY_PREFLIGHT")
    status = preflight["status"]
    evidence = preflight["queue_evidence"]
    lanes = {}
    for lane in ("GPOINT", "GSALES"):
        lane_status = status["lanes"][lane]
        queue = evidence["lanes"][lane]
        lanes[lane] = {
            "health": lane_status.get("health"),
            "queue_snapshot_state": lane_status.get("queue_snapshot_state"),
            "pending": queue["pending_jobs"],
            "running": queue["running_jobs"],
            "snapshot_fresh": True,
            "identity_and_readonly_provenance_verified": True,
        }
    return {
        "outcome": "PASS",
        "candidate_verified": True,
        "launchagent_loaded": True,
        "active_inference_absent": True,
        "release_and_transition_locks_free": True,
        "pool_busy": evidence["busy_slots"],
        "lanes": lanes,
        "mutation_performed": False,
    }


def apply() -> dict:
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        failure = PreflightFailure("transition_lock_busy", stage="lock_validation",
                                   legacy_error="worker_transition_already_running")
        _record_preflight("APPLY_INITIAL", failure)
        raise failure from exc
    try:
        # Repeat all safety checks at the point of mutation.
        preflight = _run_safety_preflight("APPLY_INITIAL", transition_lock_already_held=True)
        manifest = preflight["manifest"]
        before = preflight["inventory"]
        queue_evidence = preflight["queue_evidence"]
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
                paused_preflight = _run_safety_preflight(
                    "APPLY_PAUSED_PRE_BOOTOUT", manifest=manifest, transition_lock_already_held=True,
                )
                queue_evidence = paused_preflight["queue_evidence"]
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
            _switch_all_lane_runtime_pointers(ROOT, manifest["source_sha"])
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
    parser.add_argument("action", choices=("plan", "preflight", "prepare", "apply", "rollback",
                                            "install-lab-slot", "install-lane-slot"))
    parser.add_argument("--lane", choices=("GPOINT", "GSALES", "LAB"))
    args = parser.parse_args()
    if args.action == "install-lane-slot" and not args.lane:
        raise RuntimeError("worker_lane_required")
    result = (read_only_plan() if args.action == "plan" else read_only_preflight() if args.action == "preflight"
              else prepare() if args.action == "prepare"
              else rollback() if args.action == "rollback" else
              install_lab_slot() if args.action == "install-lab-slot" else
              install_lane_slot(args.lane) if args.action == "install-lane-slot" and args.lane else
              apply())
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightFailure as exc:
        if len(sys.argv) > 1 and sys.argv[1] == "preflight":
            print(json.dumps({
                "outcome": "BLOCKED", "reason_code": exc.reason_code, "stage": exc.stage,
                "lane": exc.lane, "diagnostic_receipt_written": exc.receipt_written,
                **exc.safe_fields,
            }, sort_keys=True))
            raise SystemExit(1) from exc
        raise SystemExit(exc.reason_code) from exc
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
