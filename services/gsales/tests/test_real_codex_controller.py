"""Offline safeguards for the one-shot installed LaunchAgent controller."""
import importlib.util
import plistlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/run-gsales-one-shot-real-codex.py"
spec = importlib.util.spec_from_file_location("real_codex_controller", SCRIPT)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def authoritative_snapshot(**overrides):
    api_image_id = "sha256:940236e984cff5e0123af62c58cea6ae5986fe879ca219daa2c91ff6ca8620e2"
    agata_image_id = "sha256:61299c506ed77971df0f75cb64cb91f739644ebbddfafc21d5cb903c4488dbb2"
    release_sha = "68bf0212d7aa5125394b0b39ffba9226eb106c0d"
    snapshot = {
        "snapshot_at": "2026-10-04T07:00:00.000000Z",
        "observed_monotonic": 100.0,
        "source_id": f"gpoint-vps:{release_sha}:api={api_image_id}:agata={agata_image_id}",
        "source_system": "gpoint",
        "database_name": "gpoint",
        "release_sha": release_sha,
        "api_build_sha": release_sha,
        "agata_build_sha": release_sha,
        "api_image_id": api_image_id,
        "agata_image_id": agata_image_id,
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "active_gpoint_profiles": 1,
        "tenant_identity_verified": True,
        "transaction_read_only": True,
        "same_login": True,
        "role_superuser": False,
        "role_bypassrls": False,
        "visible_tenant_rows": 1,
        "pending_inference_jobs": 0,
        "running_inference_jobs": 0,
        "effective_inference_ttl_seconds": 420,
    }
    return snapshot | overrides


def test_restore_bootstraps_original_and_waits_for_poll(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for path in paths:
        path.write_text("original:" + path.name)
    hashes = {str(path): controller.digest(path) for path in paths}
    monkeypatch.setattr(controller, "PLIST", paths[0])
    monkeypatch.setattr(controller, "loaded", lambda: state["loaded"])
    state = {"loaded": False, "calls": []}

    def run(args, **kwargs):
        state["calls"].append(args[0])
        state["loaded"] = True
        return subprocess.CompletedProcess(args, 0)

    poll_state = {"count": 0}
    def status():
        poll_state["count"] += 1
        return {"last_poll_at": "same", "updated_at": str(poll_state["count"]), "current_job_present": False}
    monkeypatch.setattr(controller.subprocess, "run", run)
    monkeypatch.setattr(controller, "status", status)
    monkeypatch.setattr(controller.time, "sleep", lambda _seconds: None)
    controller.restore(hashes, stopped=True)
    assert state["calls"] == ["launchctl"]
    assert controller.loaded()
    assert all(controller.digest(path) == hashes[str(path)] for path in paths)


def test_preflight_fails_closed_when_queue_snapshot_is_missing(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for path in paths:
        path.write_text("fixture")
    paths[0].write_bytes(plistlib.dumps({"Label": controller.LABEL, "RunAtLoad": True, "KeepAlive": True}))
    monkeypatch.setattr(controller, "PLIST", paths[0])
    monkeypatch.setattr(controller, "WORKER", paths[1])
    monkeypatch.setattr(controller, "WRAPPER", paths[2])
    monkeypatch.setattr(controller, "PYTHON", paths[1])
    monkeypatch.setattr(controller.subprocess, "check_output", lambda *a, **k: controller.PREPARATION_BASE_SHA + "\n")
    original_run = controller.subprocess.run
    def run(args, **kwargs):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, **kwargs)
    monkeypatch.setattr(controller.subprocess, "run", run)
    monkeypatch.setattr(controller, "loaded", lambda: True)
    monkeypatch.setattr(controller, "queue_snapshot", lambda: {"state": "idle", "current_job_present": False})
    monkeypatch.setattr(controller, "status", lambda: {"launch_agent_loaded": True, "state": "idle", "current_job_present": False, "last_poll_at": "2099-01-01T00:00:00+00:00", "queue_connectivity": "reachable"})
    with pytest.raises(RuntimeError, match="queue_snapshot_runtime_identity_mismatch"):
        controller.preflight(controller.PREPARATION_BASE_SHA)


def test_authoritative_snapshot_requires_empty_gpoint_queue_and_ttl_margin():
    snapshot = authoritative_snapshot()
    controller.validate_queue_snapshot(snapshot, now_monotonic=100.1)
    with pytest.raises(RuntimeError, match="gpoint_queue_not_empty"):
        controller.validate_queue_snapshot(authoritative_snapshot(pending_inference_jobs=1), now_monotonic=100.1)
    with pytest.raises(RuntimeError, match="effective_inference_ttl_below_outage_budget"):
        controller.validate_queue_snapshot(authoritative_snapshot(effective_inference_ttl_seconds=419), now_monotonic=100.1)


def test_authoritative_snapshot_rejects_wrong_runtime_or_untrusted_db_role():
    for override in (
        {"source_system": "gsales"},
        {"database_name": "other"},
        {"api_build_sha": "a" * 40},
        {"tenant_identity_verified": False},
        {"transaction_read_only": False},
        {"same_login": False},
        {"role_superuser": True},
        {"role_bypassrls": True},
        {"visible_tenant_rows": 0},
        {"tenant_id": "7508f36f-774a-48aa-8c6b-42f6f160415d"},
        {"active_gpoint_profiles": 0},
        {"release_sha": "8c30d676809f8c0f6d3254891bf096b0b8740bd4"},
        {"api_image_id": "sha256:" + "a" * 64},
    ):
        with pytest.raises(RuntimeError):
            controller.validate_queue_snapshot(authoritative_snapshot(**override), now_monotonic=100.1)


def test_candidate_sha_must_be_full_and_match_current_head():
    candidate = "a" * 40
    assert controller.validate_candidate_sha(candidate, candidate) == candidate
    for invalid in ("a" * 39, "a" * 40 + "b", "A" * 40, "branch-name"):
        with pytest.raises(RuntimeError, match="candidate_sha_mismatch"):
            controller.validate_candidate_sha(invalid, candidate)
    with pytest.raises(RuntimeError, match="candidate_sha_mismatch"):
        controller.validate_candidate_sha("b" * 40, candidate)


def test_authoritative_snapshot_must_be_recent_at_worker_recheck():
    with pytest.raises(RuntimeError, match="queue_snapshot_stale_before_switch"):
        controller.validate_queue_snapshot(authoritative_snapshot(), now_monotonic=106.0)


def test_outage_budget_reserves_bootout_and_confirmed_restore_time():
    # 15 seconds already spent booting out; keep 10 seconds for process
    # termination and 60 seconds for bootstrap plus polling confirmation.
    assert controller.remaining_test_budget_seconds(outage_deadline=400.0, now_monotonic=115.0) == 215.0


def test_immediate_switch_preflight_reads_queue_then_rechecks_local_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller, "queue_snapshot",
        lambda: calls.append("queue") or authoritative_snapshot(observed_monotonic=controller.time.monotonic()),
    )
    monkeypatch.setattr(controller, "assert_worker_idle", lambda: calls.append("local-idle"))
    result = controller.immediate_switch_preflight()
    assert result["pending_inference_jobs"] == 0
    assert calls == ["queue", "local-idle"]
