"""PostgreSQL contract tests for relationship identity, races, ownership and RLS."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Conversation,
    Relationship,
    RoleplayScenario,
    Scope,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.domain.relationships import record_accepted_interaction, resolve_relationship

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = "7508f36f-774a-48aa-8c6b-42f6f160415d"


@pytest.mark.asyncio(loop_scope="module")
async def test_relationship_resolution_is_idempotent_concurrent_and_tuple_scoped():
    suffix = uuid4().hex
    user_a, user_b, anna, maria, scenario_one, scenario_two = (uuid4() for _ in range(6))
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, TENANT_ID)
            assert await session.get(Tenant, TENANT_ID) is not None
            session.add_all(
                [
                    User(id=user_a, tenant_id=TENANT_ID, external_id=f"rel-a-{suffix}"),
                    User(id=user_b, tenant_id=TENANT_ID, external_id=f"rel-b-{suffix}"),
                    Character(id=anna, tenant_id=TENANT_ID, external_id=f"anna-{suffix}", name="Anna"),
                    Character(id=maria, tenant_id=TENANT_ID, external_id=f"maria-{suffix}", name="Maria"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    RoleplayScenario(id=scenario_one, tenant_id=TENANT_ID, user_id=user_a, character_id=anna, name="S1", started_at=now),
                    RoleplayScenario(id=scenario_two, tenant_id=TENANT_ID, user_id=user_a, character_id=anna, name="S2", started_at=now),
                ]
            )

    principal = PrincipalContext(UUID(TENANT_ID), user_a, user_a, "internal")

    async def resolve(user_id, character_id, scope=Scope.real_world, scenario_id=None):
        async with SessionFactory() as session:
            return await resolve_relationship(
                session,
                PrincipalContext(principal.tenant_id, user_id, user_id, "internal"),
                character_id,
                scope,
                scenario_id,
            )

    sequential = await resolve(user_a, anna)
    assert sequential.last_interaction_at is None
    repeated = await resolve(user_a, anna)
    race_left, race_right = await asyncio.gather(resolve(user_a, anna), resolve(user_a, anna))
    contexts = [
        sequential.id,
        (await resolve(user_a, maria)).id,
        (await resolve(user_b, anna)).id,
        (await resolve(user_a, anna, Scope.roleplay, scenario_one)).id,
        (await resolve(user_a, anna, Scope.roleplay, scenario_two)).id,
    ]
    assert sequential.id == repeated.id == race_left.id == race_right.id
    assert len(set(contexts)) == 5
    accepted_at = datetime.now(UTC)
    async with SessionFactory() as session:
        await record_accepted_interaction(session, principal, sequential.id, accepted_at)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, TENANT_ID)
            refreshed = await session.get(Relationship, sequential.id)
            assert refreshed is not None and refreshed.last_interaction_at == accepted_at
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, TENANT_ID)
            count = await session.scalar(
                select(func.count()).select_from(Relationship).where(
                    Relationship.tenant_id == principal.tenant_id,
                    Relationship.user_id == user_a,
                    Relationship.character_id == anna,
                    Relationship.scope == Scope.real_world,
                    Relationship.scenario_id.is_(None),
                )
            )
            assert count == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_conversation_relationship_tuple_mismatch_is_rejected_by_postgres():
    suffix = uuid4().hex
    user_id, anna, maria = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, TENANT_ID)
            session.add_all(
                [
                    User(id=user_id, tenant_id=TENANT_ID, external_id=f"mismatch-{suffix}"),
                    Character(id=anna, tenant_id=TENANT_ID, external_id=f"a-{suffix}", name="Anna"),
                    Character(id=maria, tenant_id=TENANT_ID, external_id=f"m-{suffix}", name="Maria"),
                ]
            )
            await session.flush()
        principal = PrincipalContext(UUID(TENANT_ID), user_id, user_id, "internal")
        relationship = await resolve_relationship(session, principal, anna, Scope.real_world, None)
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    Conversation(
                        tenant_id=principal.tenant_id,
                        relationship_id=relationship.id,
                        user_id=user_id,
                        character_id=maria,
                        scope=Scope.real_world,
                        scenario_id=None,
                        started_at=now,
                    )
                )
                await session.flush()


@pytest.mark.asyncio(loop_scope="module")
async def test_relationship_rls_is_enforced_for_non_owner_role():
    assert TEST_DSN is not None
    url = make_url(TEST_DSN)
    assert url.database and url.database.endswith("_agata_test")
    sync_url = TEST_DSN.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    role = f"p21_rls_{uuid4().hex[:12]}"
    suffix = uuid4().hex
    user_id, character_id = uuid4(), uuid4()
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, TENANT_ID)
            session.add_all([
                User(id=user_id, tenant_id=TENANT_ID, external_id=f"rls-{suffix}"),
                Character(id=character_id, tenant_id=TENANT_ID, external_id=f"rls-{suffix}", name="RLS"),
            ])
    principal = PrincipalContext(UUID(TENANT_ID), user_id, user_id, "internal")
    async with SessionFactory() as session:
        relationship = await resolve_relationship(session, principal, character_id, Scope.real_world, None)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE ROLE "{role}"'))
            connection.execute(text(f'GRANT USAGE ON SCHEMA agata TO "{role}"'))
            connection.execute(text(f'GRANT SELECT ON agata.relationships TO "{role}"'))
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": TENANT_ID})
            connection.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert connection.scalar(
                text("SELECT count(*) FROM agata.relationships WHERE id=:id"),
                {"id": relationship.id},
            ) == 1
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(uuid4())})
            assert connection.scalar(
                text("SELECT count(*) FROM agata.relationships WHERE id=:id"),
                {"id": relationship.id},
            ) == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP OWNED BY "{role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        engine.dispose()
