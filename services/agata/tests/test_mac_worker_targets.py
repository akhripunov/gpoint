"""Eros-only Mac worker isolation and GPOINT contract regression tests."""
import ast
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
sys.path.insert(0, str(WORKER_DIR))
import gpoint_mac_worker as worker

ENV = {
    "GPOINT_EROS_WORKER_URL": "https://gpoint.one/api/gpoint-worker",
    "GPOINT_EROS_WORKER_TOKEN": "eros-private",
    "GPOINT_EROS_TENANT_ID": "a1a46146-4b6e-4b03-8332-0661ba4d46f4",
    "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
    "GSALES_AGATA_WORKER_TOKEN": "sales-private",
    "GSALES_AGATA_TENANT_ID": "c4d67889-877e-4d1b-a8d8-597c479d638f",
}


def test_runtime_has_no_worker_pool_or_conversation_lab_imports():
    tree = ast.parse(Path(worker.__file__).read_text())
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "agata_worker_pool" not in imported_modules
    assert "lab_queue" not in imported_modules


def test_worker_refuses_legacy_gateway_without_eros_endpoint():
    with patch.dict(os.environ, {
        "GPOINT_AGATA_WORKER_URL": "https://old.example/api/agata-worker",
        "GPOINT_AGATA_WORKER_TOKEN": "legacy-secret",
        "GPOINT_AGATA_TENANT_ID": ENV["GPOINT_EROS_TENANT_ID"],
    }, clear=True):
        with pytest.raises(worker.FatalConfigurationError, match="worker_gateway_or_auth_unconfigured"):
            worker.configured_targets()
    assert not hasattr(worker, "claim_lab_job")
    assert not hasattr(worker, "execute_slot")
    assert worker.TARGET_NAMES == ("GPOINT",)


def test_eros_target_and_tenant_ignore_legacy_gsales_credentials():
    with patch.dict(os.environ, ENV, clear=True):
        assert worker.configured_targets() == {
            "GPOINT": (ENV["GPOINT_EROS_WORKER_URL"], ENV["GPOINT_EROS_WORKER_TOKEN"]),
        }
        assert worker.configured_tenant_bindings() == {
            "GPOINT": ENV["GPOINT_EROS_TENANT_ID"],
        }


def test_worker_config_loader_does_not_export_legacy_lane_values(tmp_path, monkeypatch):
    config = tmp_path / "worker.env"
    config.write_text(
        "GPOINT_EROS_WORKER_URL=https://gpoint.one/api/gpoint-worker\n"
        "GPOINT_EROS_WORKER_TOKEN=eros-private\n"
        "GPOINT_EROS_TENANT_ID=a1a46146-4b6e-4b03-8332-0661ba4d46f4\n"
        "GSALES_AGATA_WORKER_URL=https://sales.invalid/private\n"
        "GSALES_AGATA_WORKER_TOKEN=sales-private\n"
        "AGATA_WORKER_CONCURRENCY=3\n"
    )
    config.chmod(0o600)
    monkeypatch.setattr(worker, "CONFIG", config)
    with patch.dict(os.environ, {}, clear=True):
        worker.load_worker_configs()
        assert worker.configured_targets() == {
            "GPOINT": (ENV["GPOINT_EROS_WORKER_URL"], "eros-private"),
        }
        assert "GSALES_AGATA_WORKER_URL" not in os.environ
        assert "GSALES_AGATA_WORKER_TOKEN" not in os.environ
        assert "AGATA_WORKER_CONCURRENCY" not in os.environ


def test_eros_gateway_claim_requires_pinned_gpoint_tenant():
    with patch.dict(os.environ, ENV, clear=True):
        worker.validate_target_claim("GPOINT", {
            "id": "job-1", "leaseToken": "lease-1", "target": "GPOINT",
            "tenant_id": ENV["GPOINT_EROS_TENANT_ID"], "purpose": "eros_chat_completion",
        })
        with pytest.raises(worker.GatewayResponseError, match="worker_claim_target_mismatch"):
            worker.validate_target_claim("GPOINT", {
                "id": "job-foreign", "leaseToken": "lease", "target": "GSALES",
                "tenant_id": ENV["GSALES_AGATA_TENANT_ID"],
            })


def test_foreign_gpoint_claim_is_failed_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status/worker-status.json")
    calls = []

    def rpc(path, body=None, **kwargs):
        calls.append((path, body))
        if path == "claim":
            return {"job": {
                "id": "foreign", "leaseToken": "foreign-lease", "target": "GPOINT",
                "tenant_id": "00000000-0000-4000-8000-000000000099", "request": {},
            }}
        return {"status": "ready"}

    with patch.dict(os.environ, ENV, clear=True), \
         patch.object(worker, "api_request", side_effect=rpc), \
         patch.object(worker, "run_inference") as inference:
        worker._last_heartbeat_attempt = float("-inf")
        assert worker.process_one("GPOINT") is False
        inference.assert_not_called()
    assert [path for path, _body in calls] == ["health", "claim", "fail"]
    assert calls[-1][1]["job_id"] == "foreign"


def test_non_gpoint_request_or_claim_cannot_route_to_another_lane():
    with patch.dict(os.environ, ENV, clear=True), \
         patch.object(worker, "api_request") as request:
        with pytest.raises(worker.FatalConfigurationError, match="worker_target_unconfigured"):
            worker.target_request("GSALES", "health")
        with pytest.raises(worker.GatewayResponseError, match="worker_target_invalid"):
            worker.process_one("GSALES")
        request.assert_not_called()


def test_validate_startup_is_read_only_and_reports_only_gpoint(tmp_path, monkeypatch):
    support = tmp_path / "gpoint-eros-worker"
    support.mkdir(mode=0o700)
    monkeypatch.setattr(worker, "CONFIG", tmp_path / "missing-worker.env")
    with patch.dict(os.environ, {**ENV, "GPOINT_EROS_WORKER_SUPPORT": str(support)}, clear=True), \
         patch.object(worker, "api_request", side_effect=AssertionError("startup made API request")), \
         patch.object(worker, "process_one", side_effect=AssertionError("startup claimed work")), \
         patch.object(worker, "run_inference", side_effect=AssertionError("startup generated text")):
        result = worker.validate_startup_configuration()
    assert result["state"] == "PASS"
    assert result["configured_targets"] == ["GPOINT"]
    assert result["lanes"] == {"GPOINT": {"configured": True, "enabled": True}}
    assert result["runtime_slots"] is False
    assert result["claim_requests"] == result["inference_requests"] == 0
    assert not (support / "locks").exists()
    tree = ast.parse(Path(worker.__file__).read_text())
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "agata_worker_pool" not in imported_modules
    assert "lab_queue" not in imported_modules


def test_status_only_probes_gpoint_and_has_migration_safety_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status/worker-status.json")
    monkeypatch.setattr(worker, "INFERENCE_PROCESS", tmp_path / "state/inference-process.json")
    monkeypatch.setattr(worker, "load_worker_configs", lambda: None)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)
    calls = []

    def target_request(target, path, body=None):
        calls.append((target, path))
        if path == "health":
            return {"status": "ok"}
        return {
            "lane": "GPOINT", "source_id": "gpoint-eros-worker-queue-status-v1:gpoint",
            "pending_jobs": 0, "running_jobs": 0, "snapshot_at": "2026-11-08T10:00:00Z",
        }

    monkeypatch.setattr(worker, "target_request", target_request)
    with patch.dict(os.environ, ENV, clear=True):
        assert worker.status("GPOINT") == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == [("GPOINT", "health"), ("GPOINT", "queue-status")]
    assert result["launch_agent_loaded"] is False
    assert result["active_inference"] is False
    assert result["current_job_present"] is False
    assert result["lanes"]["GPOINT"]["configured"] is True
    assert result["lanes"]["GPOINT"]["health"] == "HEALTHY"
    assert result["lanes"]["GPOINT"]["queue_snapshot_state"] == "VERIFIED"
    assert result["lanes"]["GPOINT"]["queue_snapshot_source_id"] == "gpoint-eros-worker-queue-status-v1:gpoint"
    assert result["lanes"]["GPOINT"]["pending_inference_jobs"] == 0
    assert "GSALES" not in result["lanes"] and "LAB" not in result["lanes"]
    assert "slots" not in result and "pool" not in result


def test_status_fails_closed_for_a_stale_or_unreadable_inference_marker(tmp_path, monkeypatch):
    marker = tmp_path / "inference-process.json"
    marker.write_text("not-json")
    monkeypatch.setattr(worker, "INFERENCE_PROCESS", marker)
    assert worker._active_inference_present() is True
    marker.write_text(json.dumps({"state": "starting", "pid": None}))
    assert worker._active_inference_present() is True
    marker.write_text(json.dumps({"state": "running", "pid": 99999999}))
    assert worker._active_inference_present() is False


def test_single_worker_holds_one_lock_and_polls_only_gpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "INFERENCE_PROCESS", tmp_path / "state/inference-process.json")
    with patch.dict(os.environ, ENV, clear=True), \
         patch.object(worker, "process_one", return_value=False) as process:
        assert worker.run_single_worker(max_cycles=1, sleep_fn=lambda _delay: None) == 0
    process.assert_called_once_with("GPOINT")
    assert (tmp_path / "locks/worker.lock").is_file()
    assert list(tmp_path.rglob("slot-*")) == []


def test_single_worker_lock_prevents_parallel_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    first = worker.acquire_worker_lock()
    try:
        with pytest.raises(RuntimeError, match="worker_already_running"):
            worker.acquire_worker_lock()
    finally:
        first.close()
        worker._WORKER_LOCK_FD = None


def test_reject_claim_for_eros_includes_required_purpose():
    claim = {"id": "job-1", "leaseToken": "lease-1", "purpose": "eros_chat_completion"}
    sent = []
    with patch.object(worker, "target_request", side_effect=lambda lane, action, body: sent.append((lane, action, body))):
        worker.reject_claim("GPOINT", claim, "worker_start_failed")
    assert sent == [("GPOINT", "fail", {
        "job_id": "job-1", "lease_token": "lease-1", "error_code": "worker_start_failed",
        "purpose": "eros_chat_completion",
    })]
