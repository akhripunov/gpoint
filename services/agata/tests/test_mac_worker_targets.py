"""Target isolation/fairness without touching installed worker or Codex."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker

ENV = {
    "GPOINT_AGATA_WORKER_URL": "https://point.example/worker",
    "GPOINT_AGATA_WORKER_TOKEN": "point-private",
    "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
    "GSALES_AGATA_WORKER_TOKEN": "sales-private",
}


def test_target_configuration_rejects_reused_credentials():
    with patch.dict(os.environ, {**ENV, "GSALES_AGATA_WORKER_TOKEN": "point-private"}):
        with pytest.raises(worker.FatalConfigurationError):
            worker.configured_targets()


def test_scheduler_fairness_and_target_failure_isolation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    def process(target):
        calls.append(target)
        if target == "GSALES":
            raise worker.TransientNetworkError("offline")
        return True
    with patch.dict(os.environ, ENV), patch.object(worker, "process_one", side_effect=process):
        scheduler = worker.TargetScheduler()
        for _ in range(8):
            scheduler.poll(now=0)
    assert calls[:2] == ["GPOINT", "GSALES"]
    assert calls.count("GPOINT") == 7
    assert calls.count("GSALES") == 1
    assert "point-private" not in worker.STATUS.read_text()
    assert "sales-private" not in worker.STATUS.read_text()


def test_sales_claim_identity_is_checked_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    def rpc(path, body=None, **kwargs):
        if path == "claim":
            return {"job": {"id": "foreign", "leaseToken": "foreign", "target": "GPOINT", "request": {}}}
        return {}
    with patch.dict(os.environ, ENV), patch.object(worker, "api_request", side_effect=rpc), patch.object(worker, "run_inference") as inference:
        with pytest.raises(worker.GatewayResponseError):
            worker.process_one("GSALES")
        inference.assert_not_called()


def test_sales_oversized_output_is_failed_without_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    calls = []
    def rpc(path, body=None, **kwargs):
        calls.append(path)
        if path == "claim":
            return {"job": {"id": "owned", "leaseToken": "owned", "target": "GSALES", "tenant_id": worker.TARGET_TENANTS["GSALES"], "request": {}}}
        return {}
    with patch.dict(os.environ, ENV), patch.object(worker, "api_request", side_effect=rpc), patch.object(worker, "run_inference", return_value="a"*4001):
        assert worker.process_one("GSALES") is False
    assert "fail" in calls and "complete" not in calls
