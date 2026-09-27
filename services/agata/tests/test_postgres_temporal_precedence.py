"""PostgreSQL regressions for temporal slot reconciliation and serialized revisions."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import ensure_test_relationship
from sqlalchemy import select

from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    Scope,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.memory.explicit import ExplicitMemoryService, Forget
from app.memory.precedence import MemoryPrecedenceService, TemporalMode
from app.memory.retrieval import HybridRetrievalService, RetrievalRequest

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("b12b6404-e797-4871-a1ea-000000000001")


async def _owner(suffix: str):
    user_id, character_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(Tenant(id=TENANT_ID))
                await session.flush()
            await set_tenant_context(session, str(TENANT_ID))
            session.add_all([
                User(id=user_id, tenant_id=TENANT_ID, external_id=f"p23-{suffix}-{uuid4().hex}"),
                Character(id=character_id, tenant_id=TENANT_ID,
                          external_id=f"p23-{suffix}-{uuid4().hex}", name="Synthetic"),
            ])
            await session.flush()
            relationship = await ensure_test_relationship(
                session, TENANT_ID, user_id, character_id, Scope.real_world, None,
            )
            session.add_all([
                Consent(tenant_id=TENANT_ID, user_id=user_id, purpose="long_term_memory",
                        granted_at=now, policy_version="test-v1"),
                MemoryControlState(tenant_id=TENANT_ID, relationship_id=relationship.id,
                                   user_id=user_id, character_id=character_id,
                                   scope=Scope.real_world, scenario_id=None),
            ])
            await session.flush()
    return user_id, character_id, relationship.id


def _memory(*, memory_id, relationship_id, user_id, character_id, slot, observed_at,
            version=1, supersedes_id=None, lifecycle=LifecycleState.active,
            valid_from=None, valid_until=None, evidence=EvidenceState.explicit):
    return Memory(
        id=memory_id, tenant_id=TENANT_ID, relationship_id=relationship_id,
        user_id=user_id, character_id=character_id, kind="semantic_fact",
        scope=Scope.real_world, scenario_id=None, slot_key=slot, predicate="works_at",
        value_text=None, observed_at=observed_at, valid_from=valid_from, valid_until=valid_until,
        evidence_state=evidence, lifecycle_state=lifecycle, confidence=0.8, importance=0.1,
        version=version, supersedes_id=supersedes_id, created_epoch=0,
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_three_revision_chain_expiry_and_no_forget_rollback():
    user_id, character_id, relationship_id = await _owner("chain")
    now = datetime.now(UTC)
    slot = f"user.works_at.p23.{uuid4().hex}"
    a_id, b_id, c_id = uuid4(), uuid4(), uuid4()
    principal = PrincipalContext(TENANT_ID, user_id, user_id, "internal")
    key = b"p" * 32

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add(_memory(memory_id=a_id, relationship_id=relationship_id,
                                user_id=user_id, character_id=character_id, slot=slot,
                                observed_at=now - timedelta(days=3)))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            old = await MemoryPrecedenceService(session).current_for_slot(
                tenant_id=TENANT_ID, relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, scope=Scope.real_world, scenario_id=None,
                slot_key=slot, at=now,
            )
            assert old is not None
            new = _memory(memory_id=b_id, relationship_id=relationship_id, user_id=user_id,
                          character_id=character_id, slot=slot, observed_at=now - timedelta(days=2),
                          version=old.version + 1, supersedes_id=old.id)
            await MemoryPrecedenceService(session).supersede(old, new, transition_at=now)
            session.add(new)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            old = await MemoryPrecedenceService(session).current_for_slot(
                tenant_id=TENANT_ID, relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, scope=Scope.real_world, scenario_id=None,
                slot_key=slot, at=now,
            )
            assert old is not None and old.id == b_id
            new = _memory(memory_id=c_id, relationship_id=relationship_id, user_id=user_id,
                          character_id=character_id, slot=slot, observed_at=now - timedelta(days=1),
                          version=old.version + 1, supersedes_id=old.id)
            await MemoryPrecedenceService(session).supersede(old, new, transition_at=now)
            session.add(new)

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            rows = list((await session.scalars(select(Memory).where(
                Memory.tenant_id == TENANT_ID, Memory.relationship_id == relationship_id,
                Memory.slot_key == slot,
            ).order_by(Memory.version))).all())
            assert [(row.version, row.lifecycle_state, row.supersedes_id) for row in rows] == [
                (1, LifecycleState.historical, None),
                (2, LifecycleState.historical, a_id),
                (3, LifecycleState.active, b_id),
            ]

    # Forgetting the active head cannot reactivate an earlier revision.
    async with SessionFactory() as session:
        service = ExplicitMemoryService(
            session, hmac_key=key, hmac_key_version="test-v1", slot_identity_hmac_key=key,
        )
        assert await service.forget(principal, Forget(command_id=f"forget-{uuid4().hex}", memory_id=c_id)) is False
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            states = list((await session.scalars(select(Memory.lifecycle_state).where(
                Memory.tenant_id == TENANT_ID, Memory.relationship_id == relationship_id,
                Memory.slot_key == slot,
            ).order_by(Memory.version))).all())
            assert states == [LifecycleState.historical, LifecycleState.historical, LifecycleState.deleted]


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_expired_slot_reconciles_and_concurrent_updates_serialize():
    user_id, character_id, relationship_id = await _owner("concurrency")
    now = datetime.now(UTC)
    slot = f"user.current_activity.p23.{uuid4().hex}"
    expired_id = uuid4()
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add(_memory(memory_id=expired_id, relationship_id=relationship_id,
                                user_id=user_id, character_id=character_id, slot=slot,
                                observed_at=now - timedelta(days=4), valid_until=now - timedelta(hours=1)))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            reconciled = await MemoryPrecedenceService(session).current_for_slot(
                tenant_id=TENANT_ID, relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, scope=Scope.real_world, scenario_id=None,
                slot_key=slot, at=now,
            )
            assert reconciled is not None and reconciled.lifecycle_state == LifecycleState.historical
            initial = _memory(
                memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=slot, observed_at=now,
                version=reconciled.version + 1, supersedes_id=reconciled.id,
            )
            await MemoryPrecedenceService(session).supersede(reconciled, initial, transition_at=now)
            session.add(initial)
            await session.flush()

    async def replace_value(value: str):
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(TENANT_ID))
                service = MemoryPrecedenceService(session)
                old = await service.current_for_slot(
                    tenant_id=TENANT_ID, relationship_id=relationship_id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world, scenario_id=None,
                    slot_key=slot, at=datetime.now(UTC),
                )
                assert old is not None
                observed = datetime.now(UTC)
                new = _memory(memory_id=uuid4(), relationship_id=relationship_id,
                              user_id=user_id, character_id=character_id, slot=slot,
                              observed_at=observed, version=old.version + 1, supersedes_id=old.id)
                await service.supersede(old, new, transition_at=observed)
                session.add(new)
                await session.flush()
                return new.id

    await asyncio.gather(replace_value("B"), replace_value("C"))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            rows = list((await session.scalars(select(Memory).where(
                Memory.tenant_id == TENANT_ID, Memory.relationship_id == relationship_id,
                Memory.slot_key == slot,
            ).order_by(Memory.version))).all())
            active = [row for row in rows if row.lifecycle_state == LifecycleState.active]
            assert len(active) == 1
            assert [row.version for row in rows] == [1, 2, 3, 4]
            assert rows[1].supersedes_id == rows[0].id
            assert rows[2].supersedes_id == rows[1].id
            assert rows[3].supersedes_id == rows[2].id


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_current_and_historical_filters_precede_retrieval_ranking():
    user_id, character_id, relationship_id = await _owner("retrieval")
    now = datetime.now(UTC)
    memories = [
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"current.{uuid4().hex}", observed_at=now),
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"historical.{uuid4().hex}", observed_at=now-timedelta(days=2),
                lifecycle=LifecycleState.historical, valid_until=now-timedelta(days=1)),
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"legacy-null-end.{uuid4().hex}", observed_at=now-timedelta(days=3),
                lifecycle=LifecycleState.historical),
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"disputed.{uuid4().hex}", observed_at=now,
                evidence=EvidenceState.disputed),
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"expired.{uuid4().hex}", observed_at=now-timedelta(days=2),
                valid_until=now-timedelta(seconds=1)),
        _memory(memory_id=uuid4(), relationship_id=relationship_id, user_id=user_id,
                character_id=character_id, slot=f"future.{uuid4().hex}", observed_at=now,
                valid_from=now+timedelta(days=1)),
    ]
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add_all(memories)
            await session.flush()
            current_id, historical_id = memories[0].id, memories[1].id
            legacy_id = memories[2].id
            principal = PrincipalContext(TENANT_ID, user_id, user_id, "internal")
            base_request = RetrievalRequest(
                character_id=character_id, relationship_id=relationship_id,
                scope=Scope.real_world, scenario_id=None, query="works_at", as_of=now,
            )
            service = HybridRetrievalService(session, provider=object(), hmac_key=b"r" * 32)
            current_rows = list((await session.scalars(
                service._hard_filter(principal, base_request)
            )).all())
            current_ids = {row.id for row in current_rows}
            assert current_id in current_ids
            assert not {row.id for row in memories[1:]} & current_ids

            historical_request = RetrievalRequest(
                character_id=character_id, relationship_id=relationship_id,
                scope=Scope.real_world, scenario_id=None, query="works_at", as_of=now,
                temporal_mode=TemporalMode.HISTORICAL,
            )
            history_rows = list((await session.scalars(
                service._hard_filter(principal, historical_request)
            )).all())
            history_ids = {row.id for row in history_rows}
            assert {current_id, historical_id, legacy_id} <= history_ids
            assert not {row.id for row in memories[3:]} & history_ids
