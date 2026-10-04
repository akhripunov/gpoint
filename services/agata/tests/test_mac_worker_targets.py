"""Target isolation/fairness without touching installed worker or Codex."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker

ENV = {
    "GPOINT_AGATA_WORKER_URL": "https://gpoint.amiora.one/api/agata-worker",
    "GPOINT_AGATA_WORKER_TOKEN": "point-private",
    "GPOINT_AGATA_TENANT_ID": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
    "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
    "GSALES_AGATA_WORKER_TOKEN": "sales-private",
    "GSALES_AGATA_TENANT_ID": "c4d67889-877e-4d1b-a8d8-597c479d638f",
}


def test_tenant_bindings_are_trusted_runtime_config_not_source_constants():
    isolated_point = "7508f36f-774a-48aa-8c6b-42f6f160415d"
    isolated_sales = "1a461346-f069-4b73-b80e-582cc64b0231"
    config = {
        **ENV,
        "GPOINT_AGATA_TENANT_ID": isolated_point,
        "GSALES_AGATA_TENANT_ID": isolated_sales,
    }
    with patch.dict(os.environ, config):
        assert worker.configured_tenant_bindings() == {
            "GPOINT": isolated_point,
            "GSALES": isolated_sales,
        }

    with patch.dict(os.environ, {**config, "GPOINT_AGATA_TENANT_ID": "invalid"}):
        with pytest.raises(worker.FatalConfigurationError, match="worker_tenant_binding_invalid"):
            worker.configured_tenant_bindings()
    with patch.dict(os.environ, {**config, "GSALES_AGATA_TENANT_ID": isolated_point}):
        with pytest.raises(worker.FatalConfigurationError, match="worker_tenants_must_be_distinct"):
            worker.configured_tenant_bindings()


def test_gpoint_foreign_claim_identity_is_rejected_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    calls = []

    def rpc(path, body=None, **kwargs):
        calls.append(path)
        if path == "claim":
            return {"job": {
                "id": "foreign", "leaseToken": "foreign",
                "target": "GPOINT", "tenant_id": "00000000-0000-4000-8000-000000000099",
                "request": {},
            }}
        return {"status": "ready"}

    with patch.dict(os.environ, ENV), patch.object(worker, "api_request", side_effect=rpc), patch.object(
        worker, "run_inference"
    ) as inference:
        with pytest.raises(worker.GatewayResponseError, match="worker_claim_target_mismatch"):
            worker.process_one("GPOINT")
        inference.assert_not_called()
    assert calls == ["health", "claim"]


def test_identityless_gpoint_claim_requires_the_verified_legacy_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    with patch.dict(os.environ, {**ENV, "GPOINT_AGATA_WORKER_URL": "https://other.example/worker"}), patch.object(
        worker, "api_request", side_effect=lambda path, body=None, **kwargs: (
            {"job": {"id": "legacy", "leaseToken": "lease", "request": {}}}
            if path == "claim" else {"status": "ready"}
        )
    ), patch.object(worker, "run_inference") as inference:
        with pytest.raises(worker.GatewayResponseError, match="worker_claim_target_mismatch"):
            worker.process_one("GPOINT")
        inference.assert_not_called()


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
            return {"job": {"id": "owned", "leaseToken": "owned", "target": "GSALES", "tenant_id": worker.configured_tenant_bindings()["GSALES"], "request": {}}}
        return {}
    with patch.dict(os.environ, ENV), patch.object(worker, "api_request", side_effect=rpc), patch.object(worker, "run_inference", return_value="a"*4001):
        assert worker.process_one("GSALES") is False
    assert "fail" in calls and "complete" not in calls
