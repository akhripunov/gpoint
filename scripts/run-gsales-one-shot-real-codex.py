#!/usr/bin/env python3
"""One-shot guarded Real Codex QA; restores the original Mac LaunchAgent in finally."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_SHA = "50034a22ff73b15b6093bf8edf6d3de7a810b8a5"
LABEL = "one.gpoint.agata-codex-worker"
MAX_OUTAGE_SECONDS = 220  # Core's configured default inference TTL is 420s.
MAX_POLL_AGE_SECONDS = 15  # Installed worker's configured idle poll interval is 10s.
MINIMUM_TTL_SECONDS = 221
HOME_DIR = Path.home()
REPO = Path(__file__).resolve().parents[1]
PLIST = HOME_DIR / "Library/LaunchAgents" / f"{LABEL}.plist"
SUPPORT = HOME_DIR / "Library/Application Support/GPoint/agata-codex-worker"
WORKER = SUPPORT / "gpoint_mac_worker.py"
WRAPPER = SUPPORT / "run_codex_worker.sh"
PYTHON = SUPPORT / ".venv/bin/python3.12"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def loaded() -> bool:
    p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    return p.returncode == 0


def status() -> dict:
    try:
        return json.loads((SUPPORT / "worker-status.json").read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("worker_status_invalid") from exc


def preflight() -> dict:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", EXPECTED_SHA, sha], cwd=REPO
    )
    product_diff = subprocess.run(
        [
            "git", "diff", "--quiet", EXPECTED_SHA, sha, "--",
            "services/agata/app", "services/agata/worker/gpoint_mac_worker.py",
            "services/gsales/app", "services/gsales/Dockerfile",
            "services/gsales/migrations",
        ],
        cwd=REPO,
    )
    if ancestor.returncode != 0 or product_diff.returncode != 0:
        raise RuntimeError("candidate_sha_mismatch")
    if not all(path.is_file() for path in (PLIST, WORKER, WRAPPER, PYTHON)):
        raise RuntimeError("installed_worker_artifact_missing")
    config = plistlib.loads(PLIST.read_bytes())
    if config.get("Label") != LABEL or config.get("RunAtLoad") is not True or config.get("KeepAlive") is not True:
        raise RuntimeError("original_launchagent_contract_unexpected")
    hashes = {str(path): digest(path) for path in (PLIST, WORKER, WRAPPER)}
    snap = status()
    # launchctl is authoritative for registration; the worker's own compact
    # status file intentionally does not duplicate that launchd fact.
    if not loaded():
        raise RuntimeError("launchagent_not_loaded")
    if snap.get("state") != "idle" or snap.get("current_job_present") is not False:
        raise RuntimeError("worker_not_idle")
    stamp = datetime.fromisoformat(str(snap.get("last_poll_at", "")).replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - stamp).total_seconds() > MAX_POLL_AGE_SECONDS:
        raise RuntimeError("worker_poll_status_stale")
    # Worker-local IDLE/current_job_present never prove the remote durable queue
    # empty. Require explicit authoritative counters in the captured status.
    if snap.get("gpoint_pending_jobs") != 0 or snap.get("gpoint_running_jobs") != 0:
        raise RuntimeError("gpoint_queue_empty_not_proven_by_readonly_gateway")
    if not isinstance(snap.get("inference_job_ttl_seconds"), int) or snap["inference_job_ttl_seconds"] < MINIMUM_TTL_SECONDS:
        raise RuntimeError("effective_inference_ttl_not_proven_for_test_window")
    return hashes


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def restore(original_hashes: dict[str, str], stopped: bool) -> None:
    if stopped and not loaded():
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=15)
    if not loaded():
        raise RuntimeError("launchagent_restore_failed")
    for name, expected in original_hashes.items():
        if digest(Path(name)) != expected:
            raise RuntimeError("installed_worker_hash_changed")
    deadline = time.monotonic() + 45
    before_status = status()
    before = before_status.get("last_poll_at")
    before_updated = before_status.get("updated_at")
    while time.monotonic() < deadline:
        current = status()
        if (current.get("last_poll_at") != before or current.get("updated_at") != before_updated) and current.get("current_job_present") is False and loaded():
            return
        time.sleep(2)
    raise RuntimeError("worker_polling_did_not_resume")


def main() -> int:
    # Convert TERM into a Python exception so an in-progress test reaches finally.
    signal.signal(
        signal.SIGTERM,
        lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    if "--preflight-only" in sys.argv:
        try:
            preflight()
        except Exception as exc:
            print(f"REAL_CODEX_PREFLIGHT=BLOCKED:{exc}")
            return 2
        print("REAL_CODEX_PREFLIGHT=PASS")
        return 0

    # Environment/image/dependencies and synthetic full E2E are prepared before
    # this command. No production queue interaction or Telegram transport occurs.
    hashes = preflight()  # Fail before bootout unless queue is proven empty.
    stopped = False
    result_code = 1
    restore_error = None
    try:
        stopped = True  # Restore even if bootout partially succeeds then reports failure.
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=15)
        env = dict(os.environ, AGATA_CODEX_JOB_TIMEOUT_SECONDS="90")
        proc = subprocess.Popen(
            [str(REPO / "services/gsales/.venv/bin/python"),
             str(REPO / "services/gsales/tests/run_postgres.py"), "--real-codex"],
            cwd=REPO, env=env, start_new_session=True,
        )
        try:
            result_code = proc.wait(timeout=MAX_OUTAGE_SECONDS)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            result_code = 124
            print("REAL_CODEX_TEST=TIMEOUT; restoring GPOINT worker")
    finally:
        try:
            restore(hashes, stopped)
            print("GPOINT_WORKER_RESTORE=PASS")
        except Exception as exc:
            restore_error = str(exc)
            print("GPOINT_WORKER_RESTORE=FAIL:" + restore_error, file=sys.stderr)
    if restore_error:
        return 125
    print(f"REAL_CODEX_TEST_EXIT={result_code}")
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
