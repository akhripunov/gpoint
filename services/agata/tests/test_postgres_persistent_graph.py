"""Disposable PostgreSQL checks for graph persistence, privacy and dedupe."""
import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new as hmac_new
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.api.internal import UserConsentRequest, sync_user_consent
from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    Conversation,
    JobState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemoryEntity,
    MemoryGraphEvidence,
    MemoryRelation,
    MemorySource,
    Message,
    OutboxJob,
    RoleplayScenario,
    Scope,
    SuppressionRule,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.domain.relationships import resolve_relationship
from app.jobs.outbox import OutboxRepository
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.persistent_graph import PersistentMemoryGraphService, enqueue_memory_graph_sync
from app.memory.worker import MemoryWorker
from app.messages.crypto import AeadCrypto, derive_hmac_subkey
from app.messages.protected_content import content_aad, encrypt_text_for_storage
from app.messages.service import MessageIngestionService

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")


@pytest.mark.asyncio(loop_scope="module")
async def test_persistent_graph_projection_is_evidence_backed_private_and_scoped(monkeypatch):
    tenant_id, user_id, character_id = uuid4(), uuid4(), uuid4()
    other_tenant_id, other_user_id, other_character_id = uuid4(), uuid4(), uuid4()
    suffix = uuid4().hex
    now = datetime.now(UTC)
    crypto = AeadCrypto(encoded_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", key_version="v1")
    graph_root = b"p24-graph-test-hmac-root-000000000000000000000000000000"
    suppression_root = b"p24-explicit-suppression-root-000000000000000000000000000000"
    assert len(graph_root) >= 32
    created = []
    relationships = []
    seeds = []
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            session.add(Tenant(id=other_tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            user = User(id=user_id, tenant_id=tenant_id, external_id=f"p24-{suffix}")
            character = Character(id=character_id, tenant_id=tenant_id, external_id=f"p24-{suffix}", name="P24 test")
            session.add_all([user, character])
            await session.flush()
            other_user = User(id=other_user_id, tenant_id=other_tenant_id, external_id=f"p24-other-{suffix}")
            other_character = Character(
                id=other_character_id, tenant_id=other_tenant_id,
                external_id=f"p24-other-{suffix}", name="P24 other tenant",
            )
            session.add_all([other_user, other_character])
            await session.flush()
            other_principal = PrincipalContext(other_tenant_id, other_user_id, other_user_id, "internal")
            other_relationship = await resolve_relationship(
                session, other_principal, other_character_id, Scope.real_world, None
            )
            session.add(MemoryEntity(
                tenant_id=other_tenant_id, relationship_id=other_relationship.id,
                user_id=other_user_id, character_id=other_character_id, scope=Scope.real_world,
                scenario_id=None, entity_type="user", entity_key_hmac="hmac-sha256:v1:" + "a" * 64,
                created_epoch=0,
            ))
            await session.flush()
            principal = PrincipalContext(tenant_id, user_id, user_id, "internal")
            relation_a = await resolve_relationship(session, principal, character_id, Scope.real_world, None)
            # Relationship B uses a separately-owned character, preserving real FK ownership.
            char_b = Character(tenant_id=tenant_id, external_id=f"p24-b-{suffix}", name="P24 B")
            session.add(char_b)
            await session.flush()
            relation_b = await resolve_relationship(session, principal, char_b.id, Scope.real_world, None)
            scenario = RoleplayScenario(
                tenant_id=tenant_id, user_id=user_id, character_id=character_id,
                name="P24 isolated scenario", started_at=now,
            )
            session.add(scenario)
            await session.flush()
            relation_c = await resolve_relationship(
                session, principal, character_id, Scope.roleplay, scenario.id
            )
            relationships = [relation_a, relation_b, relation_c]
            consent = Consent(tenant_id=tenant_id, user_id=user_id, purpose="long_term_memory", granted_at=now, policy_version="p24-test")
            session.add(consent)
            await session.flush()
            for rel_index, relationship in enumerate(relationships):
                control = MemoryControlState(
                    tenant_id=tenant_id, relationship_id=relationship.id, user_id=user_id,
                    character_id=relationship.character_id, scope=relationship.scope,
                    scenario_id=relationship.scenario_id,
                )
                session.add(control)
                await session.flush()
                values = ("Coffee", " coffee ", "GRAPH_SECRET_SENTINEL_92C", "BLOCKED_SUPPRESSED") if rel_index == 0 else ("Coffee",)
                for value_index, value in enumerate(values):
                    message_id, memory_id, conversation_id = uuid4(), uuid4(), uuid4()
                    session.add(Conversation(
                        id=conversation_id, tenant_id=tenant_id, relationship_id=relationship.id,
                        user_id=user_id, character_id=relationship.character_id, scope=relationship.scope,
                        scenario_id=relationship.scenario_id, started_at=now,
                    ))
                    await session.flush()
                    message_envelope = crypto.encrypt(
                        "synthetic source",
                        MessageIngestionService._aad_for(tenant_id, user_id, conversation_id, message_id),
                    )
                    message = Message(
                        id=message_id, tenant_id=tenant_id, conversation_id=conversation_id,
                        user_id=user_id, character_id=relationship.character_id, scope=relationship.scope,
                        scenario_id=relationship.scenario_id, idempotency_key=f"p24-{suffix}-{rel_index}-{value_index}",
                        role="user", content_ciphertext=message_envelope.ciphertext,
                        content_nonce=message_envelope.nonce, content_key_version=message_envelope.key_version,
                        occurred_at=now,
                    )
                    session.add(message)
                    await session.flush()
                    envelope = encrypt_text_for_storage(
                        value,
                        content_aad(entity="memories", tenant_id=tenant_id, user_id=user_id,
                                    character_id=relationship.character_id, scope=relationship.scope,
                                    scenario_id=relationship.scenario_id, record_id=memory_id, field="value_text"),
                        crypto=crypto,
                    )
                    memory = Memory(
                        id=memory_id, tenant_id=tenant_id, relationship_id=relationship.id,
                        user_id=user_id, character_id=relationship.character_id, kind="semantic_fact",
                        scope=relationship.scope, scenario_id=relationship.scenario_id, slot_key=f"user.likes#v1:{value_index}",
                        subject_type="user", predicate="likes", value_text=envelope,
                        observed_at=now, evidence_state="explicit", lifecycle_state=LifecycleState.active,
                        confidence=1.0, importance=0.5, consent_id=consent.id, created_epoch=0,
                    )
                    if rel_index == 0 and value_index == 0:
                        memory.lifecycle_state = LifecycleState.historical
                        memory.valid_until = now
                    session.add(memory)
                    session.add(MemorySource(
                        tenant_id=tenant_id, memory_id=memory_id, message_id=message_id,
                            user_id=user_id, character_id=relationship.character_id,
                            scope=relationship.scope, scenario_key=relationship.scenario_key, relation="supports",
                    ))
                    created.append((memory_id, relationship.id, control.id))
                    seeds.append(SimpleNamespace(memory_id=memory_id, version=1, value=value, predicate="likes"))
                    if value == "BLOCKED_SUPPRESSED":
                        suppression_key = derive_hmac_subkey(
                            suppression_root, b"gptgirls/memory-suppression/v1"
                        )
                        session.add(SuppressionRule(
                            tenant_id=tenant_id, relationship_id=relationship.id, user_id=user_id,
                            character_id=relationship.character_id, scope=Scope.real_world,
                            scenario_id=None, rule_type="slot_value",
                            slot_hmac=hmac_new(suppression_key, b"user.likes", sha256).hexdigest(),
                            value_hmac=hmac_new(suppression_key, value.encode(), sha256).hexdigest(),
                            key_version="p24-test", reason="test_suppression",
                        ))
            await session.flush()

    # Two exact memories with conservatively equivalent value collapse to one target
    # entity and one relation within relationship A; the same value in B is isolated.
    memory_id, rel_id, control_id = created[0]
    async def concurrent_projection(item):
        item_id, _, item_control = item
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(tenant_id))
                await PersistentMemoryGraphService(
                    session, crypto=crypto, hmac_root=graph_root
                ).project_memory(
                    memory_id=item_id, memory_version=1, tenant_id=tenant_id,
                    control_id=item_control, creation_epoch=0,
                )

    await asyncio.gather(concurrent_projection(created[0]), concurrent_projection(created[0]))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            service = PersistentMemoryGraphService(
                session, crypto=crypto, hmac_root=graph_root,
                suppression_hmac_roots=(graph_root, suppression_root),
            )
            for item_id, _, item_control in created[1:]:
                await service.project_memory(memory_id=item_id, memory_version=1, tenant_id=tenant_id, control_id=item_control, creation_epoch=0)
            await service.project_memory(memory_id=memory_id, memory_version=1, tenant_id=tenant_id, control_id=control_id, creation_epoch=0)
            assert await service.project_memory(
                memory_id=created[3][0], memory_version=1, tenant_id=tenant_id,
                control_id=created[3][2], creation_epoch=0,
            ) is False
            counts = (
                await session.scalar(select(func.count()).select_from(MemoryEntity).where(MemoryEntity.tenant_id == tenant_id)),
                await session.scalar(select(func.count()).select_from(MemoryRelation).where(MemoryRelation.tenant_id == tenant_id)),
                await session.scalar(select(func.count()).select_from(MemoryGraphEvidence).where(MemoryGraphEvidence.tenant_id == tenant_id)),
            )
            assert counts == (7, 4, 10)
            assert await session.scalar(select(func.count()).select_from(MemoryEntity).where(
                MemoryEntity.tenant_id == other_tenant_id
            )) == 1
            stored = await session.execute(text(
                "SELECT entity_key_hmac FROM agata.memory_entities WHERE tenant_id=:tenant"
            ), {"tenant": tenant_id})
            fingerprints = [row.entity_key_hmac for row in stored]
            assert all(value.startswith("hmac-sha256:v1:") and "Coffee" not in value for value in fingerprints)
            assert len(set(fingerprints)) == 7
            raw = await session.execute(text(
                "SELECT entity_key_hmac || ' ' || entity_type FROM agata.memory_entities WHERE tenant_id=:tenant"
            ), {"tenant": tenant_id})
            assert "GRAPH_SECRET_SENTINEL_92C" not in " ".join(row[0] for row in raw)
            rebuilt = await service.rebuild_relationship(
                tenant_id=tenant_id, relationship_id=relationships[0].id,
                control_id=control_id, limit=100,
            )
            assert rebuilt == 3
            rebuilt_counts = (
                await session.scalar(select(func.count()).select_from(MemoryEntity).where(MemoryEntity.tenant_id == tenant_id)),
                await session.scalar(select(func.count()).select_from(MemoryRelation).where(MemoryRelation.tenant_id == tenant_id)),
                await session.scalar(select(func.count()).select_from(MemoryGraphEvidence).where(MemoryGraphEvidence.tenant_id == tenant_id)),
            )
            assert rebuilt_counts == counts
            current_context = await service.load_context(
                tenant_id=tenant_id, relationship_id=relationships[0].id, user_id=user_id,
                character_id=relationships[0].character_id, scope=Scope.real_world,
                scenario_key=relationships[0].scenario_key, as_of=now,
                temporal_mode="current", seeds=tuple(seeds[:3]),
            )
            historical_context = await service.load_context(
                tenant_id=tenant_id, relationship_id=relationships[0].id, user_id=user_id,
                character_id=relationships[0].character_id, scope=Scope.real_world,
                scenario_key=relationships[0].scenario_key, as_of=now,
                temporal_mode="historical", seeds=tuple(seeds[:3]),
            )
            assert current_context == ("User likes: coffee", "User likes: GRAPH_SECRET_SENTINEL_92C")
            assert historical_context == ("User likes: Coffee",)
            relation_a = await session.scalar(select(MemoryRelation).where(
                MemoryRelation.tenant_id == tenant_id,
                MemoryRelation.relationship_id == relationships[0].id,
            ).order_by(MemoryRelation.id).limit(1))
            relation_b_target = await session.scalar(select(MemoryRelation.target_entity_id).where(
                MemoryRelation.tenant_id == tenant_id,
                MemoryRelation.relationship_id == relationships[1].id,
            ).limit(1))
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(MemoryRelation(
                        tenant_id=tenant_id, relationship_id=relationships[0].id,
                        user_id=user_id, character_id=relationships[0].character_id,
                        scope=Scope.real_world, scenario_id=None,
                        source_entity_id=relation_a.source_entity_id,
                        target_entity_id=relation_b_target, relation_type="likes", created_epoch=0,
                    ))
                    await session.flush()
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(MemoryGraphEvidence(
                        tenant_id=tenant_id, relationship_id=relationships[0].id,
                        user_id=user_id, character_id=relationships[0].character_id,
                        scope=Scope.real_world, scenario_id=None, memory_id=memory_id,
                        memory_version=999, relation_id=relation_a.id,
                        evidence_role="supports_relation", created_epoch=0,
                    ))
                    await session.flush()
            stored_memory = await session.get(Memory, memory_id)
            stored_control = await session.get(MemoryControlState, control_id)
            await enqueue_memory_graph_sync(session, memory=stored_memory, control=stored_control)

    # Force a non-owner, non-superuser role through the migration's row-level policy.
    runtime_role = f"p24_rls_{uuid4().hex[:12]}"
    graph_tables = "agata.memory_entities, agata.memory_relations, agata.memory_graph_evidence"
    async with SessionFactory() as admin_session:
        async with admin_session.begin():
            await admin_session.execute(text(f'CREATE ROLE "{runtime_role}" NOLOGIN'))
            await admin_session.execute(text(f'GRANT USAGE ON SCHEMA agata TO "{runtime_role}"'))
            await admin_session.execute(text(f'GRANT EXECUTE ON FUNCTION agata.runtime_tenant_id() TO "{runtime_role}"'))
            await admin_session.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {graph_tables} TO "{runtime_role}"'))
    try:
        async with SessionFactory() as rls_session:
            async with rls_session.begin():
                await rls_session.execute(text(f'SET LOCAL ROLE "{runtime_role}"'))
                await set_tenant_context(rls_session, str(tenant_id))
                hidden = await rls_session.scalar(text(
                    "SELECT count(*) FROM agata.memory_entities WHERE tenant_id=:tenant"
                ), {"tenant": other_tenant_id})
                assert hidden == 0
                with pytest.raises(DBAPIError):
                    async with rls_session.begin_nested():
                        await rls_session.execute(text(
                            "INSERT INTO agata.memory_entities "
                            "(tenant_id,relationship_id,user_id,character_id,scope,entity_type,entity_key_hmac,created_epoch) "
                            "VALUES (:tenant,:relationship,:user,:character,'real_world','user',:fingerprint,0)"
                        ), {
                            "tenant": other_tenant_id, "relationship": other_relationship.id,
                            "user": other_user_id, "character": other_character_id,
                            "fingerprint": "hmac-sha256:v1:" + "b" * 64,
                        })
                update_result = await rls_session.execute(text(
                    "UPDATE agata.memory_entities SET entity_type='place' "
                    "WHERE tenant_id=:tenant"
                ), {"tenant": other_tenant_id})
                delete_result = await rls_session.execute(text(
                    "DELETE FROM agata.memory_entities WHERE tenant_id=:tenant"
                ), {"tenant": other_tenant_id})
                assert update_result.rowcount == 0 and delete_result.rowcount == 0
    finally:
        async with SessionFactory() as admin_session:
            async with admin_session.begin():
                await admin_session.execute(text(f'DROP OWNED BY "{runtime_role}"'))
                await admin_session.execute(text(f'DROP ROLE "{runtime_role}"'))

    worker = MemoryWorker(
        SessionFactory, tenant_id=tenant_id, provider=HuggingFaceFreeEmbeddingProvider(),
        crypto=crypto, shadow_hmac_key=graph_root, batch_size=2,
    )
    cycle = await worker.run_once()
    assert (cycle.claimed, cycle.completed, cycle.failed) == (1, 1, 0)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            job = await session.scalar(select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id, OutboxJob.job_type == "memory_graph_sync"
            ))
            assert job is not None and job.state == JobState.completed
            assert await session.scalar(select(func.count()).select_from(MemoryEntity).where(MemoryEntity.tenant_id == tenant_id)) == 7

    # Model the forget epoch transition: remove derived rows, then prove an old
    # outbox payload cannot recreate them after the epoch advances.
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            control = await session.scalar(select(MemoryControlState).where(
                MemoryControlState.tenant_id == tenant_id,
                MemoryControlState.id == control_id,
            ).with_for_update())
            control.deletion_epoch = 1
            await session.execute(text("DELETE FROM agata.memory_graph_evidence WHERE tenant_id=:tenant AND relationship_id=:relationship"), {"tenant": tenant_id, "relationship": relationships[0].id})
            await session.execute(text("DELETE FROM agata.memory_relations WHERE tenant_id=:tenant AND relationship_id=:relationship"), {"tenant": tenant_id, "relationship": relationships[0].id})
            await session.execute(text("DELETE FROM agata.memory_entities WHERE tenant_id=:tenant AND relationship_id=:relationship"), {"tenant": tenant_id, "relationship": relationships[0].id})
            await OutboxRepository(session).enqueue(
                tenant_id=tenant_id, job_type="memory_graph_sync", dedupe_key=f"stale-graph-{suffix}",
                aggregate_id=memory_id, memory_control_state_id=control.id, creation_epoch=0,
                payload={"memory_id": str(memory_id), "memory_version": "1"},
            )
    stale_cycle = await worker.run_once()
    assert (stale_cycle.claimed, stale_cycle.failed) == (1, 0)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            stale_job = await session.scalar(select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id, OutboxJob.dedupe_key == f"stale-graph-{suffix}"
            ))
            assert stale_job is not None and stale_job.state == JobState.dead
            assert await session.scalar(select(func.count()).select_from(MemoryGraphEvidence).where(
                MemoryGraphEvidence.tenant_id == tenant_id,
                MemoryGraphEvidence.relationship_id == relationships[0].id,
            )) == 0

    # Exercise the real consent-revocation handler: it purges derived graph rows
    # and makes an already queued projection terminal before the transaction ends.
    consent_memory_id, _, consent_control_id = created[4]
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            consent_memory = await session.get(Memory, consent_memory_id)
            consent_control = await session.get(MemoryControlState, consent_control_id)
            await enqueue_memory_graph_sync(
                session, memory=consent_memory, control=consent_control
            )
    monkeypatch.setenv("AGATA_TENANT_ID", str(tenant_id))
    get_settings.cache_clear()
    try:
        async with SessionFactory() as session:
            result = await sync_user_consent(
                f"p24-{suffix}",
                UserConsentRequest(
                    chat_history=True, long_term_memory=False, age_band="unknown",
                    age_status="pending", age_method="p24-test", jurisdiction="US",
                    disclosure_accepted=False, disclosure_receipt_id=f"p24-{suffix}",
                ),
                session,
            )
            assert result["user_id"] == str(user_id)
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(tenant_id))
                for model in (MemoryGraphEvidence, MemoryRelation, MemoryEntity):
                    assert await session.scalar(select(func.count()).select_from(model).where(
                        model.tenant_id == tenant_id
                    )) == 0
                consent_job = await session.scalar(select(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.dedupe_key == f"graph:{consent_memory_id}:1:0",
                ))
                assert consent_job is not None and consent_job.state == JobState.dead
    finally:
        get_settings.cache_clear()
