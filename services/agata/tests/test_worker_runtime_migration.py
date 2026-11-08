"""Eros-only Mac Worker migration and single-agent safety contracts."""
from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "worker/migrate_worker_runtime.py"
SPEC = importlib.util.spec_from_file_location("worker_runtime_migration", MODULE_PATH)
migration = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(migration)


def _queue_status(now: datetime, *, pending: int = 0, running: int = 0) -> dict:
    return {
        "launch_agent_loaded": True,
        "current_job_present": False,
        "active_inference": False,
        "last_poll_at": now.isoformat(),
        "lanes": {"GPOINT": {
            "configured": True,
            "health": "HEALTHY",
            "queue_snapshot_state": "VERIFIED",
            "queue_snapshot_source_id": "gpoint-eros-worker-queue-status-v1:gpoint",
            "queue_snapshot_at": now.isoformat(),
            "pending_inference_jobs": pending,
            "running_inference_jobs": running,
        }},
    }


def test_fresh_empty_gpoint_status_passes_without_pool_or_slots():
    now = datetime.now(UTC)
    evidence = migration.require_fresh_empty_production_queues(_queue_status(now), now=now)
    assert evidence["active_workers"] == 0
    assert set(evidence["lanes"]) == {"GPOINT"}


@pytest.mark.parametrize("change", [
    lambda status: status.update(current_job_present=True),
    lambda status: status.update(active_inference=True),
    lambda status: status.update(launch_agent_loaded=False),
    lambda status: status["lanes"]["GPOINT"].update(pending_inference_jobs=1),
    lambda status: status["lanes"]["GPOINT"].update(running_inference_jobs=1),
    lambda status: status["lanes"]["GPOINT"].update(queue_snapshot_state="UNAVAILABLE"),
])
def test_queue_gate_blocks_active_or_unverified_gpoint_state(change):
    now = datetime.now(UTC)
    status = _queue_status(now)
    change(status)
    with pytest.raises(migration.PreflightFailure):
        migration.require_fresh_empty_production_queues(status, now=now)


def test_queue_gate_rejects_any_non_gpoint_lane():
    now = datetime.now(UTC)
    status = _queue_status(now)
    status["lanes"]["GSALES"] = {}
    with pytest.raises(migration.PreflightFailure):
        migration.validate_fresh_production_queue_snapshots(status, now=now)


def test_launchagent_restore_unloads_candidate_before_bootstrapping_source(monkeypatch, tmp_path):
    events = []
    loaded = {migration.LABEL: True, migration.OLD_LABEL: False}
    source_plist = tmp_path / "old.plist"
    original = plistlib.dumps({"Label": migration.OLD_LABEL})

    def bootout(label):
        events.append(("bootout", label))
        loaded[label] = False

    def run(command, **kwargs):
        if command[1] == "bootstrap":
            label = plistlib.loads(Path(command[-1]).read_bytes())["Label"]
            events.append(("bootstrap", label))
            assert not any(loaded.values())
            loaded[label] = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(migration, "launchctl_loaded", lambda label: loaded[label])
    monkeypatch.setattr(migration, "_bootout_and_wait", bootout)
    monkeypatch.setattr(migration.subprocess, "run", run)

    result = migration._restore_single_launchagent(migration.OLD_LABEL, source_plist, original)

    assert events == [("bootout", migration.LABEL), ("bootstrap", migration.OLD_LABEL)]
    assert loaded == {migration.LABEL: False, migration.OLD_LABEL: True}
    assert result["restored_label"] == migration.OLD_LABEL


def test_failed_candidate_unload_never_bootstraps_old_agent(monkeypatch, tmp_path):
    loaded = {migration.LABEL: True, migration.OLD_LABEL: False}
    bootstraps = []

    def bootout(label):
        raise RuntimeError("candidate did not stop")

    monkeypatch.setattr(migration, "launchctl_loaded", lambda label: loaded[label])
    monkeypatch.setattr(migration, "_bootout_and_wait", bootout)
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs: bootstraps.append(args))

    with pytest.raises(RuntimeError, match="candidate did not stop"):
        migration._restore_single_launchagent(
            migration.OLD_LABEL, tmp_path / "old.plist", plistlib.dumps({"Label": migration.OLD_LABEL}),
        )
    assert not bootstraps
    assert loaded == {migration.LABEL: True, migration.OLD_LABEL: False}


def test_failed_candidate_unload_stops_loaded_source_as_final_safety_action(monkeypatch, tmp_path):
    loaded = {migration.LABEL: True, migration.OLD_LABEL: True}
    stopped = []

    def bootout(label):
        stopped.append(label)
        if label == migration.LABEL:
            raise RuntimeError("candidate did not stop")
        loaded[label] = False

    monkeypatch.setattr(migration, "launchctl_loaded", lambda label: loaded[label])
    monkeypatch.setattr(migration, "_bootout_and_wait", bootout)
    with pytest.raises(RuntimeError, match="candidate did not stop"):
        migration._restore_single_launchagent(
            migration.OLD_LABEL, tmp_path / "old.plist", plistlib.dumps({"Label": migration.OLD_LABEL}),
        )
    assert stopped == [migration.LABEL, migration.OLD_LABEL]
    assert loaded == {migration.LABEL: True, migration.OLD_LABEL: False}


def test_inventory_refuses_both_agent_identities_as_active(monkeypatch):
    monkeypatch.setattr(migration, "launchctl_loaded", lambda _label: True)
    with pytest.raises(RuntimeError, match="multiple_worker_launchagents_loaded"):
        migration._active_worker_identity()


def test_eros_bootstrap_requires_old_agent_absent_and_verifies_single_new_label(monkeypatch, tmp_path):
    loaded = {migration.LABEL: False, migration.OLD_LABEL: True}
    calls = []
    monkeypatch.setattr(migration, "launchctl_loaded", lambda label: loaded[label])
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="multiple_worker_launchagents_loaded"):
        migration._bootstrap_candidate_only(migration.OLD_LABEL, tmp_path / "eros.plist")
    assert calls == []

    loaded[migration.OLD_LABEL] = False
    def bootstrap(*args, **kwargs):
        loaded[migration.LABEL] = True
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0)
    monkeypatch.setattr(migration.subprocess, "run", bootstrap)
    migration._bootstrap_candidate_only(migration.OLD_LABEL, tmp_path / "eros.plist")
    assert loaded == {migration.LABEL: True, migration.OLD_LABEL: False}
    assert len(calls) == 1


def test_source_bootstrap_failure_leaves_no_second_agent(monkeypatch, tmp_path):
    loaded = {migration.LABEL: True, migration.OLD_LABEL: False}
    events = []

    def bootout(label):
        events.append(("bootout", label))
        loaded[label] = False

    def run(command, **kwargs):
        if command[1] == "bootstrap":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(migration, "launchctl_loaded", lambda label: loaded[label])
    monkeypatch.setattr(migration, "_bootout_and_wait", bootout)
    monkeypatch.setattr(migration.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        migration._restore_single_launchagent(
            migration.OLD_LABEL, tmp_path / "old.plist", plistlib.dumps({"Label": migration.OLD_LABEL}),
        )
    assert loaded == {migration.LABEL: False, migration.OLD_LABEL: False}
    assert events == [("bootout", migration.LABEL), ("bootout", migration.OLD_LABEL)]


def test_pause_failure_clears_maintenance_marker_without_bootstrap(tmp_path, monkeypatch):
    marker = tmp_path / "maintenance-stop"
    marker.touch()

    def pause(_label):
        raise RuntimeError("active_inference_blocks_transition")

    monkeypatch.setattr(migration, "_pause_idle_launchagent", pause)
    with pytest.raises(RuntimeError, match="active_inference_blocks_transition"):
        migration._pause_worker_or_clear_marker(migration.OLD_LABEL, marker)
    assert not marker.exists()


def test_runtime_bundle_and_installer_are_eros_only():
    worker_dir = MODULE_PATH.parent
    installer = (worker_dir / "install_codex_launch_agent.py").read_text(encoding="utf-8")
    worker = (worker_dir / "gpoint_mac_worker.py").read_text(encoding="utf-8")
    migration_source = MODULE_PATH.read_text(encoding="utf-8")
    assert migration.ASSETS == ("gpoint_mac_worker.py", "run_codex_worker.sh", "requirements.txt")
    assert "one.gpoint.eros-worker" in installer
    assert "gpoint-eros-worker" in installer
    assert "TARGET_NAMES = (\"GPOINT\",)" in worker
    assert "install-lane-slot" not in migration_source
    assert "install-lab-slot" not in migration_source
    assert "--preserve-stopped-gsales" not in migration_source
    assert not (worker_dir / "install_launch_agent.py").exists()


def test_candidate_runtime_skeleton_has_only_private_eros_state_dirs(tmp_path, monkeypatch):
    root = tmp_path / "gpoint-eros-worker"
    monkeypatch.setattr(migration, "ROOT", root)

    migration._ensure_runtime_directories()

    assert {path.name for path in root.iterdir()} == {
        "config", "state", "locks", "receipts", "status", "logs",
    }
    assert all(path.stat().st_mode & 0o077 == 0 for path in root.iterdir())
