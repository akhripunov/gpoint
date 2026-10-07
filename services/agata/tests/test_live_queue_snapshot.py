from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/agata-worker-queue-snapshot.py"
SPEC = importlib.util.spec_from_file_location("agata_live_queue_snapshot", SCRIPT)
snapshot_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(snapshot_module)


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, row, statements):
        self.row = row
        self.statements = statements

    def begin(self):
        return _Transaction()

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if "SELECT transaction_timestamp()" in str(statement):
            return _Result(self.row)
        return None


class _Connect:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return _Connect(self.connection)


def _configure(monkeypatch, *, lane="gsales", sales_drafts=True):
    tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f" if lane == "gsales" else "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    row = {
        "snapshot_at": datetime.now(UTC), "database_name": "gpoint",
        "transaction_read_only": "on", "same_login": True,
        "role_superuser": False, "role_bypassrls": False,
        "visible_tenant_rows": 1, "pending_inference_jobs": 0,
        "running_inference_jobs": 0,
    }
    statements = []
    connection = _Connection(row, statements)
    settings = SimpleNamespace(
        source_system=lane, tenant_id=tenant, sales_drafts_enabled=sales_drafts,
        inference_job_ttl_seconds=600,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.db.base._engine", lambda: _Engine(connection))
    monkeypatch.setattr("app.db.base.set_tenant_context", lambda *_args: asyncio.sleep(0))
    monkeypatch.setenv("SNAPSHOT_LANE", lane.upper())
    monkeypatch.setenv("SNAPSHOT_EXPECTED_TENANT_ID", tenant)
    monkeypatch.setenv("SNAPSHOT_RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("SNAPSHOT_API_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("SNAPSHOT_CORE_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("SNAPSHOT_API_IMAGE_ID", "sha256:" + "1" * 64)
    monkeypatch.setenv("SNAPSHOT_CORE_IMAGE_ID", "sha256:" + "2" * 64)
    return statements, tenant


def test_gsales_snapshot_counts_only_claimable_job_types_in_readonly_tenant_transaction(monkeypatch):
    statements, tenant = _configure(monkeypatch)

    result = asyncio.run(snapshot_module.read_snapshot())

    assert result["lane"] == "GSALES"
    assert result["tenant_id"] == tenant
    assert result["transaction_read_only"] is True
    assert result["same_login"] is True
    assert result["role_superuser"] is False
    assert result["role_bypassrls"] is False
    sql = "\n".join(statement for statement, _params in statements)
    assert "sales_draft_inference" in sql
    assert "job_type" in sql
    assert "tenant_id" in sql
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in sql
    assert "payload" not in result


def test_gpoint_snapshot_does_not_count_gsales_job_type(monkeypatch):
    statements, _tenant = _configure(monkeypatch, lane="gpoint", sales_drafts=True)

    result = asyncio.run(snapshot_module.read_snapshot())

    assert result["lane"] == "GPOINT"
    sql = "\n".join(statement for statement, _params in statements)
    assert "sales_draft_inference" not in sql


def test_snapshot_fails_closed_when_runtime_lane_or_tenant_differs_from_expected(monkeypatch):
    _configure(monkeypatch, lane="gpoint")
    monkeypatch.setenv("SNAPSHOT_LANE", "GSALES")

    with pytest.raises(RuntimeError, match="runtime_lane_identity_mismatch"):
        asyncio.run(snapshot_module.read_snapshot())
