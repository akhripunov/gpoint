#!/usr/bin/env python3
"""One-shot guarded Real Codex QA; restores the original Mac LaunchAgent in finally."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PREPARATION_BASE_SHA = "50034a22ff73b15b6093bf8edf6d3de7a810b8a5"
EXPECTED_PRODUCTION_SHA = "68bf0212d7aa5125394b0b39ffba9226eb106c0d"
EXPECTED_GPOINT_TENANT_ID = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
EXPECTED_API_IMAGE_ID = "sha256:940236e984cff5e0123af62c58cea6ae5986fe879ca219daa2c91ff6ca8620e2"
EXPECTED_AGATA_IMAGE_ID = "sha256:61299c506ed77971df0f75cb64cb91f739644ebbddfafc21d5cb903c4488dbb2"
LABEL = "one.gpoint.agata-codex-worker"
MAX_FULL_OUTAGE_SECONDS = 300
RECOVERY_START_AFTER_SECONDS = 220
WATCHDOG_LOCK_BUSY_FORCE_SECONDS = 20
WATCHDOG_POLL_SECONDS = 1
MAX_BOOTOUT_SECONDS = 15
MAX_PROCESS_TERMINATION_SECONDS = 10
MAX_RESTORE_SECONDS = 60  # Includes bootstrap (≤15s) and poll confirmation (≤45s).
RESTORE_POLL_CONFIRM_SECONDS = 45
TTL_SAFETY_MARGIN_SECONDS = 120
MAX_POLL_AGE_SECONDS = 15  # Installed worker's configured idle poll interval is 10s.
QUEUE_SNAPSHOT_MAX_AGE_SECONDS = 5
MAX_SNAPSHOT_COMMAND_SECONDS = 45
MINIMUM_TTL_SECONDS = MAX_FULL_OUTAGE_SECONDS + TTL_SAFETY_MARGIN_SECONDS
HOME_DIR = Path.home()
REPO = Path(__file__).resolve().parents[1]
VPS_RUNNER = HOME_DIR / ".local/bin/gpoint-vps-runner"
PLIST = HOME_DIR / "Library/LaunchAgents" / f"{LABEL}.plist"
SUPPORT = HOME_DIR / "Library/Application Support/GPoint/agata-codex-worker"
RECOVERY_ROOT = HOME_DIR / "Library/Application Support/GPoint/gsales-qa-recovery"
WORKER = SUPPORT / "gpoint_mac_worker.py"
WRAPPER = SUPPORT / "run_codex_worker.sh"
PYTHON = SUPPORT / ".venv/bin/python3.12"
SAFE_RECOVERY_ERRORS = {
    "authoritative_queue_snapshot_failed",
    "authoritative_queue_snapshot_invalid",
    "authoritative_queue_snapshot_runner_unavailable",
    "authoritative_queue_snapshot_too_slow",
    "authoritative_queue_snapshot_unavailable",
    "candidate_sha_mismatch",
    "effective_inference_ttl_below_outage_budget",
    "gpoint_queue_not_empty",
    "installed_worker_artifact_missing",
    "installed_worker_hash_changed",
    "insufficient_test_budget",
    "launchagent_not_loaded",
    "launchagent_not_loaded_at_final_preflight",
    "launchagent_restore_failed",
    "original_launchagent_contract_unexpected",
    "queue_snapshot_not_gpoint",
    "queue_snapshot_runtime_identity_mismatch",
    "queue_snapshot_source_identity_mismatch",
    "queue_snapshot_stale_before_switch",
    "queue_snapshot_trust_boundary_not_proven",
    "recovery_directory_permissions_invalid",
    "recovery_lock_permissions_invalid",
    "recovery_source_invalid",
    "recovery_state_invalid",
    "recovery_state_permissions_invalid",
    "recovery_target_identity_invalid",
    "recovery_watchdog_exit_not_confirmed",
    "recovery_watchdog_failed",
    "recovery_watchdog_not_ready",
    "recovery_watchdog_start_failed",
    "watchdog_deadline_elapsed_before_switch",
    "worker_authentication_failed",
    "worker_not_idle",
    "worker_poll_status_stale",
    "worker_polling_did_not_resume_before_deadline",
    "worker_restore_deadline_exceeded",
    "worker_status_invalid",
}
QUEUE_SNAPSHOT_SCRIPT = REPO / "scripts/gpoint-agata-readonly-queue-snapshot.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def loaded() -> bool:
    try:
        p = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0


def status() -> dict:
    try:
        return json.loads((SUPPORT / "worker-status.json").read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("worker_status_invalid") from exc


def remaining_test_budget_seconds(*, outage_deadline: float, now_monotonic: float | None = None) -> float:
    """Keep process termination and confirmed worker restore inside the outage cap."""
    now = time.monotonic() if now_monotonic is None else now_monotonic
    return max(0.0, outage_deadline - now - MAX_PROCESS_TERMINATION_SECONDS - MAX_RESTORE_SECONDS)


def require_test_budget(*, outage_deadline: float, now_monotonic: float | None = None) -> float:
    remaining = remaining_test_budget_seconds(outage_deadline=outage_deadline, now_monotonic=now_monotonic)
    if remaining <= 0:
        raise RuntimeError("insufficient_test_budget")
    return remaining


def authenticated_poll_confirmed(current: dict, before: dict) -> bool:
    return (
        current.get("last_poll_at") != before.get("last_poll_at")
        and current.get("last_poll_at") is not None
        and current.get("state") == "idle"
        and current.get("current_job_present") is False
        and current.get("last_error_code") is None
    )


def _state_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def _write_recovery_state(state_path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".qa-recovery-", dir=str(state_path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
        directory_fd = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_recovery_state(state_path: Path) -> dict:
    file_stat = state_path.lstat()
    if state_path.is_symlink() or file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o777 != 0o600:
        raise RuntimeError("recovery_state_permissions_invalid")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError("recovery_state_invalid")
    return state


def create_recovery_session(
    root: Path,
    original_hashes: dict[str, str],
    *,
    parent_pid: int,
    now: float | None = None,
) -> Path:
    if root.is_symlink():
        raise RuntimeError("recovery_directory_permissions_invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    root_stat = root.stat()
    if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o777 != 0o700:
        raise RuntimeError("recovery_directory_permissions_invalid")
    started = time.monotonic() if now is None else now
    state_path = root / f"{uuid.uuid4().hex}.json"
    lock_fd = os.open(_state_lock_path(state_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(lock_fd)
    _write_recovery_state(state_path, {
        "version": 1,
        "run_id": state_path.stem,
        "parent_pid": parent_pid,
        "started_monotonic": started,
        "restore_after_monotonic": started + RECOVERY_START_AFTER_SECONDS,
        "outage_deadline_monotonic": started + MAX_FULL_OUTAGE_SECONDS,
        "original_hashes": original_hashes,
        "bootout_attempted": False,
        "qa_pid": None,
        "watchdog_pid": None,
        "restore_confirmed": False,
        "restore_source": None,
        "last_recovery_error": None,
        "cancelled": False,
    })
    return state_path


def _locked_state(state_path: Path, *, timeout: float | None = None):
    lock_path = _state_lock_path(state_path)
    lock_stat = lock_path.lstat()
    if lock_path.is_symlink() or lock_stat.st_uid != os.getuid() or lock_stat.st_mode & 0o777 != 0o600:
        raise RuntimeError("recovery_lock_permissions_invalid")
    lock = lock_path.open("rb")
    if timeout is None:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return lock
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            if time.monotonic() >= deadline:
                lock.close()
                raise TimeoutError("recovery_lock_busy") from None
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def _safe_recovery_error(exc: BaseException) -> str:
    value = str(exc)
    if value in SAFE_RECOVERY_ERRORS:
        return value
    if value.startswith("authoritative_queue_snapshot_blocked:"):
        code = value.partition(":")[2]
        if code in {
            "gpoint_api_agata_release_mismatch",
            "queue_snapshot_database_role_is_privileged",
            "queue_snapshot_gpoint_tenant_not_visible",
            "queue_snapshot_transaction_not_read_only_same_login",
            "readonly_snapshot_blocked",
            "runtime_database_is_not_gpoint",
            "runtime_gpoint_tenant_identity_mismatch",
            "runtime_gpoint_tenant_not_registered",
            "runtime_image_identity_invalid",
            "runtime_is_not_gpoint_tenant",
            "runtime_release_identity_invalid",
        }:
            return value
    return type(exc).__name__.lower()


def _record_qa_pid(state_path: Path, pid: int | None) -> None:
    lock = _locked_state(state_path)
    try:
        state = read_recovery_state(state_path)
        state["qa_pid"] = pid
        _write_recovery_state(state_path, state)
    finally:
        lock.close()


def _record_bootout_attempt(state_path: Path) -> None:
    lock = _locked_state(state_path)
    try:
        state = read_recovery_state(state_path)
        state["bootout_attempted"] = True
        _write_recovery_state(state_path, state)
    finally:
        lock.close()


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_controller_process(state: dict) -> None:
    pid = state.get("parent_pid")
    if type(pid) is not int or pid <= 1 or not process_exists(pid):
        return
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        command = result.stdout
        if result.returncode != 0 or "run-gsales-one-shot-real-codex.py" not in command or "--candidate" not in command:
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and process_exists(pid):
            time.sleep(0.05)
        if process_exists(pid):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def terminate_recorded_qa_process(state: dict) -> None:
    pid = state.get("qa_pid")
    if type(pid) is not int or pid <= 1:
        return
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pgid=,command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if listing.returncode != 0:
            return
        matching_group = []
        for line in listing.stdout.splitlines():
            columns = line.strip().split(None, 1)
            if len(columns) == 2 and columns[0].isdigit() and int(columns[0]) == pid:
                matching_group.append(columns[1])
        if not matching_group or not any("run_postgres.py" in command and "--real-codex" in command for command in matching_group):
            return
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except (ProcessLookupError, subprocess.TimeoutExpired):
        return


def restore_once(
    state_path: Path,
    *,
    source: str,
    restore_fn=None,
    lock_timeout: float | None = None,
) -> bool:
    restore_fn = restore if restore_fn is None else restore_fn
    lock = _locked_state(state_path, timeout=lock_timeout)
    try:
        state = read_recovery_state(state_path)
        if state.get("restore_confirmed") or state.get("cancelled"):
            return False
        if source not in {"controller", "watchdog"}:
            raise RuntimeError("recovery_source_invalid")
        if restore_fn is restore:
            expected_paths = {str(PLIST), str(WORKER), str(WRAPPER)}
            hashes = state.get("original_hashes")
            if (
                not isinstance(hashes, dict)
                or set(hashes) != expected_paths
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(ch not in "0123456789abcdef" for ch in value)
                    for value in hashes.values()
                )
            ):
                raise RuntimeError("recovery_target_identity_invalid")
        try:
            restore_fn(
                state["original_hashes"],
                True,
                outage_deadline=state["outage_deadline_monotonic"],
            )
        except BaseException as exc:
            state["last_recovery_error"] = _safe_recovery_error(exc)
            _write_recovery_state(state_path, state)
            raise
        state["restore_confirmed"] = True
        state["restore_source"] = source
        state["restored_at_monotonic"] = time.monotonic()
        state["last_recovery_error"] = None
        _write_recovery_state(state_path, state)
        return True
    finally:
        lock.close()


def _cancel_recovery_session(state_path: Path) -> None:
    lock = _locked_state(state_path)
    try:
        state = read_recovery_state(state_path)
        if not state.get("bootout_attempted") and not state.get("restore_confirmed"):
            state["cancelled"] = True
            _write_recovery_state(state_path, state)
    finally:
        lock.close()


def run_recovery_watchdog(
    state_path: Path,
    *,
    now_fn=time.monotonic,
    sleep_fn=time.sleep,
    parent_exists_fn=process_exists,
    restore_fn=None,
    terminate_qa_fn=terminate_recorded_qa_process,
    terminate_controller_fn=None,
    restore_once_fn=None,
) -> int:
    terminate_controller_fn = terminate_controller_process if terminate_controller_fn is None else terminate_controller_fn
    restore_once_fn = restore_once if restore_once_fn is None else restore_once_fn
    own_lock = _locked_state(state_path)
    try:
        state = read_recovery_state(state_path)
        state["watchdog_pid"] = os.getpid()
        _write_recovery_state(state_path, state)
    finally:
        own_lock.close()
    attempted = False
    lock_busy_since = None

    def force_controller_if_lock_busy() -> None:
        nonlocal lock_busy_since
        if lock_busy_since is None:
            lock_busy_since = now_fn()
        elif now_fn() - lock_busy_since >= WATCHDOG_LOCK_BUSY_FORCE_SECONDS:
            terminate_controller_fn(read_recovery_state(state_path))

    while True:
        state = read_recovery_state(state_path)
        if state.get("restore_confirmed") or state.get("cancelled"):
            return 0
        now = now_fn()
        parent_alive = parent_exists_fn(state["parent_pid"])
        triggered = not parent_alive or now >= state["restore_after_monotonic"]
        if triggered:
            try:
                terminate_qa_fn(state)
                restore_once_fn(state_path, source="watchdog", restore_fn=restore_fn, lock_timeout=0.5)
                if read_recovery_state(state_path).get("restore_confirmed"):
                    return 0
            except TimeoutError:
                attempted = True
                force_controller_if_lock_busy()
                sleep_fn(min(0.1, max(0.01, state["outage_deadline_monotonic"] - now_fn())))
                continue
            except BaseException as exc:
                try:
                    lock = _locked_state(state_path, timeout=0.5)
                    try:
                        latest = read_recovery_state(state_path)
                        latest["last_recovery_error"] = _safe_recovery_error(exc)
                        _write_recovery_state(state_path, latest)
                    finally:
                        lock.close()
                except TimeoutError:
                    force_controller_if_lock_busy()
                attempted = True
        latest = read_recovery_state(state_path)
        if now_fn() >= latest["outage_deadline_monotonic"]:
            return 1 if attempted or not latest.get("restore_confirmed") else 0
        sleep_fn(min(WATCHDOG_POLL_SECONDS, max(0.01, latest["outage_deadline_monotonic"] - now_fn())))


def start_recovery_watchdog(state_path: Path) -> subprocess.Popen:
    safe_env = {
        "HOME": str(HOME_DIR),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "USER": os.environ.get("USER", ""),
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--watchdog-state", str(state_path)],
        cwd=REPO,
        env=safe_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = read_recovery_state(state_path)
        if state.get("watchdog_pid") == process.pid:
            return process
        if process.poll() is not None:
            raise RuntimeError("recovery_watchdog_start_failed")
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=2)
    raise RuntimeError("recovery_watchdog_not_ready")


def require_watchdog_success(process: subprocess.Popen) -> None:
    try:
        code = process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise RuntimeError("recovery_watchdog_exit_not_confirmed") from None
    if code != 0:
        raise RuntimeError("recovery_watchdog_failed")


def install_controller_signal_handlers() -> None:
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)


def run_guarded_window(
    *,
    bootout_fn,
    launch_qa_fn,
    restore_fn,
    outage_deadline: float,
    now_fn=time.monotonic,
    record_qa_fn=None,
) -> int:
    remaining = require_test_budget(outage_deadline=outage_deadline, now_monotonic=now_fn())
    process = None
    termination_attempted = False
    try:
        bootout_fn()
        remaining = require_test_budget(outage_deadline=outage_deadline, now_monotonic=now_fn())
        process = launch_qa_fn()
        if record_qa_fn is not None:
            record_qa_fn(process.pid)
        try:
            return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            termination_attempted = True
            terminate_process_tree(process)
            return 124
    finally:
        try:
            if process is not None and process.poll() is None and not termination_attempted:
                termination_attempted = True
                terminate_process_tree(process)
        finally:
            try:
                if process is not None and process.poll() is not None and record_qa_fn is not None:
                    record_qa_fn(None)
            finally:
                restore_fn()


def remote_snapshot_command() -> str:
    script = base64.b64encode(QUEUE_SNAPSHOT_SCRIPT.read_bytes()).decode("ascii")
    return f"""set -eu
env_file=/opt/gpoint/env/production.env
release_sha=$(sed -n 's/^RELEASE_SHA=//p' "$env_file" | head -1)
case "$release_sha" in *[!0-9a-f]*|'') echo runtime_release_sha_invalid >&2; exit 42 ;; esac
test "${{#release_sha}}" -eq 40
api=compose-api-1
agata=compose-agata-api-1
api_image_id=$(docker inspect "$api" --format '{{{{.Image}}}}')
api_running=$(docker inspect "$api" --format '{{{{.State.Running}}}}')
api_health=$(docker inspect "$api" --format '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}')
api_sha=$(docker image inspect "$api_image_id" --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}')
agata_image_id=$(docker inspect "$agata" --format '{{{{.Image}}}}')
agata_running=$(docker inspect "$agata" --format '{{{{.State.Running}}}}')
agata_health=$(docker inspect "$agata" --format '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}')
agata_sha=$(docker image inspect "$agata_image_id" --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}')
test "$api_running" = true && test "$api_health" = healthy
test "$agata_running" = true && test "$agata_health" = healthy
test "$api_sha" = "$release_sha" && test "$agata_sha" = "$release_sha"
source_id="gpoint-vps:${{release_sha}}:api=${{api_image_id}}:agata=${{agata_image_id}}"
printf '%s' '{script}' | base64 -d | docker exec -i -w /srv/agata \\
  -e SNAPSHOT_SOURCE_ID="$source_id" \\
  -e SNAPSHOT_RELEASE_SHA="$release_sha" \\
  -e SNAPSHOT_API_BUILD_SHA="$api_sha" \\
  -e SNAPSHOT_AGATA_BUILD_SHA="$agata_sha" \\
  -e SNAPSHOT_API_IMAGE_ID="$api_image_id" \\
  -e SNAPSHOT_AGATA_IMAGE_ID="$agata_image_id" \\
  "$agata" python -"""


def queue_snapshot() -> dict:
    """Read fresh queue facts from the live, identity-checked GPOINT runtime."""
    if not VPS_RUNNER.is_file() or not os.access(VPS_RUNNER, os.X_OK):
        raise RuntimeError("authoritative_queue_snapshot_runner_unavailable")
    env = dict(os.environ)
    env["REMOTE_CMD"] = remote_snapshot_command()
    env["GPOINT_REMOTE_COMMAND_TIMEOUT_SECONDS"] = "30"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(VPS_RUNNER)], capture_output=True, text=True,
            timeout=MAX_SNAPSHOT_COMMAND_SECONDS, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("authoritative_queue_snapshot_unavailable") from exc
    if result.returncode != 0:
        # SSH stderr can contain operational detail; fail closed without copying
        # remote diagnostics (or any environment data) into local logs.
        raise RuntimeError("authoritative_queue_snapshot_failed")
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        snapshot = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("authoritative_queue_snapshot_invalid") from exc
    safe_errors = {
        "runtime_is_not_gpoint_tenant",
        "runtime_gpoint_tenant_not_registered",
        "runtime_gpoint_tenant_identity_mismatch",
        "runtime_release_identity_invalid",
        "gpoint_api_agata_release_mismatch",
        "runtime_image_identity_invalid",
        "runtime_database_is_not_gpoint",
        "queue_snapshot_transaction_not_read_only_same_login",
        "queue_snapshot_database_role_is_privileged",
        "queue_snapshot_gpoint_tenant_not_visible",
        "readonly_snapshot_blocked",
    }
    if isinstance(snapshot, dict) and snapshot.get("preflight_error") in safe_errors:
        raise RuntimeError(f"authoritative_queue_snapshot_blocked:{snapshot['preflight_error']}")
    snapshot["observed_monotonic"] = time.monotonic()
    snapshot["observed_local_at"] = datetime.now(timezone.utc).isoformat()
    if snapshot["observed_monotonic"] - started > QUEUE_SNAPSHOT_MAX_AGE_SECONDS:
        raise RuntimeError("authoritative_queue_snapshot_too_slow")
    return snapshot


def validate_queue_snapshot(snapshot: dict, *, now_monotonic: float | None = None) -> None:
    if not isinstance(snapshot, dict):
        raise RuntimeError("authoritative_queue_snapshot_invalid")
    release_sha = snapshot.get("release_sha")
    if (
        not isinstance(release_sha, str) or len(release_sha) != 40
        or any(character not in "0123456789abcdef" for character in release_sha)
        or release_sha != EXPECTED_PRODUCTION_SHA
        or snapshot.get("api_build_sha") != release_sha
        or snapshot.get("agata_build_sha") != release_sha
    ):
        raise RuntimeError("queue_snapshot_runtime_identity_mismatch")
    for field in ("api_image_id", "agata_image_id"):
        value = snapshot.get(field)
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
            raise RuntimeError("queue_snapshot_runtime_identity_mismatch")
    if (
        snapshot["api_image_id"] != EXPECTED_API_IMAGE_ID
        or snapshot["agata_image_id"] != EXPECTED_AGATA_IMAGE_ID
    ):
        raise RuntimeError("queue_snapshot_runtime_identity_mismatch")
    expected_source = (
        f"gpoint-vps:{release_sha}:api={snapshot['api_image_id']}"
        f":agata={snapshot['agata_image_id']}"
    )
    if snapshot.get("source_id") != expected_source:
        raise RuntimeError("queue_snapshot_source_identity_mismatch")
    if snapshot.get("source_system") != "gpoint" or snapshot.get("database_name") != "gpoint":
        raise RuntimeError("queue_snapshot_not_gpoint")
    if (
        snapshot.get("tenant_identity_verified") is not True
        or snapshot.get("transaction_read_only") is not True
        or snapshot.get("same_login") is not True
        or snapshot.get("role_superuser") is not False
        or snapshot.get("role_bypassrls") is not False
        or snapshot.get("visible_tenant_rows") != 1
        or snapshot.get("tenant_id") != EXPECTED_GPOINT_TENANT_ID
        or type(snapshot.get("active_gpoint_profiles")) is not int
        or snapshot.get("active_gpoint_profiles", 0) < 1
    ):
        raise RuntimeError("queue_snapshot_trust_boundary_not_proven")
    for field in ("pending_inference_jobs", "running_inference_jobs"):
        if type(snapshot.get(field)) is not int:
            raise RuntimeError("authoritative_queue_snapshot_invalid")
        if snapshot[field] != 0:
            raise RuntimeError("gpoint_queue_not_empty")
    ttl = snapshot.get("effective_inference_ttl_seconds")
    if type(ttl) is not int or ttl < MINIMUM_TTL_SECONDS:
        raise RuntimeError("effective_inference_ttl_below_outage_budget")
    try:
        captured_at = datetime.fromisoformat(str(snapshot["snapshot_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("authoritative_queue_snapshot_invalid") from exc
    if captured_at.tzinfo is None:
        raise RuntimeError("authoritative_queue_snapshot_invalid")
    observed = snapshot.get("observed_monotonic")
    now = time.monotonic() if now_monotonic is None else now_monotonic
    age = now - observed if isinstance(observed, (int, float)) else float("inf")
    if age < 0 or age > QUEUE_SNAPSHOT_MAX_AGE_SECONDS:
        raise RuntimeError("queue_snapshot_stale_before_switch")


def assert_worker_idle() -> dict:
    snap = status()
    if not loaded():
        raise RuntimeError("launchagent_not_loaded")
    if snap.get("state") != "idle" or snap.get("current_job_present") is not False:
        raise RuntimeError("worker_not_idle")
    stamp = datetime.fromisoformat(str(snap.get("last_poll_at", "")).replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - stamp).total_seconds() > MAX_POLL_AGE_SECONDS:
        raise RuntimeError("worker_poll_status_stale")
    return snap


def immediate_switch_preflight() -> dict:
    """Refresh server queue facts, then recheck local worker activity."""
    snapshot = queue_snapshot()
    validate_queue_snapshot(snapshot)
    assert_worker_idle()
    return snapshot


def validate_candidate_sha(candidate_sha: str, head_sha: str) -> str:
    if (
        not isinstance(candidate_sha, str)
        or len(candidate_sha) != 40
        or any(character not in "0123456789abcdef" for character in candidate_sha)
        or candidate_sha != head_sha
    ):
        raise RuntimeError("candidate_sha_mismatch")
    return candidate_sha


def preflight(candidate_sha: str) -> tuple[dict[str, str], dict]:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    candidate_sha = validate_candidate_sha(candidate_sha, sha)
    clean = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", PREPARATION_BASE_SHA, candidate_sha], cwd=REPO
    )
    product_diff = subprocess.run(
        [
            "git", "diff", "--quiet", PREPARATION_BASE_SHA, candidate_sha, "--",
            "apps/api/src", "services/agata/app",
            "services/gsales/app", "services/gsales/Dockerfile",
            "services/gsales/migrations",
        ],
        cwd=REPO,
    )
    if clean.returncode != 0 or clean.stdout.strip() or ancestor.returncode != 0 or product_diff.returncode != 0:
        raise RuntimeError("candidate_sha_mismatch")
    if not all(path.is_file() for path in (PLIST, WORKER, WRAPPER, PYTHON)):
        raise RuntimeError("installed_worker_artifact_missing")
    config = plistlib.loads(PLIST.read_bytes())
    if config.get("Label") != LABEL or config.get("RunAtLoad") is not True or config.get("KeepAlive") is not True:
        raise RuntimeError("original_launchagent_contract_unexpected")
    hashes = {str(path): digest(path) for path in (PLIST, WORKER, WRAPPER)}
    snapshot = immediate_switch_preflight()
    return hashes, snapshot


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


def restore(original_hashes: dict[str, str], stopped: bool, *, outage_deadline: float | None = None) -> None:
    deadline = outage_deadline if outage_deadline is not None else time.monotonic() + MAX_RESTORE_SECONDS
    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    for name, expected in original_hashes.items():
        if digest(Path(name)) != expected:
            raise RuntimeError("installed_worker_hash_changed")
    if stopped and not loaded():
        if remaining() <= 0:
            raise RuntimeError("worker_restore_deadline_exceeded")
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=min(MAX_BOOTOUT_SECONDS, remaining()))
    if not loaded():
        raise RuntimeError("launchagent_restore_failed")
    for name, expected in original_hashes.items():
        if digest(Path(name)) != expected:
            raise RuntimeError("installed_worker_hash_changed")
    confirm_deadline = min(deadline, time.monotonic() + RESTORE_POLL_CONFIRM_SECONDS)
    before_status = status()
    while time.monotonic() < confirm_deadline:
        current = status()
        if authenticated_poll_confirmed(current, before_status) and loaded():
            return
        time.sleep(min(2, max(0, confirm_deadline - time.monotonic())))
    raise RuntimeError("worker_polling_did_not_resume_before_deadline")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--watchdog-state", type=Path)
    args = parser.parse_args()
    if args.watchdog_state is not None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        return run_recovery_watchdog(args.watchdog_state)
    if not args.candidate:
        parser.error("--candidate is required unless --watchdog-state is set")
    # Convert termination signals into exceptions so cleanup runs.
    install_controller_signal_handlers()
    if args.preflight_only:
        try:
            _, snapshot = preflight(args.candidate)
        except Exception as exc:
            print(f"REAL_CODEX_PREFLIGHT=BLOCKED:{exc}")
            return 2
        print("REAL_CODEX_CANDIDATE=" + args.candidate)
        print("REAL_CODEX_QUEUE_SNAPSHOT=" + json.dumps({key: snapshot[key] for key in (
            "snapshot_at", "source_id", "release_sha", "pending_inference_jobs",
            "running_inference_jobs", "effective_inference_ttl_seconds", "tenant_id",
        )}, separators=(",", ":")))
        print("REAL_CODEX_PREFLIGHT=PASS")
        return 0

    # Environment, disposable PostgreSQL and synthetic E2E are prepared before
    # this command. The real QA path is disabled until the new candidate SHA is
    # explicitly approved by the OWNER.
    hashes, _ = preflight(args.candidate)  # Fail before bootout unless live queue is proven empty.
    state_path = None
    watchdog_process = None
    try:
        # Arm independent recovery before the last authoritative snapshot.
        outage_started = time.monotonic()
        outage_deadline = outage_started + MAX_FULL_OUTAGE_SECONDS
        require_test_budget(outage_deadline=outage_deadline)
        state_path = create_recovery_session(RECOVERY_ROOT, hashes, parent_pid=os.getpid(), now=outage_started)
        watchdog_process = start_recovery_watchdog(state_path)

        # Take the final server snapshot only after the watchdog is ready.
        final_snapshot = immediate_switch_preflight()
        validate_queue_snapshot(final_snapshot)
        assert_worker_idle()
        if read_recovery_state(state_path).get("restore_confirmed"):
            raise RuntimeError("watchdog_deadline_elapsed_before_switch")
        require_test_budget(outage_deadline=outage_deadline)
        if not loaded():
            raise RuntimeError("launchagent_not_loaded_at_final_preflight")

        def bootout() -> None:
            _record_bootout_attempt(state_path)
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                check=True,
                timeout=min(MAX_BOOTOUT_SECONDS, max(1, outage_deadline - time.monotonic())),
            )

        def launch_qa() -> subprocess.Popen:
            env = dict(os.environ, AGATA_CODEX_JOB_TIMEOUT_SECONDS="90")
            return subprocess.Popen(
                [str(REPO / "services/gsales/.venv/bin/python"),
                 str(REPO / "services/gsales/tests/run_postgres.py"), "--real-codex"],
                cwd=REPO,
                env=env,
                start_new_session=True,
            )

        def recover() -> None:
            restore_once(state_path, source="controller")
            print("GPOINT_WORKER_RESTORE=PASS")

        try:
            result_code = run_guarded_window(
                bootout_fn=bootout,
                launch_qa_fn=launch_qa,
                restore_fn=recover,
                outage_deadline=outage_deadline,
                record_qa_fn=lambda pid: _record_qa_pid(state_path, pid),
            )
        except BaseException:
            state = read_recovery_state(state_path)
            if state.get("restore_confirmed"):
                require_watchdog_success(watchdog_process)
                print("REAL_CODEX_TEST=INTERRUPTED;GPOINT_WORKER_RESTORE=PASS")
                return 130
            print("REAL_CODEX_TEST=ABORTED;GPOINT_RECOVERY_WATCHDOG=ACTIVE", file=sys.stderr)
            return 125
        require_watchdog_success(watchdog_process)
        print(f"REAL_CODEX_TEST_EXIT={result_code}")
        return result_code
    except BaseException as exc:
        if state_path is not None:
            state = read_recovery_state(state_path)
            if not state.get("bootout_attempted") and not state.get("restore_confirmed"):
                _cancel_recovery_session(state_path)
        code = _safe_recovery_error(exc)
        print("REAL_CODEX_CONTROLLER=FAILED:" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
