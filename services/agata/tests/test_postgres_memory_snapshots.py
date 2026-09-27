"""Disposable PostgreSQL coverage for canonical snapshot safety and CAS refreshes."""

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
    MemorySnapshot,
    Scope,
    Tenant,
    User,
    UserCharacterSettings,
)
from app.memory.snapshots import MemorySnapshotService, snapshot_fingerprint
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    encrypt_text_for_storage,
    memory_snapshot_content_aad,
)

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("e61a4c7d-2145-4626-a2a9-000000000001")


@pytest.mark.asyncio(loop_scope="module")
async def test_relationship_note_provenance_consent_and_snapshot_generation_cas():
    crypto = AeadCrypto(
        encoded_key=os.environ["AGATA_MESSAGE_AEAD_KEY"],
        key_version=os.getenv("AGATA_MESSAGE_AEAD_KEY_VERSION", "v1"),
    )
    suffix = uuid4().hex
    now = datetime.now(UTC)
    user_id, character_id, memory_id, historical_memory_id, control_id = (uuid4() for _ in range(5))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(Tenant(id=TENANT_ID))
                await session.flush()
            user = User(id=user_id, tenant_id=TENANT_ID, external_id=f"snapshot-{suffix}")
            character = Character(
                id=character_id, tenant_id=TENANT_ID, external_id=f"snapshot-{suffix}", name="Synthetic"
            )
            session.add_all([user, character])
            await session.flush()
            session.add(Consent(
                tenant_id=TENANT_ID, user_id=user_id, purpose="long_term_memory",
                granted_at=now, policy_version="test-v1",
            ))
            await session.flush()
            relationship = await ensure_test_relationship(
                session, TENANT_ID, user_id, character_id, Scope.real_world, None
            )
            session.add_all([
                MemoryControlState(
                    id=control_id, tenant_id=TENANT_ID, relationship_id=relationship.id,
                    user_id=user_id, character_id=character_id, scope=Scope.real_world,
                    scenario_id=None,
                ),
                UserCharacterSettings(
                    tenant_id=TENANT_ID, user_id=user_id, character_id=character_id,
                    preferences={"preferred_name": "Alex"}, version=3,
                ),
            ])
            memory = Memory(
                id=memory_id, tenant_id=TENANT_ID, relationship_id=relationship.id,
                user_id=user_id, character_id=character_id, kind="semantic_fact",
                scope=Scope.real_world, scenario_id=None, slot_key=f"snapshot.fact.{suffix}",
                predicate="likes", value_text=encrypt_text_for_storage(
                    "tea", content_aad(
                        entity="memories", tenant_id=TENANT_ID, user_id=user_id,
                        character_id=character_id, scope=Scope.real_world, scenario_id=None,
                        record_id=memory_id, field="value_text",
                    ), crypto=crypto,
                ), observed_at=now, evidence_state=EvidenceState.confirmed,
                lifecycle_state=LifecycleState.active, confidence=0.98, importance=0.9,
                created_epoch=0,
            )
            historical_memory = Memory(
                id=historical_memory_id, tenant_id=TENANT_ID, relationship_id=relationship.id,
                user_id=user_id, character_id=character_id, kind="semantic_fact",
                scope=Scope.real_world, scenario_id=None, slot_key=memory.slot_key,
                predicate="works_at", value_text=encrypt_text_for_storage(
                    "Company A", content_aad(
                        entity="memories", tenant_id=TENANT_ID, user_id=user_id,
                        character_id=character_id, scope=Scope.real_world, scenario_id=None,
                        record_id=historical_memory_id, field="value_text",
                    ), crypto=crypto,
                ), observed_at=now-timedelta(days=30), valid_until=now-timedelta(days=1),
                evidence_state=EvidenceState.confirmed, lifecycle_state=LifecycleState.historical,
                confidence=0.98, importance=1.0, created_epoch=0,
            )
            session.add_all([memory, historical_memory])
            await session.flush()
            relationship_data = {
                "id": relationship.id, "tenant_id": TENANT_ID, "user_id": user_id,
                "character_id": character_id, "scope": Scope.real_world,
                "scenario_id": None,
            }
            note = await MemorySnapshotService(session, crypto).refresh_relationship_note(
                tenant_id=TENANT_ID, relationship=relationship,
            )
            assert note is not None
            assert note.generation == 1
            assert note.source_memory_ids == [str(memory_id)]
            note_payload = decrypt_json_from_storage(
                note.content, memory_snapshot_content_aad(note), crypto=crypto
            )
            assert note_payload["preferred_name"] == "Alex"
            assert note_payload["stable_facts"] == [{"predicate": "likes", "value": "tea"}]

            first = await MemorySnapshotService(session, crypto).upsert(
                tenant_id=TENANT_ID, relationship=relationship,
                snapshot_type="relationship_summary", snapshot_key="relationship",
                content={"recent_summary": "v1"},
                source_fingerprint=snapshot_fingerprint("initial", suffix),
                creation_epoch=0,
            )
            assert first is not None and first.generation == 1
            relationship_id = relationship.id

    detached_relationship = type("RelationshipOwner", (), relationship_data)()

    async def refresh(value: str):
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(TENANT_ID))
                return await MemorySnapshotService(session, crypto).upsert(
                    tenant_id=TENANT_ID, relationship=detached_relationship,
                    snapshot_type="relationship_summary", snapshot_key="relationship",
                    content={"recent_summary": value},
                    source_fingerprint=snapshot_fingerprint("concurrent", value),
                    creation_epoch=0, expected_generation=1,
                )

    concurrent_results = await asyncio.gather(refresh("new-a"), refresh("new-b"))
    assert all(row is not None and row.generation == 2 for row in concurrent_results)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            current = await session.scalar(select(MemorySnapshot).where(
                MemorySnapshot.tenant_id == TENANT_ID,
                MemorySnapshot.relationship_id == relationship_id,
                MemorySnapshot.snapshot_type == "relationship_summary",
            ))
            assert current is not None and current.generation == 2
            assert decrypt_json_from_storage(
                current.content, memory_snapshot_content_aad(current), crypto=crypto
            )["recent_summary"] in {"new-a", "new-b"}
            service = MemorySnapshotService(session, crypto)
            assert await service.resolve_current(
                tenant_id=TENANT_ID, relationship_id=relationship_id,
                snapshot_type="relationship_note", snapshot_key="relationship",
                user_id=user_id, required_consent="long_term_memory",
            ) is not None
            await service.invalidate_relationship(
                TENANT_ID, relationship_id, snapshot_types=("relationship_note",)
            )
            assert await service.resolve_current(
                tenant_id=TENANT_ID, relationship_id=relationship_id,
                snapshot_type="relationship_note", snapshot_key="relationship",
                user_id=user_id, required_consent="long_term_memory",
            ) is None
