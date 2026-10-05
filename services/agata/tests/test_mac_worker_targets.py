"""Target isolation/fairness without touching installed worker or Codex."""
import hashlib
import json
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


def test_legacy_gpoint_sidecar_binds_identity_to_installed_worker_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    worker_hash = hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()
    sidecar = {
        "version": 1,
        "candidate_sha": "a" * 40,
        "worker_sha256": worker_hash,
        "tenant_id": ENV["GPOINT_AGATA_TENANT_ID"],
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    (tmp_path / "config/tenant-bindings.json").parent.mkdir()
    (tmp_path / "config/tenant-bindings.json").write_text(json.dumps(sidecar))
    (tmp_path / "config/tenant-bindings.json").chmod(0o600)
    with patch.dict(os.environ, {
        "GPOINT_AGATA_WORKER_URL": worker.LEGACY_GPOINT_GATEWAY_URL,
        "GPOINT_AGATA_WORKER_TOKEN": "point-private",
    }, clear=True):
        assert worker.configured_tenant_bindings() == {"GPOINT": ENV["GPOINT_AGATA_TENANT_ID"]}

    sidecar["worker_sha256"] = "0" * 64
    (tmp_path / "config/tenant-bindings.json").write_text(json.dumps(sidecar))
    (tmp_path / "config/tenant-bindings.json").chmod(0o600)
    with patch.dict(os.environ, {
        "GPOINT_AGATA_WORKER_URL": worker.LEGACY_GPOINT_GATEWAY_URL,
        "GPOINT_AGATA_WORKER_TOKEN": "point-private",
    }, clear=True):
        with pytest.raises(worker.FatalConfigurationError, match="worker_tenant_binding_invalid"):
            worker.configured_tenant_bindings()


def test_legacy_gpoint_without_binding_fails_closed_on_identity_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    with patch.dict(os.environ, {
        "GPOINT_AGATA_WORKER_URL": worker.LEGACY_GPOINT_GATEWAY_URL,
        "GPOINT_AGATA_WORKER_TOKEN": "point-private",
    }, clear=True):
        assert worker.configured_tenant_bindings() == {}
        def rpc(path, body=None, **kwargs):
            if path == "claim":
                return {"job": {"id": "job", "leaseToken": "lease", "target": "GPOINT",
                                 "tenant_id": ENV["GPOINT_AGATA_TENANT_ID"], "request": {}}}
            return {"status": "ready"}
        with patch.object(worker, "api_request", side_effect=rpc), patch.object(worker, "run_inference") as inference:
            with pytest.raises(worker.GatewayResponseError, match="worker_claim_target_mismatch"):
                worker.process_one("GPOINT")
            inference.assert_not_called()


def test_gpoint_foreign_claim_identity_is_rejected_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    worker._last_heartbeat_attempt = float("-inf")
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


def test_claim_and_execution_are_separate_for_supervisor_slot_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "SUPPORT", tmp_path)
    monkeypatch.setattr(worker, "STATUS", tmp_path / "status.json")
    claim = {"id": "sales-job", "leaseToken": "lease", "target": "GSALES",
             "tenant_id": ENV["GSALES_AGATA_TENANT_ID"], "request": {"persona": {"name": "seller"}}}
    calls = []

    def rpc(path, body=None, **kwargs):
        calls.append((path, body, kwargs))
        if path == "claim":
            return {"job": claim}
        return {"accepted": True, "completed": True}

    with patch.dict(os.environ, ENV), patch.object(worker, "api_request", side_effect=rpc), patch.object(
        worker, "run_inference", return_value="draft"
    ) as inference:
        worker._last_heartbeat_attempt = float("-inf")
        worker._target_heartbeats.pop("GSALES", None)
        leased = worker.claim_target_job("GSALES")
        assert leased == claim
        inference.assert_not_called()
        assert worker.execute_claimed_job("GSALES", leased) is True
        inference.assert_called_once_with(claim)

    assert [item[0] for item in calls] == ["health", "claim", "complete"]
    assert calls[-1][1]["job_id"] == "sales-job"


def test_orphan_lab_slot_lock_prevents_startup_from_marking_live_job_ambiguous(tmp_path):
    from agata_worker_pool import PoolPaths, acquire_lock

    paths = PoolPaths(tmp_path)
    paths.ensure()
    slot_dir = paths.slot_dir("slot-01")
    slot_dir.mkdir(parents=True)
    (slot_dir / "state.json").write_text(json.dumps({
        "state": "BUSY", "lane": "LAB", "job_id": "lab-active-1", "phase": "INFERENCE_STARTED",
    }))
    lock = acquire_lock(paths.slot_lock("slot-01"))
    assert worker._active_lab_slot_job_ids(paths) == {"lab-active-1"}
    lock.close()
    assert worker._active_lab_slot_job_ids(paths) == set()
