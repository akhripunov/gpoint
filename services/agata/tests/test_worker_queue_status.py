"""Read-only, tenant-scoped production queue snapshot contract."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api import internal


class _AsyncBegin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Result:
    def __init__(self, pending, running):
        self.rows = [("pending", pending), ("running", running)]

    def all(self):
        return self.rows


class _Session:
    def __init__(self, counts):
        self.counts = iter(counts)
        self.statements = []
        self.aggregate_statements = []
        self.aggregate_parameters = []

    def begin(self):
        return _AsyncBegin()

    async def execute(self, statement, parameters=None):
        self.statements.append((str(statement), parameters))
        if "set_config" in str(statement):
            return None
        self.aggregate_statements.append(str(statement))
        self.aggregate_parameters.append(statement.compile().params)
        return _Result(next(self.counts), next(self.counts))


def test_gpoint_queue_status_is_aggregate_read_only_and_has_snapshot_identity(monkeypatch):
    tenant_id = "7508f36f-774a-48aa-8c6b-42f6f160415d"
    monkeypatch.setattr(internal, "get_settings", lambda: SimpleNamespace(
        tenant_id=tenant_id, source_system="gpoint", sales_drafts_enabled=False,
    ))
    session = _Session((2, 1))

    result = asyncio.run(internal.worker_queue_status(session=session))

    assert result["source_id"] == "agata-worker-queue-status-v1:gpoint"
    assert result["lane"] == "GPOINT"
    assert result["pending_jobs"] == 2
    assert result["running_jobs"] == 1
    assert result["snapshot_at"]
    assert not ({"payload", "request", "prompt", "user_id", "job_ids"} & result.keys())
    assert len(session.aggregate_statements) == 1
    assert "count(" in session.aggregate_statements[0].lower()
    assert "outbox_jobs" in session.aggregate_statements[0].lower()
    assert "tenant_id" in session.aggregate_statements[0].lower()
    assert all(not any(word in statement.lower() for word in ("update ", "delete ", "insert "))
               for statement, _parameters in session.statements)
    assert "sales_draft_inference" not in repr(session.aggregate_parameters[0])


def test_gsales_queue_status_counts_exact_claimable_job_types(monkeypatch):
    monkeypatch.setattr(internal, "get_settings", lambda: SimpleNamespace(
        tenant_id="7508f36f-774a-48aa-8c6b-42f6f160415d",
        source_system="gsales", sales_drafts_enabled=True,
    ))
    session = _Session((0, 0))

    result = asyncio.run(internal.worker_queue_status(session=session))

    assert result["lane"] == "GSALES"
    assert "sales_draft_inference" in repr(session.aggregate_parameters[0])
