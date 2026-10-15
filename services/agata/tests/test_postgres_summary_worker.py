"""Disposable PostgreSQL E2E for message → segment → canonical snapshots."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import ensure_test_relationship
from sqlalchemy import select

from app.api.internal import _background_inference_is_current
from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    Conversation,
    ConversationSegment,
    JobState,
    MemoryControlState,
    MemorySnapshot,
    Message,
    OutboxJob,
    RetentionPolicy,
    Scope,
    Tenant,
    User,
)
from app.jobs.outbox import OutboxRepository
from app.memory.retrieval import DeterministicFakeEmbeddingProvider
from app.memory.worker import MemoryWorker
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    decrypt_json_from_storage,
    decrypt_text_from_storage,
    memory_snapshot_content_aad,
    segment_summary_aad,
)
from app.messages.service import MessageIngestionService
from app.responses.pipeline import ProviderEnvelope

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("7508f36f-774a-48aa-8c6b-42f6f160415d")


class _SummaryProvider:
    def __init__(self):
        self.context = None
        self.parent_job_id = None

    async def complete(self, context):
        self.context = context
        return ProviderEnvelope("PRIVATE_SUMMARY_SENTINEL — проверенная тестовая сводка", (), "fake")

    async def complete_for_job(self, context, *, parent_job_id):
        self.parent_job_id = parent_job_id
        return await self.complete(context)


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_summary_worker_persists_chronological_encrypted_segment_and_snapshots():
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    user_id, character_id, conversation_id, control_id = (uuid4() for _ in range(4))
    suffix = uuid4().hex
    now = datetime.now(UTC)
    expired_message_id = None
    retained_message_ids = []
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            assert await session.get(Tenant, TENANT_ID) is not None
            session.add_all(
                [
                    User(id=user_id, tenant_id=TENANT_ID, external_id=f"summary-{suffix}"),
                    Character(
                        id=character_id,
                        tenant_id=TENANT_ID,
                        external_id=f"summary-{suffix}",
                        name="Synthetic Character",
                    ),
                ]
            )
            await session.flush()
            relationship = await ensure_test_relationship(
                session, TENANT_ID, user_id, character_id, Scope.real_world, None
            )
            conversation = Conversation(
                id=conversation_id,
                tenant_id=TENANT_ID,
                relationship_id=relationship.id,
                user_id=user_id,
                character_id=character_id,
                scope=Scope.real_world,
                scenario_id=None,
                started_at=now - timedelta(minutes=1),
            )
            control = MemoryControlState(
                id=control_id,
                tenant_id=TENANT_ID,
                relationship_id=relationship.id,
                user_id=user_id,
                character_id=character_id,
                scope=Scope.real_world,
                scenario_id=None,
            )
            session.add_all([conversation, control])
            session.add(Consent(
                tenant_id=TENANT_ID, user_id=user_id, purpose="chat_history",
                granted_at=now, policy_version="test-v1",
            ))
            session.add(Consent(
                tenant_id=TENANT_ID, user_id=user_id, purpose="long_term_memory",
                granted_at=now, policy_version="test-v1",
            ))
            retention = RetentionPolicy(
                tenant_id=TENANT_ID,
                name=f"summary-expired-{suffix}",
                message_days=1,
            )
            session.add(retention)
            await session.flush()
            for index in range(6):
                message_id = uuid4()
                if index == 0:
                    expired_message_id = message_id
                else:
                    retained_message_ids.append(message_id)
                envelope = crypto.encrypt(
                    "EXPIRED_RETENTION_SENTINEL" if index == 0 else f"synthetic message {index}",
                    MessageIngestionService._aad_for(
                        TENANT_ID, user_id, conversation_id, message_id
                    ),
                )
                session.add(
                    Message(
                        id=message_id,
                        tenant_id=TENANT_ID,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                        idempotency_key=f"summary-{suffix}-{index}",
                        role="user",
                        content_ciphertext=envelope.ciphertext,
                        content_nonce=envelope.nonce,
                        content_key_version=envelope.key_version,
                        occurred_at=(now - timedelta(days=3)) if index == 0 else now + timedelta(seconds=index),
                        retention_policy_id=retention.id if index == 0 else None,
                    )
                )
            await session.flush()
            await OutboxRepository(session).enqueue(
                tenant_id=TENANT_ID,
                job_type="conversation_summary",
                dedupe_key=f"summary-e2e:{suffix}",
                aggregate_id=conversation_id,
                memory_control_state_id=control_id,
                creation_epoch=0,
                payload={
                    "conversation_id": str(conversation_id),
                    "user_id": str(user_id),
                    "character_id": str(character_id),
                },
            )

    summary_provider = _SummaryProvider()
    worker = MemoryWorker(
        SessionFactory,
        tenant_id=TENANT_ID,
        provider=DeterministicFakeEmbeddingProvider(),
        crypto=crypto,
        shadow_hmac_key=b"s" * 32,
        summary_provider=summary_provider,
    )
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"summary-e2e:{suffix}")
            )
            assert job is not None and job.state == JobState.pending
            job.state = JobState.running
            job.claimed_at = datetime.now(UTC)
            job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
    await worker._process_summary(job)
    assert summary_provider.parent_job_id == job.id
    assert summary_provider.context is not None
    assert len(summary_provider.context.background_source_message_ids) == 5
    assert "EXPIRED_RETENTION_SENTINEL" not in summary_provider.context.current_message
    assert summary_provider.context.current_message.index("synthetic message 1") < (
        summary_provider.context.current_message.index("synthetic message 5")
    )

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            segment = await session.scalar(
                select(ConversationSegment).where(
                    ConversationSegment.conversation_id == conversation_id
                )
            )
            assert segment is not None
            assert segment.started_at == now + timedelta(seconds=1)
            assert segment.ended_at == now + timedelta(seconds=5)
            assert segment.summary is not None
            assert "PRIVATE_SUMMARY_SENTINEL" not in segment.summary
            conversation = await session.get(Conversation, conversation_id)
            assert conversation is not None
            plaintext = decrypt_text_from_storage(
                segment.summary, segment_summary_aad(segment, conversation), crypto=crypto
            )
            assert plaintext.startswith("PRIVATE_SUMMARY_SENTINEL")
            snapshot = await session.scalar(select(MemorySnapshot).where(
                MemorySnapshot.relationship_id == conversation.relationship_id,
                MemorySnapshot.snapshot_type == "conversation_summary",
            ))
            assert snapshot is not None
            assert snapshot.conversation_id == conversation_id
            assert "PRIVATE_SUMMARY_SENTINEL" not in str(snapshot.content)
            content = decrypt_json_from_storage(
                snapshot.content, memory_snapshot_content_aad(snapshot), crypto=crypto
            )
            assert content["summary"].startswith("PRIVATE_SUMMARY_SENTINEL")
            working = await session.scalar(select(MemorySnapshot).where(
                MemorySnapshot.relationship_id == conversation.relationship_id,
                MemorySnapshot.snapshot_type == "current_context_summary",
            ))
            assert working is not None and working.conversation_id == conversation_id
            relationship_summary = await session.scalar(select(MemorySnapshot).where(
                MemorySnapshot.relationship_id == conversation.relationship_id,
                MemorySnapshot.snapshot_type == "relationship_summary",
                MemorySnapshot.snapshot_key == "relationship",
            ))
            assert relationship_summary is not None
            assert relationship_summary.conversation_id is None
            relationship_content = decrypt_json_from_storage(
                relationship_summary.content,
                memory_snapshot_content_aad(relationship_summary),
                crypto=crypto,
            )
            assert relationship_content["recent_summary"].startswith("PRIVATE_SUMMARY_SENTINEL")
            job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"summary-e2e:{suffix}")
            )
            assert job is not None and job.state == JobState.completed

            # A message can expire after summary construction but before a
            # queued Product Worker child is claimed. Recheck every source ID.
            assert expired_message_id is not None
            parent = await OutboxRepository(session).enqueue(
                tenant_id=TENANT_ID,
                job_type="conversation_summary",
                dedupe_key=f"summary-retention-parent:{suffix}",
                aggregate_id=conversation_id,
                memory_control_state_id=control_id,
                creation_epoch=0,
                payload={
                    "conversation_id": str(conversation_id),
                    "user_id": str(user_id),
                    "character_id": str(character_id),
                },
            )
            parent.state = JobState.running
            parent.claimed_at = datetime.now(UTC)
            parent.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            child = OutboxJob(
                tenant_id=TENANT_ID,
                job_type="inference",
                aggregate_id=retained_message_ids[-1],
                dedupe_key=f"summary-retention-child:{suffix}",
                payload={
                    "purpose": "conversation_summary",
                    "parent_job_id": str(parent.id),
                    "source_message_ids": json.dumps(
                        [str(expired_message_id), str(retained_message_ids[-1])]
                    ),
                },
                state=JobState.running,
                claim_token="s" * 32,
                claimed_at=datetime.now(UTC),
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(child)
            await session.flush()
            assert not await _background_inference_is_current(
                session,
                tenant_id=TENANT_ID,
                job=child,
                purpose="conversation_summary",
            )
