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
    monkeypatch.setattr(controller.subprocess, "check_output", lambda *a, **k: controller.EXPECTED_SHA + "\n")
    monkeypatch.setattr(controller, "loaded", lambda: True)
    monkeypatch.setattr(controller, "status", lambda: {"launch_agent_loaded": True, "state": "idle", "current_job_present": False, "last_poll_at": "2099-01-01T00:00:00+00:00", "queue_connectivity": "reachable"})
    with pytest.raises(RuntimeError, match="gpoint_queue_empty_not_proven"):
        controller.preflight()
