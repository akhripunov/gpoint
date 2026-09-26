"""Keep async PostgreSQL connection pools bound to their pytest event loop."""

from datetime import UTC, datetime

import pytest_asyncio

from app.db.base import dispose_engine
from app.db.models import Relationship


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def dispose_async_database_pool_between_modules():
    await dispose_engine()
    yield
    await dispose_engine()


async def ensure_test_relationship(session, tenant_id, user_id, character_id, scope, scenario_id):
    """Seed the canonical relationship anchor for existing PostgreSQL fixtures."""
    from sqlalchemy import select

    relationship = await session.scalar(
        select(Relationship).where(
            Relationship.tenant_id == tenant_id,
            Relationship.user_id == user_id,
            Relationship.character_id == character_id,
            Relationship.scope == scope,
            Relationship.scenario_id == scenario_id,
        )
    )
    if relationship is None:
        now = datetime.now(UTC)
        relationship = Relationship(
            tenant_id=tenant_id,
            user_id=user_id,
            character_id=character_id,
            scope=scope,
            scenario_id=scenario_id,
            status="ACTIVE",
            started_at=now,
            last_interaction_at=now,
        )
        session.add(relationship)
        await session.flush()
    return relationship
