#!/usr/bin/env python3
"""Controlled migration of the local Eros Worker LaunchAgent runtime.

The plan is read-only. Apply is fail-closed on non-main/dirty source, active
inference, a concurrent transition, invalid credentials/configuration, or any
failed health/restart check. No remote host or unrelated GPoint service is contacted.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

OLD_LABEL = "one.gpoint.agata-codex-worker"
LABEL = "one.gpoint.eros-worker"
HOME = Path.home()
OLD_ROOT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
ROOT = HOME / "Library/Application Support/GPoint/gpoint-eros-worker"
LEGACY = OLD_ROOT
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
OLD_PLIST = HOME / "Library/LaunchAgents" / f"{OLD_LABEL}.plist"
TRANSITION_LOCK = HOME / ".config/gpoint/agata-worker-pool-transition.lock"
RELEASE_LOCK = HOME / ".config/gpoint/release-production.lock"
CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
WORKER_DIR = Path(__file__).resolve().parent
ASSETS = ("gpoint_mac_worker.py", "run_codex_worker.sh", "requirements.txt")
QUEUE_SNAPSHOT_MAX_AGE_SECONDS = 30
QUEUE_SNAPSHOT_FUTURE_SKEW_SECONDS = 5
QUEUE_SNAPSHOT_MAX_RETRIES = 2
QUEUE_SNAPSHOT_REQUEST_TIMEOUT_SECONDS = 25
QUEUE_SNAPSHOT_PREFLIGHT_DEADLINE_SECONDS = 90
QUEUE_SNAPSHOT_RETRY_DELAYS_SECONDS = (0.25, 0.5)
LAUNCHAGENT_UNLOAD_TIMEOUT_SECONDS = 20
SAFE_DIAGNOSTIC_ERROR_CODES = frozenset({
    "worker_claim_invalid", "worker_claim_target_mismatch", "worker_config_permissions_invalid",
    "worker_gateway_auth_rejected", "worker_gateway_http_404", "worker_gateway_network_error",
    "worker_gateway_or_auth_unconfigured", "worker_gateway_response_invalid", "worker_gateway_transport_error",
    "worker_gateway_url_invalid", "worker_target_invalid",
    "worker_target_unconfigured", "worker_tenant_binding_invalid",
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
        self.lane = lane if lane == "GPOINT" else None
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
        if not isinstance(item, dict) or item.get("lane") != "GPOINT":
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


def launchctl_loaded(label: str = LABEL) -> bool:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                            capture_output=True, text=True, timeout=8, check=False)
    if result.returncode != 0:
        return False
    state = re.search(r"^\s*state = ([^\s]+)\s*$", result.stdout or "", re.MULTILINE)
    return bool(state and state.group(1).lower() in {"running", "waiting"})


def _active_worker_identity(*, require_loaded: bool = True) -> tuple[str, Path, Path]:
    """Return the one loaded service identity; refuse ambiguous dual agents."""
    old_loaded = launchctl_loaded(OLD_LABEL)
    eros_loaded = launchctl_loaded(LABEL)
    if old_loaded and eros_loaded:
        raise RuntimeError("multiple_worker_launchagents_loaded")
    if old_loaded:
        return OLD_LABEL, OLD_ROOT, OLD_PLIST
    if eros_loaded:
        return LABEL, ROOT, PLIST
    if require_loaded:
        raise RuntimeError("worker_launchagent_not_loaded")
    old_present = OLD_PLIST.is_file() and not OLD_PLIST.is_symlink()
    eros_present = PLIST.is_file() and not PLIST.is_symlink()
    if old_present == eros_present:
        raise RuntimeError("worker_launchagent_identity_ambiguous")
    return (OLD_LABEL, OLD_ROOT, OLD_PLIST) if old_present else (LABEL, ROOT, PLIST)


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
        point = targets.get("GPOINT")
        result["targets"] = ({"GPOINT": {key: point.get(key) for key in fields if key in point}}
                              if isinstance(point, dict) else {})
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
        if "gpoint_mac_worker.py" in lower and any(token in lower for token in (" execute", " infer")):
            try:
                found.append(int(line.strip().split(None, 1)[0]))
            except (ValueError, IndexError):
                continue
    for record_path in (OLD_ROOT / "inference-process.json", OLD_ROOT / "state/inference-process.json",
                        ROOT / "state/inference-process.json"):
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


def _launchagent_pid(label: str | None = None) -> tuple[int, str]:
    if label is None:
        label, _root, _plist = _active_worker_identity()
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=True,
                            capture_output=True, text=True, timeout=8)
    match = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("launchagent_pid_ambiguous")
    pid = int(match.group(1))
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], check=True,
                             capture_output=True, text=True, timeout=8).stdout.strip()
    expected_roots = (OLD_ROOT, ROOT / "runtime") if label == OLD_LABEL else (ROOT / "runtime",)
    if "gpoint_mac_worker.py watch" not in command or not any(
            str(root) in command for root in expected_roots):
        raise RuntimeError("launchagent_process_identity_invalid")
    return pid, command


def _wait_launchagent_unloaded(previous_pid: int | None, *, label: str | None = None,
                               deadline: float = LAUNCHAGENT_UNLOAD_TIMEOUT_SECONDS) -> bool:
    """Wait for launchd to remove the label and for observed worker PIDs to exit."""
    if label is None:
        label, _root, _plist = _active_worker_identity(require_loaded=False)
    service_id = f"gui/{os.getuid()}/{label}"
    observed_pids = {previous_pid} if isinstance(previous_pid, int) and previous_pid > 1 else set()
    until = time.monotonic() + deadline
    while True:
        result, command_error = _run_readonly_command(["launchctl", "print", service_id], 5)
        output = result.stdout or ""
        error = result.stderr or ""
        if result.returncode == 0 and f"{label} = {{" in output:
            pid_match = re.search(r"^\s*pid = ([0-9]+)\s*$", output, re.MULTILINE)
            if pid_match:
                observed_pids.add(int(pid_match.group(1)))
            service_absent = False
        elif result.returncode != 0 and f'Could not find service "{label}"' in error:
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


def _bootout_and_wait(label: str) -> None:
    """Remove one LaunchAgent and prove its label and worker process are gone."""
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                   capture_output=True, text=True, timeout=20, check=False)
    _wait_launchagent_unloaded(None, label=label)


def _restore_single_launchagent(source_label: str, source_plist: Path,
                                original_plist: bytes) -> dict:
    """Fail closed: unload Eros before restoring and starting exactly one source agent."""
    try:
        _bootout_and_wait(LABEL)
    except Exception:
        # If Eros cannot be unloaded while the source is also loaded, stop the
        # source as the final safety action. A failed recovery may leave zero
        # workers or one worker, but must not knowingly leave both running.
        if source_label != LABEL and launchctl_loaded(source_label):
            try:
                _bootout_and_wait(source_label)
            except Exception:
                pass
        raise
    if launchctl_loaded(LABEL):
        raise RuntimeError("rollback_candidate_launchagent_still_loaded")
    if source_label != LABEL and launchctl_loaded(source_label):
        _bootout_and_wait(source_label)
    if launchctl_loaded(LABEL) or (source_label != LABEL and launchctl_loaded(source_label)):
        raise RuntimeError("rollback_multiple_launchagents_loaded")
    source_plist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = source_plist.with_name(f".{source_plist.name}.{os.getpid()}.restore")
    temporary.write_bytes(original_plist)
    temporary.chmod(0o600)
    os.replace(temporary, source_plist)
    try:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(source_plist)],
                       check=True, timeout=20, capture_output=True, text=True)
        if not launchctl_loaded(source_label):
            raise RuntimeError("rollback_launchagent_not_loaded")
        if source_label != LABEL and launchctl_loaded(LABEL):
            raise RuntimeError("rollback_multiple_launchagents_loaded")
        return {"restored_label": source_label, "launchagent_loaded": True,
                "candidate_launchagent_loaded": launchctl_loaded(LABEL)}
    except Exception:
        # A failed bootstrap may have partially registered the source. Stop it
        # before returning so recovery never leaves both labels active.
        try:
            _bootout_and_wait(source_label)
        except Exception:
            pass
        raise


def _bootstrap_candidate_only(source_label: str, candidate_plist: Path) -> None:
    """Register Eros only after the source label is confirmed absent."""
    if launchctl_loaded(source_label) or launchctl_loaded(LABEL):
        raise RuntimeError("multiple_worker_launchagents_loaded")
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(candidate_plist)],
                   check=True, timeout=20)
    if not launchctl_loaded(LABEL) or (source_label != LABEL and launchctl_loaded(source_label)):
        raise RuntimeError("worker_launchagent_identity_ambiguous")


def _pause_idle_launchagent(label: str | None = None) -> int | None:
    if label is None:
        label, _root, _plist = _active_worker_identity(require_loaded=False)
    if not launchctl_loaded(label):
        return None
    pid, _command = _launchagent_pid(label)
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


def _pause_worker_or_clear_marker(source_label: str, marker: Path) -> int | None:
    """Do not leave a stale maintenance stop if pausing fails before bootout."""
    try:
        return _pause_idle_launchagent(source_label)
    except Exception:
        marker.unlink(missing_ok=True)
        raise


def inventory() -> dict:
    if OLD_ROOT.is_symlink() or (ROOT.exists() and ROOT.is_symlink()):
        raise RuntimeError("worker_runtime_root_symlink_invalid")
    if not CONFIG.is_file() or CONFIG.is_symlink() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    if not CODEX.is_file():
        raise RuntimeError("codex_cli_missing")
    active_label, active_root, active_plist = _active_worker_identity(require_loaded=False)
    legacy_status = safe_worker_status(OLD_ROOT / "status/worker-status.json")
    new_status = safe_worker_status(ROOT / "status/worker-status.json")
    pids = _running_inference_pids()
    legacy_pool_path = OLD_ROOT / "status/pool-status.json"
    legacy_pool_active = False
    if active_label == OLD_LABEL and legacy_pool_path.exists():
        if legacy_pool_path.is_symlink() or not legacy_pool_path.is_file():
            raise RuntimeError("legacy_worker_status_invalid")
        try:
            legacy_pool = json.loads(legacy_pool_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("legacy_worker_status_invalid") from exc
        slots = legacy_pool.get("slots") if isinstance(legacy_pool, dict) else None
        if not isinstance(slots, dict) or not slots:
            raise RuntimeError("legacy_worker_status_invalid")
        legacy_pool_active = any(
            not isinstance(slot, dict) or slot.get("state") != "IDLE" or slot.get("job_id") is not None
            for slot in slots.values()
        )
    if any(path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid())
           for path in (OLD_PLIST, PLIST)):
        raise RuntimeError("launchagent_plist_owner_invalid")
    old_plist = active_plist.read_bytes() if active_plist.is_file() else b""
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
    active = bool(pids or legacy_pool_active or active_status(legacy_status) or active_status(new_status))
    return {
        "label": active_label,
        "active_runtime_root": str(active_root),
        "active_plist": str(active_plist),
        "launchagent_loaded": launchctl_loaded(active_label),
        "old_launchagent_loaded": launchctl_loaded(OLD_LABEL),
        "eros_launchagent_loaded": launchctl_loaded(LABEL),
        "production_release_lock_busy": _release_lock_busy(),
        "launchagent_program_arguments": old_args,
        "legacy_runtime_present": OLD_ROOT.exists(),
        "legacy_worker_status": legacy_status,
        "new_worker_status": new_status,
        "legacy_pool_active": legacy_pool_active,
        "active_inference_pids": pids,
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


def validate_fresh_production_queue_snapshots(
    status: dict, *, lanes: tuple[str, ...] = ("GPOINT",), now: datetime | None = None,
) -> dict:
    """Validate fresh GPOINT queue and local single-worker idle evidence."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or not isinstance(status, dict):
        raise PreflightFailure("queue_snapshot_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    if not isinstance(lanes, tuple) or any(lane != "GPOINT" for lane in lanes):
        raise PreflightFailure("queue_lane_scope_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    if (status.get("current_job_present") is not False or status.get("active_inference") is not False
            or status.get("launch_agent_loaded") is not True):
        raise PreflightFailure("launchagent_not_loaded", stage="local_worker_validation",
                               legacy_error="production_queue_snapshot_invalid")
    busy_workers = 0
    worker_authority = "gpoint_eros_single_worker_status_v1"
    try:
        captured_value = status.get("last_poll_at")
        pool_captured = datetime.fromisoformat(captured_value.replace("Z", "+00:00"))
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
    evidence = _validate_production_queue_lanes(status.get("lanes"), current=current, selected_lanes=lanes)
    return {"checked_at": current.isoformat(), "worker_snapshot_at": pool_captured.isoformat(),
            "worker_authority": worker_authority, "active_workers": busy_workers, "lanes": evidence}


def _validate_production_queue_lanes(
    lanes: dict, *, current: datetime, selected_lanes: tuple[str, ...] = ("GPOINT",),
) -> dict:
    """Validate authoritative GPOINT queue evidence, independently of local worker state."""
    if not isinstance(lanes, dict) or set(lanes) != {"GPOINT"}:
        raise PreflightFailure("worker_status_schema_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    if (not selected_lanes or len(set(selected_lanes)) != len(selected_lanes)
            or any(lane != "GPOINT" for lane in selected_lanes)):
        raise PreflightFailure("queue_lane_scope_invalid", stage="queue_validation",
                               legacy_error="production_queue_snapshot_invalid")
    evidence = {}
    for lane in selected_lanes:
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
        eros_source = "gpoint-eros-worker-queue-status-v1:gpoint"
        source = value.get("queue_snapshot_source_id")
        if source != eros_source or value.get("queue_snapshot_provenance") is not None:
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
    return evidence


def require_fresh_empty_production_queues(
    status: dict, *, lanes: tuple[str, ...] = ("GPOINT",), now: datetime | None = None,
) -> dict:
    """Require a fresh empty GPOINT queue and no active local worker operation."""
    evidence = validate_fresh_production_queue_snapshots(status, lanes=lanes, now=now)
    if evidence["active_workers"] != 0:
        raise PreflightFailure("local_worker_not_idle", stage="queue_validation",
                               legacy_error="worker_operation_active")
    for lane, item in evidence["lanes"].items():
        if item["pending_jobs"] or item["running_jobs"]:
            raise PreflightFailure("production_queue_not_empty", stage="queue_validation", lane=lane,
                                   legacy_error="production_queue_not_empty")
    return evidence


def _read_owned_regular_file(path: Path, *, expected_mode: int | None = None,
                             forbidden_mode_bits: int = 0) -> tuple[bytes, os.stat_result]:
    """Read a private file without following a swapped or symlink path."""
    before = path.lstat()
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid() or before.st_nlink != 1
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
            or stat.S_IMODE(before.st_mode) & forbidden_mode_bits):
        raise ValueError("owned_file_identity_invalid")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or (expected_mode is not None and stat.S_IMODE(opened.st_mode) != expected_mode)
                or stat.S_IMODE(opened.st_mode) & forbidden_mode_bits):
            raise ValueError("owned_file_identity_changed")
        chunks = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after_fd = os.fstat(fd)
        after_path = path.lstat()
        identity = ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(opened, item) != getattr(after_fd, item) for item in identity)
                or any(getattr(opened, item) != getattr(after_path, item) for item in identity)
                or sum(map(len, chunks)) != opened.st_size):
            raise ValueError("owned_file_identity_changed")
        return b"".join(chunks), opened
    finally:
        os.close(fd)


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


def _read_production_queue_status(manifest: dict | None = None, *,
                                  retry_budget: QueueSnapshotRetryBudget | None = None,
                                  lane_scope: tuple[str, ...] | None = None) -> dict:
    """Read the selected GPOINT runtime and its authoritative queue status only."""
    runtime_link = (ROOT / "runtime/current" if (ROOT / "runtime/current").is_symlink()
                    else OLD_ROOT / "runtime/current")
    try:
        if not runtime_link.is_symlink():
            raise RuntimeError("installed_worker_runtime_unavailable")
        runtime = runtime_link.resolve(strict=True)
        runtime_root = runtime_link.parent.resolve(strict=True)
        if not runtime.is_relative_to(runtime_root):
            raise RuntimeError("installed_worker_runtime_invalid")
        # Run the lane-scoped read-only status from this candidate source so
        # the legacy installed process never polls a non-GPOINT target.
        worker = WORKER_DIR / "gpoint_mac_worker.py"
        python = runtime / ".venv/bin/python3.12"
        if (not worker.is_file() or worker.is_symlink()
                or not python.is_file() or not os.access(python, os.X_OK)):
            raise RuntimeError("installed_worker_runtime_unavailable")
    except OSError as exc:
        raise PreflightFailure("installed_worker_runtime_unavailable", stage="worker_status") from exc
    try:
        command = [str(python), str(worker), "status"]
        if lane_scope is not None:
            if (not lane_scope or len(set(lane_scope)) != len(lane_scope)
                    or any(lane != "GPOINT" for lane in lane_scope)):
                raise RuntimeError("worker_queue_lane_scope_invalid")
            if len(lane_scope) == 1:
                command.extend(("--lane", lane_scope[0]))
        status_env = _launchagent_environment()
        status_env["GPOINT_EROS_WORKER_SUPPORT"] = str(runtime_link.parent.parent)
        result = subprocess.run(
            command, capture_output=True,
            text=True, timeout=35, check=False, env=status_env,
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
    if not isinstance(lanes, dict) or set(lanes) != {"GPOINT"}:
        raise PreflightFailure("worker_status_schema_invalid", stage="worker_status",
                               legacy_error="production_queue_status_unavailable")
    old_loaded = launchctl_loaded(OLD_LABEL)
    eros_loaded = launchctl_loaded(LABEL)
    if old_loaded == eros_loaded:
        raise PreflightFailure("worker_launchagent_identity_ambiguous", stage="local_inventory",
                               legacy_error="production_queue_status_unavailable")
    status["launch_agent_loaded"] = old_loaded or eros_loaded
    status["active_inference"] = bool(_running_inference_pids())
    if not isinstance(lanes.get("GPOINT"), dict) or lanes["GPOINT"].get("configured", True) is False:
        raise PreflightFailure("worker_status_lane_scope_mismatch", stage="worker_status")
    if lanes["GPOINT"].get("queue_snapshot_state") == "VERIFIED":
        return status
    raise PreflightFailure("authoritative_queue_snapshot_unavailable", stage="queue_snapshot",
                           lane="GPOINT", legacy_error="production_queue_status_unavailable")


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
    label, source_root, source_plist = _active_worker_identity()
    if not source_plist.is_file() or source_plist.is_symlink():
        raise RuntimeError("launchagent_plist_missing_or_invalid")
    original = source_plist.read_bytes()
    try:
        value = plistlib.loads(original)
    except Exception as exc:
        raise RuntimeError("launchagent_plist_invalid") from exc
    expected_arguments = (
        [str(source_root / "run_codex_worker.sh"), "watch"],
        [str(source_root / "runtime/current/run_codex_worker.sh"), "watch"],
    )
    if value.get("Label") != label or value.get("ProgramArguments") not in expected_arguments:
        raise RuntimeError("launchagent_identity_invalid")
    temporary = rollback_tree / f".original-launchagent-{os.getpid()}.tmp"
    temporary.write_bytes(original)
    temporary.chmod(0o600)
    os.replace(temporary, rollback_tree / "original-launchagent.plist")
    _atomic_json(rollback_tree / "source-identity.json", {
        "version": 1, "label": label, "runtime_root": str(source_root),
        "plist_path": str(source_plist),
    })
    return original


def prepare() -> dict:
    """Stage candidate and verify rollback evidence without stopping the worker."""
    manifest = candidate()
    before = inventory()
    if before["production_release_lock_busy"]:
        raise RuntimeError("production_release_lock_busy")
    if before["active_inference"]:
        raise RuntimeError("active_inference_blocks_transition")
    active_root = Path(before["active_runtime_root"])
    expected_program = str(active_root / "run_codex_worker.sh")
    current_program = str(active_root / "runtime/current/run_codex_worker.sh")
    if before["launchagent_program_arguments"] not in (
            [expected_program, "watch"], [current_program, "watch"]):
        raise RuntimeError("launchagent_identity_invalid")
    _ensure_runtime_directories()
    staged_runtime = _stage_runtime(manifest)
    startup_check = _validate_candidate_startup(staged_runtime, manifest)
    rollback_tree = _ensure_rollback_tree(manifest["source_sha"])
    original_plist = _save_original_launchagent(rollback_tree)
    backup = rollback_tree / "legacy-runtime"
    last_error = None
    for _attempt in range(3):
        try:
            _prepare_legacy_snapshot(backup, source_root=active_root)
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
    return ROOT.parent / ".gpoint-eros-worker-rollback"


def _runtime_staging_base() -> Path:
    return ROOT.parent / ".gpoint-eros-worker-staging"


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
    return ROOT.parent / ".gpoint-eros-worker-diagnostics"


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
        "worker_gateway_url_invalid", "worker_poll_interval_invalid",
        "worker_root_must_be_absolute", "worker_target_unconfigured",
        "worker_tenant_binding_invalid",
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
        "GPOINT_EROS_WORKER_CONFIG": str(CONFIG),
        "GPOINT_EROS_WORKER_SUPPORT": str(ROOT),
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


def _candidate_startup_environment(manifest: dict) -> dict[str, str]:
    """Use the existing private GPOINT config; never import legacy lane bindings."""
    del manifest
    if not _config_declares_key("GPOINT_EROS_TENANT_ID"):
        raise RuntimeError("worker_tenant_binding_invalid")
    return _launchagent_environment()


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
        and isinstance(lanes, dict)
        and isinstance(lanes.get("GPOINT"), dict)
        and lanes["GPOINT"].get("configured") is True
        and set(lanes) == {"GPOINT"}
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
    """Verify exact candidate, the sole Eros agent, its process and GPOINT health."""
    service_id = f"gui/{os.getuid()}/{LABEL}"
    service_result, service_error = _run_readonly_command(["launchctl", "print", service_id], command_timeout)
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
    process_result, process_error = _run_readonly_command(
        ["ps", "-p", str(pid), "-o", "command="], command_timeout,
    ) if pid_exists else (subprocess.CompletedProcess([], 1, "", ""), None)
    process_output = (process_result.stdout or "").strip()
    runtime_identity = _runtime_candidate_identity(expected_source_sha)
    expected_runtime = ROOT / "runtime" / expected_source_sha
    process_identity = bool(
        pid_exists and runtime_identity.get("passed") is True
        and str(expected_runtime) in process_output
        and "gpoint_mac_worker.py watch" in process_output
    )
    status_result, status_error = _run_readonly_command(
        [str(expected_runtime / ".venv/bin/python3.12"),
         str(expected_runtime / "gpoint_mac_worker.py"), "status"], command_timeout,
    )
    try:
        status = json.loads(status_result.stdout)
    except (ValueError, TypeError):
        status = {}
    lanes = status.get("lanes", {}) if isinstance(status.get("lanes"), dict) else {}
    point = lanes.get("GPOINT", {}) if isinstance(lanes.get("GPOINT"), dict) else {}
    old_loaded = launchctl_loaded(OLD_LABEL)
    eros_loaded = launchctl_loaded(LABEL)
    process_inventory, inventory_error = _run_readonly_command(
        ["ps", "-axo", "pid=,command="], command_timeout,
    )
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
    queue_evidence = None
    try:
        queue_evidence = validate_fresh_production_queue_snapshots(
            {**status, "launch_agent_loaded": True, "active_inference": not not active_pids},
            lanes=("GPOINT",),
        )
    except (PreflightFailure, RuntimeError):
        pass
    checks = {
        "service": service_result.returncode == 0 and f"{LABEL} = {{" in service_output
                   and "state = running" in service_output,
        "exactly_one_launchagent": eros_loaded and not old_loaded,
        "pid": pid_exists,
        "process_identity": process_identity,
        "runtime_parity": runtime_identity.get("passed") is True,
        "status": status_result.returncode == 0 and isinstance(status, dict),
        "gpoint_health": point.get("health") == "HEALTHY",
        "gpoint_queue_verified": point.get("queue_snapshot_state") == "VERIFIED"
                                   and queue_evidence is not None,
        "no_active_generation": not active_pids and status.get("current_job_present") is False
                                and status.get("active_inference") is False,
        "one_worker_process": len(worker_processes) == 1 and worker_processes[0]["pid"] == pid,
    }
    safe_status = {
        "worker_version": status.get("worker_version"),
        "last_poll_at": status.get("last_poll_at"),
        "current_job_present": status.get("current_job_present"),
        "active_inference": status.get("active_inference"),
        "lanes": {"GPOINT": {key: point.get(key) for key in (
            "configured", "health", "queue_snapshot_state", "queue_snapshot_at",
            "pending_inference_jobs", "running_inference_jobs", "last_error_code",
        )}},
    }
    return {
        "passed": all(checks.values()), "checks": checks, "pid": pid,
        "active_inference_pids": active_pids, "worker_processes": worker_processes,
        "command_errors": {"launchctl_print": service_error, "process": process_error,
                           "worker_status": status_error, "process_inventory": inventory_error},
        "launchctl_print": _redact_command_output(service_output),
        "launchctl_stderr": _redact_command_output(service_result.stderr),
        "process_command": _redact_command_output(process_output),
        "worker_status": safe_status, "runtime_identity": runtime_identity,
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


def _stage_runtime(manifest: dict) -> Path:
    release = ROOT / "runtime" / manifest["source_sha"]
    if _runtime_directory_present(release, "worker_runtime_release_path_symlink_invalid"):
        _verify_staged_runtime(release, manifest)
        return release
    staged_release = _runtime_staging_base() / manifest["source_sha"]
    if _runtime_directory_present(staged_release, "worker_runtime_release_path_symlink_invalid"):
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
    _copy_worker_assets(stage, manifest)
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
    if _runtime_directory_present(release, "worker_runtime_release_path_symlink_invalid"):
        _verify_staged_runtime(release, manifest)
        return release
    if not _runtime_directory_present(staged_release, "worker_runtime_release_path_symlink_invalid"):
        raise RuntimeError("worker_staged_runtime_missing")
    _verify_staged_runtime(staged_release, manifest)
    os.replace(staged_release, release)
    _verify_staged_runtime(release, manifest)
    return release


def _runtime_directory_present(path: Path, symlink_error: str) -> bool:
    """Inspect a runtime path without following dangling links or other entries."""
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("worker_runtime_path_unavailable") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise RuntimeError(symlink_error)
    if not stat.S_ISDIR(entry.st_mode):
        raise RuntimeError("worker_runtime_path_invalid")
    return True


def _verify_staged_runtime(runtime: Path, manifest: dict) -> None:
    manifest_path = runtime / "bundle-manifest.json"
    python = runtime / ".venv/bin/python3.12"
    manifest_fd = None
    try:
        _verify_runtime_assets(runtime, manifest)
        manifest_stat = manifest_path.lstat()
        if (stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode)
                or manifest_stat.st_uid != os.getuid()
                or stat.S_IMODE(manifest_stat.st_mode) != 0o600):
            raise RuntimeError("worker_runtime_manifest_identity_invalid")
        manifest_fd = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened_manifest = os.fstat(manifest_fd)
        if ((opened_manifest.st_dev, opened_manifest.st_ino) !=
                (manifest_stat.st_dev, manifest_stat.st_ino)
                or opened_manifest.st_uid != os.getuid()
                or stat.S_IMODE(opened_manifest.st_mode) != 0o600):
            raise RuntimeError("worker_runtime_manifest_identity_invalid")
        manifest_bytes = bytearray()
        while True:
            block = os.read(manifest_fd, 64 * 1024)
            if not block:
                break
            manifest_bytes.extend(block)
        recorded = json.loads(manifest_bytes)
        if recorded != manifest or not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError
        subprocess.run([str(python), "-c", "import sys,httpx,certifi; assert sys.version_info >= (3,12)"],
                       check=True, capture_output=True, timeout=10,
                       env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
    except RuntimeError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError("staged_runtime_environment_invalid") from exc
    finally:
        if manifest_fd is not None:
            os.close(manifest_fd)


def _copy_worker_assets(stage: Path, manifest: dict) -> None:
    """Copy the exact source assets into a private, mode-verified runtime."""
    if set(manifest.get("assets", {})) != set(ASSETS):
        raise RuntimeError("worker_runtime_asset_manifest_invalid")
    try:
        stage_stat = stage.lstat()
    except OSError as exc:
        raise RuntimeError("worker_runtime_staging_directory_invalid") from exc
    if (stat.S_ISLNK(stage_stat.st_mode) or not stat.S_ISDIR(stage_stat.st_mode)
            or stage_stat.st_uid != os.getuid() or stat.S_IMODE(stage_stat.st_mode) != 0o700):
        raise RuntimeError("worker_runtime_staging_directory_invalid")

    for name in ASSETS:
        source = WORKER_DIR / name
        destination = stage / name
        expected_mode = 0o700 if name == "run_codex_worker.sh" else 0o600
        try:
            source_stat = source.lstat()
        except OSError as exc:
            raise RuntimeError("worker_runtime_asset_source_invalid") from exc
        if stat.S_ISLNK(source_stat.st_mode):
            raise RuntimeError("worker_runtime_asset_source_symlink_invalid")
        if (not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_uid != os.getuid()):
            raise RuntimeError("worker_runtime_asset_source_invalid")
        source_fd = destination_fd = None
        digest = hashlib.sha256()
        try:
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened_source = os.fstat(source_fd)
            if ((opened_source.st_dev, opened_source.st_ino) !=
                    (source_stat.st_dev, source_stat.st_ino)
                    or not stat.S_ISREG(opened_source.st_mode)
                    or opened_source.st_uid != os.getuid()):
                raise RuntimeError("worker_runtime_asset_source_invalid")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                expected_mode,
            )
            os.fchmod(destination_fd, expected_mode)
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
            if digest.hexdigest() != manifest["assets"][name]:
                raise RuntimeError("worker_runtime_asset_content_mismatch")
        except OSError as exc:
            code = ("worker_runtime_asset_source_symlink_invalid"
                    if exc.errno == errno.ELOOP else "worker_runtime_asset_copy_failed")
            raise RuntimeError(code) from exc
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)


def _verify_runtime_assets(runtime: Path, manifest: dict) -> None:
    """Fail closed on unsafe runtime directories, asset identity, modes, or bytes."""
    try:
        runtime_stat = runtime.lstat()
    except OSError as exc:
        raise RuntimeError("worker_runtime_directory_invalid") from exc
    if (stat.S_ISLNK(runtime_stat.st_mode) or not stat.S_ISDIR(runtime_stat.st_mode)
            or runtime_stat.st_uid != os.getuid()
            or stat.S_IMODE(runtime_stat.st_mode) != 0o700):
        raise RuntimeError("worker_runtime_owner_invalid")
    if set(manifest.get("assets", {})) != set(ASSETS):
        raise RuntimeError("worker_runtime_asset_manifest_invalid")
    for name in ASSETS:
        path = runtime / name
        expected_mode = 0o700 if name == "run_codex_worker.sh" else 0o600
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise RuntimeError("worker_runtime_asset_missing") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise RuntimeError("worker_runtime_asset_symlink_invalid")
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.getuid():
            raise RuntimeError("worker_runtime_owner_invalid")
        if stat.S_IMODE(path_stat.st_mode) != expected_mode:
            raise RuntimeError("worker_runtime_asset_permissions_invalid")
        fd = None
        digest = hashlib.sha256()
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(fd)
            if ((opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()):
                raise RuntimeError("worker_runtime_owner_invalid")
            if stat.S_IMODE(opened.st_mode) != expected_mode:
                raise RuntimeError("worker_runtime_asset_permissions_invalid")
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        except OSError as exc:
            raise RuntimeError("worker_runtime_asset_unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if digest.hexdigest() != manifest["assets"][name]:
            raise RuntimeError("worker_runtime_asset_content_mismatch")


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


def _ensure_runtime_directories() -> None:
    """Create only the private Eros state directories required by startup validation."""
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = (ROOT, *(ROOT / name for name in ("config", "state", "locks", "receipts", "status", "logs")))
    for path in paths:
        if path != ROOT:
            path.mkdir(exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise RuntimeError("worker_runtime_directory_owner_invalid")
        path.chmod(0o700)


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


def _restore_legacy_snapshot(backup: Path, *, destination: Path = LEGACY) -> None:
    previous = backup.with_name(".legacy-runtime-previous")
    if backup.is_symlink() or not backup.is_dir():
        if previous.is_symlink() or not previous.is_dir():
            raise RuntimeError("rollback_snapshot_missing")
        backup = previous
    if destination.exists() and _same_tree(destination, backup):
        return
    _safe_remove_tree(destination)
    shutil.copytree(backup, destination, symlinks=True)
    destination.chmod(0o700)
    if not _same_tree(destination, backup):
        raise RuntimeError("rollback_legacy_snapshot_mismatch")


def _prepare_legacy_snapshot(backup: Path, *, source_root: Path = LEGACY) -> Path:
    """Atomically refresh the rollback copy from the stopped live legacy runtime."""
    if not source_root.is_dir() or source_root.is_symlink():
        raise RuntimeError("legacy_runtime_missing")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.parent.chmod(0o700)
    previous = backup.with_name(".legacy-runtime-previous")
    stage = backup.with_name(f".legacy-runtime-staging-{os.getpid()}")
    _safe_remove_tree(stage)
    # Recover an interrupted two-rename swap before creating the next snapshot.
    if not backup.exists() and previous.exists():
        os.replace(previous, backup)
    shutil.copytree(source_root, stage, symlinks=True, copy_function=shutil.copy2)
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
    if not _same_tree(source_root, backup):
        _safe_remove_tree(backup)
        if previous.exists():
            os.replace(previous, backup)
        raise RuntimeError("rollback_snapshot_refresh_mismatch")
    _safe_remove_tree(previous)
    return backup


def _health_check(deadline: float = 60.0) -> dict:
    """Wait for the sole Eros Worker and its fresh GPOINT queue snapshot."""
    python = ROOT / "runtime/current/.venv/bin/python3.12"
    worker = ROOT / "runtime/current/gpoint_mac_worker.py"
    until = time.monotonic() + deadline
    last = {}
    while time.monotonic() < until:
        if not launchctl_loaded(LABEL) or launchctl_loaded(OLD_LABEL):
            time.sleep(1)
            continue
        result = subprocess.run([str(python), str(worker), "status"], capture_output=True,
                                text=True, timeout=15, check=False)
        try:
            last = json.loads(result.stdout)
        except ValueError:
            last = {}
        try:
            validate_fresh_production_queue_snapshots(
                {**last, "launch_agent_loaded": True}, lanes=("GPOINT",),
            )
            lane = last.get("lanes", {}).get("GPOINT", {})
            if (result.returncode == 0 and lane.get("health") == "HEALTHY"
                    and lane.get("queue_snapshot_state") == "VERIFIED"):
                return last
        except (PreflightFailure, RuntimeError, AttributeError):
            pass
        time.sleep(1)
    raise RuntimeError("new_worker_health_check_failed")


def _wait_for_successful_polls(baseline: dict, deadline: float = 90.0) -> dict:
    status_path = ROOT / "status/worker-status.json"
    old_poll = baseline.get("last_poll_at")
    until = time.monotonic() + deadline
    latest = {}
    while time.monotonic() < until:
        latest = safe_worker_status(status_path)
        new_poll = latest.get("last_poll_at")
        try:
            progressed = bool(new_poll and (not old_poll or datetime.fromisoformat(
                str(new_poll).replace("Z", "+00:00")) > datetime.fromisoformat(
                str(old_poll).replace("Z", "+00:00"))))
        except (ValueError, TypeError):
            progressed = False
        lane = latest.get("targets", {}).get("GPOINT", {}) if isinstance(latest.get("targets"), dict) else {}
        no_error = latest.get("last_error_code") in (None, "") and lane.get("last_error_code") in (None, "")
        if progressed and no_error and latest.get("current_job_present") is False:
            return {"passed": True, "last_poll_at": new_poll,
                    "gpoint_poll_advanced": True, "last_error_code": None}
        time.sleep(min(1.0, max(0.0, until - time.monotonic())))
    return {"passed": False, "last_poll_at": latest.get("last_poll_at"),
            "gpoint_poll_advanced": False, "last_error_code": latest.get("last_error_code")}

def _network_state_sha256() -> str:
    observations = []
    for command in (("/usr/sbin/scutil", "--nwi"), ("/sbin/route", "-n", "get", "default")):
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=True)
        observations.append(result.stdout)
    return hashlib.sha256("\n".join(observations).encode()).hexdigest()



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
        status = _read_production_queue_status(
            candidate_manifest, retry_budget=retry_budget, lane_scope=("GPOINT",),
        )
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
            "gpoint_health_healthy": status.get("lanes", {}).get("GPOINT", {}).get("health") == "HEALTHY",
            "gpoint_state": status.get("lanes", {}).get("GPOINT", {}).get("health"),
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
            "worker_slots_busy": "local_worker_not_idle",
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
    for lane in ("GPOINT",):
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
        "active_workers": evidence["active_workers"],
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
        preflight = _run_safety_preflight("APPLY_INITIAL", transition_lock_already_held=True)
        manifest, queue_evidence = preflight["manifest"], preflight["queue_evidence"]
        identity = preflight["inventory"]
        source_label = identity["label"]
        source_root = Path(identity["active_runtime_root"])
        source_plist = Path(identity["active_plist"])
        if ROOT.exists() and (ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid()):
            raise RuntimeError("worker_runtime_root_owner_invalid")
        _ensure_runtime_directories()
        rollback_tree = _ensure_rollback_tree(manifest["source_sha"])
        staged = _stage_runtime(manifest)
        startup_check = _validate_candidate_startup(staged, manifest)
        _save_original_launchagent(rollback_tree)
        original_plist = source_plist.read_bytes()
        identity_path = rollback_tree / "source-identity.json"
        source_identity = json.loads(identity_path.read_text(encoding="utf-8"))
        backup = rollback_tree / "legacy-runtime"
        diagnostics = _create_diagnostic_attempt(manifest["source_sha"], uuid.uuid4().hex)
        attempt_started = datetime.now(UTC).isoformat()
        target_link = ROOT / "runtime/current"
        marker = ROOT / "state/maintenance-stop"
        marker.touch(mode=0o600, exist_ok=True)
        marker.chmod(0o600)
        paused_pid = _pause_worker_or_clear_marker(source_label, marker)
        if paused_pid is None:
            marker.unlink(missing_ok=True)
            raise RuntimeError("worker_launchagent_unloaded_during_transition")
        switched = False
        stopped_snapshot_verified = False
        restart_evidence = None
        try:
            paused = _run_safety_preflight("APPLY_PAUSED_PRE_BOOTOUT", manifest=manifest,
                                           transition_lock_already_held=True)
            queue_evidence = paused["queue_evidence"]
            # Only after the paused GPOINT queue and generation checks pass do
            # we unload the old label. Never bootstrap Eros until it is absent.
            _bootout_and_wait(source_label)
            try:
                os.kill(paused_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            if launchctl_loaded(source_label) or _running_inference_pids():
                raise RuntimeError("worker_process_did_not_stop")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _running_inference_pids():
                time.sleep(0.5)
            if _running_inference_pids():
                raise RuntimeError("active_inference_blocks_transition")
            backup = _prepare_legacy_snapshot(backup, source_root=source_root)
            stopped_snapshot_verified = True
            release = _install_staged_runtime(staged, manifest)
            for name in ("runtime", "config", "state", "locks", "receipts", "status", "logs"):
                directory = ROOT / name
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if directory.is_symlink() or directory.stat().st_uid != os.getuid():
                    raise RuntimeError("worker_runtime_directory_owner_invalid")
                directory.chmod(0o700)
            next_link = ROOT / "runtime/.current-next"
            _safe_remove_tree(next_link)
            next_link.symlink_to(release.name)
            os.replace(next_link, target_link)
            next_plist = _render_plist(target_link, diagnostics)
            temp_plist = PLIST.with_suffix(".plist.new")
            temp_plist.write_bytes(next_plist)
            temp_plist.chmod(0o600)
            os.replace(temp_plist, PLIST)
            marker.unlink(missing_ok=True)
            _bootstrap_candidate_only(source_label, PLIST)
            switched = True
            restart_evidence = _kickstart_and_verify(manifest["source_sha"])
            if restart_evidence.get("verification") != "PASS":
                raise RuntimeError("restart_verification_failed")
            status = restart_evidence["adjudication"].get("status", {})
            if set(status.get("lanes", {})) != {"GPOINT"}:
                raise RuntimeError("worker_status_lane_scope_mismatch")
            if not status.get("checks", {}).get("exactly_one_launchagent", True):
                raise RuntimeError("multiple_worker_launchagents_loaded")
            poll_baseline = safe_worker_status(ROOT / "status/worker-status.json")
            poll = _wait_for_successful_polls(poll_baseline)
            if not poll.get("passed"):
                raise RuntimeError("successful_worker_polls_not_verified")
            health = _health_check()
            if set(health.get("lanes", {})) != {"GPOINT"}:
                raise RuntimeError("worker_status_lane_scope_mismatch")
            # Keep the old tree only in the rollback snapshot. It has no plist
            # and cannot be launched after successful handover.
            if source_root != ROOT:
                _safe_remove_tree(source_root)
            if source_plist != PLIST:
                source_plist.unlink(missing_ok=True)
            receipt = {
                "state": "COMPLETE", "at": datetime.now(UTC).isoformat(),
                "label": LABEL, "source_sha": manifest["source_sha"],
                "bundle_sha256": manifest["bundle_sha256"],
                "worker_process_count": 1, "launchagent_count": 1,
                "queue_preflight": queue_evidence, "startup_check": startup_check,
                "health_check": health, "restart_check": restart_evidence,
                "fresh_poll_check": poll, "rollback_snapshot": str(backup),
                "source_identity": source_identity,
                "old_runtime_retained_only_for_rollback": True,
                "old_plist_removed": source_plist != PLIST,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            _atomic_json(ROOT / "receipts/runtime-migration.json", receipt)
            return receipt
        except Exception as failure:
            marker.unlink(missing_ok=True)
            if launchctl_loaded(source_label) and not switched:
                if source_label != LABEL and launchctl_loaded(LABEL):
                    try:
                        _restore_single_launchagent(source_label, source_plist, original_plist)
                    except Exception as rollback_failure:
                        _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", {
                            "state": "ROLLBACK_INCOMPLETE", "at": datetime.now(UTC).isoformat(),
                            "source_sha": manifest["source_sha"], "source_label": source_label,
                            "launchagent_restore": {"restored_label": None, "launchagent_loaded": False},
                            "rollback_error_code": _safe_error_code(rollback_failure),
                            "rollback_snapshot": str(backup), "failure_code": _safe_error_code(failure),
                            "restart_evidence": restart_evidence,
                        })
                        raise
                # The source service is still registered. Resume that one
                # worker in place; do not replace its live queue/state tree
                # with the earlier prepare snapshot.
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                raise
            try:
                # Make the candidate absent first. If that cannot be proven,
                # do not bootstrap the source agent; zero agents is safer than two.
                _bootout_and_wait(LABEL)
                if launchctl_loaded(LABEL):
                    raise RuntimeError("rollback_candidate_launchagent_still_loaded")
                if stopped_snapshot_verified:
                    _restore_legacy_snapshot(backup, destination=source_root)
                restored = _restore_single_launchagent(source_label, source_plist, original_plist)
                rollback_state = "ROLLED_BACK"
                rollback_error = None
            except Exception as rollback_failure:
                restored = {"restored_label": None, "launchagent_loaded": False}
                rollback_state = "ROLLBACK_INCOMPLETE"
                rollback_error = _safe_error_code(rollback_failure)
                # Explicit final guard: if both labels somehow appear, stop
                # the new Eros service before recording the failed recovery.
                if launchctl_loaded(OLD_LABEL) and launchctl_loaded(LABEL):
                    try:
                        _bootout_and_wait(LABEL)
                    except Exception:
                        try:
                            _bootout_and_wait(source_label)
                        except Exception:
                            pass
            _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", {
                "state": rollback_state, "at": datetime.now(UTC).isoformat(),
                "source_sha": manifest["source_sha"], "source_label": source_label,
                "launchagent_restore": restored, "rollback_error_code": rollback_error,
                "rollback_snapshot": str(backup), "failure_code": _safe_error_code(failure),
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
                      and (entry / "original-launchagent.plist").is_file()
                      and (entry / "source-identity.json").is_file()]
    if not candidates:
        raise RuntimeError("rollback_snapshot_missing")
    snapshot_root = max(candidates, key=lambda entry: entry.stat().st_mtime)
    backup = snapshot_root / "legacy-runtime"
    original_plist_file = snapshot_root / "original-launchagent.plist"
    identity_file = snapshot_root / "source-identity.json"
    if backup.is_symlink() or not backup.is_dir() or not original_plist_file.is_file():
        raise RuntimeError("rollback_snapshot_missing")
    identity = json.loads(identity_file.read_text(encoding="utf-8"))
    source_label = identity.get("label")
    source_root = Path(identity.get("runtime_root", ""))
    source_plist = Path(identity.get("plist_path", ""))
    if source_label not in {OLD_LABEL, LABEL} or source_root not in {OLD_ROOT, ROOT} \
            or source_plist not in {OLD_PLIST, PLIST}:
        raise RuntimeError("rollback_source_identity_invalid")
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
        active_label, _active_root, _active_plist = _active_worker_identity()
        paused_pid = _pause_idle_launchagent(active_label)
        if paused_pid is not None:
            try:
                _bootout_and_wait(active_label)
            finally:
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        _wait_launchagent_unloaded(paused_pid, label=active_label)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _running_inference_pids():
            time.sleep(1)
        if _running_inference_pids():
            raise RuntimeError("worker_process_did_not_stop")
        _restore_legacy_snapshot(backup, destination=source_root)
        original_plist = original_plist_file.read_bytes()
        (ROOT / "state/maintenance-stop").unlink(missing_ok=True)
        restored = _restore_single_launchagent(source_label, source_plist, original_plist)
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{source_label}"], check=True, timeout=15)
        if not source_plist.is_file() or source_plist.read_bytes() != original_plist:
            raise RuntimeError("rollback_launchagent_plist_mismatch")
        if not launchctl_loaded(source_label) or (source_label != LABEL and launchctl_loaded(LABEL)):
            raise RuntimeError("rollback_multiple_launchagents_loaded")
        evidence = {
            "state": "ROLLED_BACK", "at": datetime.now(UTC).isoformat(), "label": source_label,
            "legacy_runtime_restored": source_root.exists(), "launchagent_loaded": True,
            "restored_plist_sha256": hashlib.sha256(original_plist).hexdigest(),
            "rollback_snapshot": str(backup), "launchagent_restore": restored,
        }
        _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", evidence)
        return evidence
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "preflight", "prepare", "apply", "rollback"))
    args = parser.parse_args()
    result = (read_only_plan() if args.action == "plan" else read_only_preflight() if args.action == "preflight"
              else prepare() if args.action == "prepare"
              else rollback() if args.action == "rollback" else apply())
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
