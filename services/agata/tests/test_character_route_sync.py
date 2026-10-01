"""An unchanged persona must remain usable after the configured model changes."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm.evaluator import _EvaluatorCompiler

from app.api import internal
from app.db.models import Character, CharacterProfileVersion, ResponseRoutePolicy


class CharacterSession:
    """Evaluate the actual ORM predicates against existing route rows."""

    def __init__(self, tenant_id, routes):
        self.character = Character(id=uuid4(), tenant_id=tenant_id, name="Models Hub")
        self.profile = CharacterProfileVersion(
            character_id=self.character.id, version=1,
            personality={"identity_core": {"name": "Models Hub"}},
            speech_style={}, backstory={}, boundaries={},
        )
        self.routes = routes
        for route in routes:
            route.tenant_id = tenant_id
            route.character_id = self.character.id
            route.profile_version = 1

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, *args):
        pass

    async def get(self, *args):
        return object()

    async def scalar(self, statement):
        model = statement.column_descriptions[0]["entity"]
        if model is Character:
            return self.character
        if model is CharacterProfileVersion:
            return self.profile
        assert model is ResponseRoutePolicy
        matches = _EvaluatorCompiler().process(statement.whereclause)
        return next((row for row in self.routes if matches(row)), None)

    def add(self, row):
        assert isinstance(row, ResponseRoutePolicy)
        self.routes.append(row)


def route(model, enabled):
    return ResponseRoutePolicy(
        provider="mac_worker", model=model, enabled=enabled,
        prompt_version="agata-core-v1", schema_version="context-v1",
        policy_version="gpoint-internal-v1", streaming_allowed=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_current", [None, False, True])
async def test_unchanged_character_sync_reconciles_exact_model_route(monkeypatch, existing_current):
    tenant_id = uuid4()
    monkeypatch.setattr(internal, "get_settings", lambda: SimpleNamespace(
        tenant_id=str(tenant_id), llm_provider="mac_worker", llm_model="codex-default"
    ))
    old = route("cx/gpt-5.6-terra", True)
    routes = [old]
    if existing_current is not None:
        routes.append(route("codex-default", existing_current))
    session = CharacterSession(tenant_id, routes)
    payload = internal.CharacterSyncRequest(name="Models Hub")
    for _ in range(2):
        result = await internal.sync_character("models-hub", payload, session)
        assert result["profile_version"] == 1
        current = [row for row in session.routes if row.model == "codex-default"]
        assert len(current) == 1
        assert current[0].enabled is (existing_current is not False)
    assert old.model == "cx/gpt-5.6-terra"
