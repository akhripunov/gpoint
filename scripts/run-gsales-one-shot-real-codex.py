#!/usr/bin/env python3
"""One-shot guarded Real Codex QA; restores the original Mac LaunchAgent in finally."""
from __future__ import annotations

import base64
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
MAX_FULL_OUTAGE_SECONDS = 300
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
WORKER = SUPPORT / "gpoint_mac_worker.py"
WRAPPER = SUPPORT / "run_codex_worker.sh"
PYTHON = SUPPORT / ".venv/bin/python3.12"
QUEUE_SNAPSHOT_SCRIPT = REPO / "scripts/gpoint-agata-readonly-queue-snapshot.py"


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


def remaining_test_budget_seconds(*, outage_deadline: float, now_monotonic: float | None = None) -> float:
    """Keep process termination and confirmed worker restore inside the outage cap."""
    now = time.monotonic() if now_monotonic is None else now_monotonic
    return max(0.0, outage_deadline - now - MAX_PROCESS_TERMINATION_SECONDS - MAX_RESTORE_SECONDS)


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
        or snapshot.get("api_build_sha") != release_sha
        or snapshot.get("agata_build_sha") != release_sha
    ):
        raise RuntimeError("queue_snapshot_runtime_identity_mismatch")
    for field in ("api_image_id", "agata_image_id"):
        value = snapshot.get(field)
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
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


def preflight() -> tuple[dict[str, str], dict]:
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
    snapshot = immediate_switch_preflight()
    return hashes, snapshot


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


def restore(original_hashes: dict[str, str], stopped: bool, *, outage_deadline: float | None = None) -> None:
    deadline = outage_deadline if outage_deadline is not None else time.monotonic() + MAX_RESTORE_SECONDS
    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())
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
    before = before_status.get("last_poll_at")
    before_updated = before_status.get("updated_at")
    while time.monotonic() < confirm_deadline:
        current = status()
        if (current.get("last_poll_at") != before or current.get("updated_at") != before_updated) and current.get("current_job_present") is False and loaded():
            return
        time.sleep(min(2, max(0, confirm_deadline - time.monotonic())))
    raise RuntimeError("worker_polling_did_not_resume_before_deadline")


def main() -> int:
    # Convert TERM into a Python exception so an in-progress test reaches finally.
    signal.signal(
        signal.SIGTERM,
        lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    if "--preflight-only" in sys.argv:
        try:
            _, snapshot = preflight()
        except Exception as exc:
            print(f"REAL_CODEX_PREFLIGHT=BLOCKED:{exc}")
            return 2
        print("REAL_CODEX_QUEUE_SNAPSHOT=" + json.dumps({key: snapshot[key] for key in (
            "snapshot_at", "source_id", "release_sha", "pending_inference_jobs",
            "running_inference_jobs", "effective_inference_ttl_seconds",
        )}, separators=(",", ":")))
        print("REAL_CODEX_PREFLIGHT=PASS")
        return 0

    # Environment/image/dependencies and synthetic full E2E are prepared before
    # this command. No production queue interaction or Telegram transport occurs.
    hashes, _ = preflight()  # Fail before bootout unless live queue is proven empty.
    stopped = False
    result_code = 1
    restore_error = None
    outage_deadline = None
    try:
        # Repeat the DB snapshot directly before switching, then recheck the
        # local worker so any generation begun during the remote read blocks.
        immediate_switch_preflight()
        outage_deadline = time.monotonic() + MAX_FULL_OUTAGE_SECONDS
        stopped = True  # Restore even if bootout partially succeeds then reports failure.
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=min(MAX_BOOTOUT_SECONDS, max(1, outage_deadline - time.monotonic())))
        env = dict(os.environ, AGATA_CODEX_JOB_TIMEOUT_SECONDS="90")
        proc = subprocess.Popen(
            [str(REPO / "services/gsales/.venv/bin/python"),
             str(REPO / "services/gsales/tests/run_postgres.py"), "--real-codex"],
            cwd=REPO, env=env, start_new_session=True,
        )
        try:
            result_code = proc.wait(timeout=remaining_test_budget_seconds(outage_deadline=outage_deadline))
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            result_code = 124
            print("REAL_CODEX_TEST=TIMEOUT; restoring GPOINT worker")
    finally:
        try:
            restore(hashes, stopped, outage_deadline=outage_deadline)
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
