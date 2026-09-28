"""Readiness/metrics contracts without external services or live credentials."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import health


class ScalarSession:
    def __init__(self, values):
        self.values = list(values)

    async def scalar(self, _statement):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_readiness_accepts_expected_provider_schema_head_and_pgvector(monkeypatch):
    monkeypatch.setattr(
        health, "get_settings", lambda: SimpleNamespace(llm_provider="local_fake", llm_api_key="fake")
    )

    result = await health.ready(ScalarSession([1, 1, "agata0008", 1]))

    assert result == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_accepts_disabled_provider_without_api_key(monkeypatch):
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: SimpleNamespace(llm_provider="disabled", llm_api_key=None),
    )

    result = await health.ready(ScalarSession([1, 1, "agata0008", 1]))

    assert result == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_fails_closed_without_provider_configuration(monkeypatch):
    monkeypatch.setattr(
        health, "get_settings", lambda: SimpleNamespace(llm_provider="local_fake", llm_api_key=None)
    )
    session = ScalarSession([])

    with pytest.raises(HTTPException) as error:
        await health.ready(session)

    assert error.value.status_code == 503
    assert session.values == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        [1, 1, "agata0005", 1],
        [1, 1, "agata0008", None],
        [RuntimeError("disposable database unavailable")],
    ],
    ids=["wrong-head", "missing-pgvector", "database-unavailable"],
)
async def test_readiness_fails_closed_for_dependency_or_revision_mismatch(monkeypatch, values):
    monkeypatch.setattr(
        health, "get_settings", lambda: SimpleNamespace(llm_provider="local_fake", llm_api_key="fake")
    )

    with pytest.raises(HTTPException) as error:
        await health.ready(ScalarSession(values))

    assert error.value.status_code == 503


class MetricsSession:
    def __init__(self, values):
        self.values = iter(values)
        self.executed = 0

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, _statement, _params):
        self.executed += 1

    async def scalar(self, _statement):
        return next(self.values)


@pytest.mark.asyncio
async def test_metrics_are_aggregate_only_and_content_free(monkeypatch):
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: SimpleNamespace(tenant_id="7508f36f-774a-48aa-8c6b-42f6f160415d"),
    )
    session = MetricsSession([12, 3, 1])

    result = await health.metrics(session)

    assert result == {
        "status": "ok",
        "messages": 12,
        "active_memories": 3,
        "recent_failed_responses": 1,
    }
    assert session.executed == 1
