"""PostgreSQL coverage for physical retention purge and legal-hold preservation."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import ensure_test_relationship
from sqlalchemy import select

from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    Conversation,
    ConversationSegment,
    DerivedMemoryView,
    EvidenceState,
    JobState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    Message,
    OutboxJob,
    RetentionPolicy,
    Scope,
    ShadowMemoryCandidate,
    Tenant,
    User,
)
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    content_aad,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
)
from app.messages.service import MessageIngestionService
from app.responses.pipeline import run_retention_cleanup

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("e61a4c7d-2145-4626-a2a9-000000000001")
SCENARIO_ZERO = UUID("00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio(loop_scope="module")
async def test_retention_physically_purges_content_and_respects_legal_hold():
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    suffix = uuid4().hex
    now = datetime.now(UTC)
    expired_ids = [uuid4() for _ in range(7)]
    user_id, character_id, conversation_id, control_id = expired_ids[:4]
    policy_id, message_id, memory_id = expired_ids[4:]
    hold_user_id, hold_conversation_id, hold_message_id, hold_memory_id = (uuid4() for _ in range(4))
    hold_policy_id, hold_character_id, hold_control_id = (uuid4() for _ in range(3))
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(Tenant(id=TENANT_ID))
                await session.flush()
            session.add_all(
                [
                    User(id=user_id, tenant_id=TENANT_ID, external_id=f"purge-{suffix}"),
                    Character(id=character_id, tenant_id=TENANT_ID, external_id=f"purge-{suffix}", name="Synthetic"),
                    User(id=hold_user_id, tenant_id=TENANT_ID, external_id=f"hold-{suffix}"),
                    Character(id=hold_character_id, tenant_id=TENANT_ID, external_id=f"hold-{suffix}", name="Synthetic hold"),
                    RetentionPolicy(id=policy_id, tenant_id=TENANT_ID, name=f"expire-{suffix}", message_days=0, memory_days=0, legal_hold=False),
                    RetentionPolicy(id=hold_policy_id, tenant_id=TENANT_ID, name=f"hold-{suffix}", message_days=0, memory_days=0, legal_hold=True),
                ]
            )
            await session.flush()
            consent = Consent(tenant_id=TENANT_ID, user_id=user_id, purpose="long_term_memory", granted_at=now, policy_version="test")
            hold_consent = Consent(tenant_id=TENANT_ID, user_id=hold_user_id, purpose="long_term_memory", granted_at=now, policy_version="test")
            session.add_all([consent, hold_consent])
            await session.flush()
            relationship = await ensure_test_relationship(
                session, TENANT_ID, user_id, character_id, Scope.real_world, None
            )
            hold_relationship = await ensure_test_relationship(
                session, TENANT_ID, hold_user_id, hold_character_id, Scope.real_world, None
            )
            conversation = Conversation(id=conversation_id, tenant_id=TENANT_ID, relationship_id=relationship.id, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, started_at=now-timedelta(days=2))
            control = MemoryControlState(id=control_id, tenant_id=TENANT_ID, relationship_id=relationship.id, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None)
            hold_conversation = Conversation(id=hold_conversation_id, tenant_id=TENANT_ID, relationship_id=hold_relationship.id, user_id=hold_user_id, character_id=hold_character_id, scope=Scope.real_world, scenario_id=None, started_at=now-timedelta(days=2))
            hold_control = MemoryControlState(id=hold_control_id, tenant_id=TENANT_ID, relationship_id=hold_relationship.id, user_id=hold_user_id, character_id=hold_character_id, scope=Scope.real_world, scenario_id=None)
            session.add_all([conversation, control, hold_conversation, hold_control])
            await session.flush()

            def make_message(mid, uid, cid, conv_id, retention_id, suffix_part):
                aad = MessageIngestionService._aad_for(TENANT_ID, uid, conv_id, mid)
                envelope = crypto.encrypt(f"private message {suffix_part}", aad)
                return Message(
                    id=mid, tenant_id=TENANT_ID, conversation_id=conv_id, user_id=uid,
                    character_id=cid, scope=Scope.real_world, scenario_id=None,
                    idempotency_key=f"purge-message-{suffix}-{suffix_part}", role="user",
                    content_ciphertext=envelope.ciphertext, content_nonce=envelope.nonce,
                    content_key_version=envelope.key_version,
                    retention_policy_id=retention_id, occurred_at=now-timedelta(days=2),
                )

            message = make_message(message_id, user_id, character_id, conversation_id, policy_id, "expired")
            hold_message = make_message(hold_message_id, hold_user_id, hold_character_id, hold_conversation_id, hold_policy_id, "held")
            session.add_all([message, hold_message])
            memory = Memory(
                id=memory_id, tenant_id=TENANT_ID, relationship_id=relationship.id, user_id=user_id, character_id=character_id,
                kind="semantic_fact", scope=Scope.real_world, scenario_id=None,
                slot_key=f"purge.{suffix}", predicate="private", value_text=encrypt_text_for_storage(
                    "private memory", content_aad(entity="memories", tenant_id=TENANT_ID, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, record_id=memory_id, field="value_text"), crypto=crypto),
                value_json=encrypt_json_for_storage({"text": "private json"}, content_aad(entity="memories", tenant_id=TENANT_ID, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, record_id=memory_id, field="value_json"), crypto=crypto), search_text="opaque-private-token",
                observed_at=now-timedelta(days=2), evidence_state=EvidenceState.confirmed,
                lifecycle_state=LifecycleState.active, confidence=0.9, importance=0.9,
                consent_id=consent.id, retention_policy_id=policy_id, created_epoch=0,
                embedding=[0.1] * 1536, embedding_model="test", embedded_at=now-timedelta(days=1),
            )
            hold_memory = Memory(
                id=hold_memory_id, tenant_id=TENANT_ID, relationship_id=hold_relationship.id, user_id=hold_user_id, character_id=hold_character_id,
                kind="semantic_fact", scope=Scope.real_world, scenario_id=None,
                slot_key=f"hold.{suffix}", predicate="private", value_text=encrypt_text_for_storage(
                    "held memory", content_aad(entity="memories", tenant_id=TENANT_ID, user_id=hold_user_id, character_id=hold_character_id, scope=Scope.real_world, scenario_id=None, record_id=hold_memory_id, field="value_text"), crypto=crypto),
                observed_at=now-timedelta(days=2), evidence_state=EvidenceState.confirmed,
                lifecycle_state=LifecycleState.active, confidence=0.9, importance=0.9,
                consent_id=hold_consent.id, retention_policy_id=hold_policy_id, created_epoch=0,
            )
            session.add_all([memory, hold_memory])
            await session.flush()
            segment_id = uuid4()
            session.add_all([
                MemorySource(tenant_id=TENANT_ID, memory_id=memory_id, message_id=message_id, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_key=SCENARIO_ZERO, relation="supports"),
                ConversationSegment(id=segment_id, tenant_id=TENANT_ID, conversation_id=conversation_id, started_at=now-timedelta(days=2), ended_at=now-timedelta(days=1), summary=encrypt_text_for_storage("private summary", content_aad(entity="conversation_segments", tenant_id=TENANT_ID, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, record_id=segment_id, field="summary"), crypto=crypto), keyphrases=encrypt_json_for_storage(["private phrase"], content_aad(entity="conversation_segments", tenant_id=TENANT_ID, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, record_id=segment_id, field="keyphrases"), crypto=crypto)),
            ])
            view_id = uuid4()
            view = DerivedMemoryView(id=view_id, tenant_id=TENANT_ID, relationship_id=relationship.id, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, view_type="conversation_summary", content=encrypt_json_for_storage({"text": "private view"}, content_aad(entity="derived_memory_views", tenant_id=TENANT_ID, user_id=user_id, character_id=character_id, scope=Scope.real_world, scenario_id=None, record_id=view_id, field="content"), crypto=crypto), parent_memory_ids=[str(memory_id)])
            session.add(view)
            candidate_id = uuid4()
            candidate_envelope = crypto.encrypt("private candidate", f"shadow:{TENANT_ID}:{user_id}:{message_id}:{candidate_id}".encode())
            candidate = ShadowMemoryCandidate(
                id=candidate_id, tenant_id=TENANT_ID, relationship_id=relationship.id, user_id=user_id, character_id=character_id,
                scope=Scope.real_world, scenario_id=None, source_message_id=message_id,
                memory_control_state_id=control_id, category="preference", subject="user",
                predicate="likes", value_ciphertext=candidate_envelope.ciphertext,
                value_nonce=candidate_envelope.nonce, value_key_version=candidate_envelope.key_version,
                value_fingerprint=f"fp-{suffix}", confidence=0.9,
                extractor_provider="test", extractor_model="test", extractor_prompt_version="v1",
                extractor_schema_version="v1", source_deletion_epoch=0, consent_id=consent.id,
                retention_policy_id=policy_id,
            )
            session.add(candidate)
            await session.flush()
            from app.jobs.outbox import OutboxRepository
            repository = OutboxRepository(session)
            await repository.enqueue(tenant_id=TENANT_ID, job_type="memory_extract", dedupe_key=f"purge-extract-{suffix}", aggregate_id=message_id, memory_control_state_id=control_id, payload={"message_id": str(message_id), "conversation_id": str(conversation_id), "code": "extract"})
            await repository.enqueue(tenant_id=TENANT_ID, job_type="memory_promote", dedupe_key=f"purge-promote-{suffix}", aggregate_id=candidate_id, memory_control_state_id=control_id, payload={"candidate_id": str(candidate_id)})
            deleted = await run_retention_cleanup(session, TENANT_ID)
            assert deleted == 2

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            purged_message = await session.get(Message, message_id)
            assert purged_message is not None and purged_message.deleted_at is not None
            assert purged_message.content_ciphertext is None
            purged_memory = await session.get(Memory, memory_id)
            assert purged_memory is not None and purged_memory.lifecycle_state == LifecycleState.deleted
            assert (purged_memory.value_text, purged_memory.value_json, purged_memory.search_text, purged_memory.embedding) == (None, None, None, None)
            segment = await session.scalar(select(ConversationSegment).where(ConversationSegment.conversation_id == conversation_id))
            assert segment is not None and segment.summary is None and segment.keyphrases is None
            view = await session.scalar(select(DerivedMemoryView).where(DerivedMemoryView.user_id == user_id, DerivedMemoryView.character_id == character_id))
            assert view is not None and view.content == {} and view.parent_memory_ids == []
            candidate = await session.get(ShadowMemoryCandidate, candidate_id)
            assert candidate is not None and candidate.status == "invalidated"
            assert (candidate.value_ciphertext, candidate.value_nonce, candidate.value_key_version) == (None, None, None)
            control = await session.get(MemoryControlState, control_id)
            assert control is not None and control.deletion_epoch == 1
            jobs = list((await session.scalars(select(OutboxJob).where(OutboxJob.dedupe_key.in_([f"purge-extract-{suffix}", f"purge-promote-{suffix}"])))).all())
            assert len(jobs) == 2 and all(job.state == JobState.dead for job in jobs), [
                (job.job_type, job.state, job.payload) for job in jobs
            ]
            held_message = await session.get(Message, hold_message_id)
            held_memory = await session.get(Memory, hold_memory_id)
            assert held_message is not None and held_message.deleted_at is None and held_message.content_ciphertext is not None
            assert held_memory is not None and held_memory.lifecycle_state == LifecycleState.active and held_memory.value_text is not None
