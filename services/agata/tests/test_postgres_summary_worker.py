"""Disposable PostgreSQL end-to-end test for message → summary segment/view."""

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
    Conversation,
    ConversationSegment,
    DerivedMemoryView,
    JobState,
    MemoryControlState,
    Message,
    OutboxJob,
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
    derived_view_content_aad,
    segment_summary_aad,
)
from app.messages.service import MessageIngestionService
from app.responses.pipeline import ProviderEnvelope

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("7508f36f-774a-48aa-8c6b-42f6f160415d")


class _SummaryProvider:
    async def complete(self, _context):
        return ProviderEnvelope("PRIVATE_SUMMARY_SENTINEL — проверенная тестовая сводка", (), "fake")


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_summary_worker_persists_chronological_encrypted_segment_and_view():
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    user_id, character_id, conversation_id, control_id = (uuid4() for _ in range(4))
    suffix = uuid4().hex
    now = datetime.now(UTC)
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
            await session.flush()
            for index in range(5):
                message_id = uuid4()
                envelope = crypto.encrypt(
                    f"synthetic message {index}",
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
                        occurred_at=now + timedelta(seconds=index),
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

    worker = MemoryWorker(
        SessionFactory,
        tenant_id=TENANT_ID,
        provider=DeterministicFakeEmbeddingProvider(),
        crypto=crypto,
        shadow_hmac_key=b"s" * 32,
        summary_provider=_SummaryProvider(),
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

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            segment = await session.scalar(
                select(ConversationSegment).where(
                    ConversationSegment.conversation_id == conversation_id
                )
            )
            assert segment is not None
            assert segment.started_at == now
            assert segment.ended_at == now + timedelta(seconds=4)
            assert segment.summary is not None
            assert "PRIVATE_SUMMARY_SENTINEL" not in segment.summary
            conversation = await session.get(Conversation, conversation_id)
            assert conversation is not None
            plaintext = decrypt_text_from_storage(
                segment.summary, segment_summary_aad(segment, conversation), crypto=crypto
            )
            assert plaintext.startswith("PRIVATE_SUMMARY_SENTINEL")
            view = await session.scalar(
                select(DerivedMemoryView).where(
                    DerivedMemoryView.user_id == user_id,
                    DerivedMemoryView.character_id == character_id,
                )
            )
            assert view is not None
            assert "PRIVATE_SUMMARY_SENTINEL" not in str(view.content)
            content = decrypt_json_from_storage(
                view.content, derived_view_content_aad(view), crypto=crypto
            )
            assert content["latest_summary"].startswith("PRIVATE_SUMMARY_SENTINEL")
            job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"summary-e2e:{suffix}")
            )
            assert job is not None and job.state == JobState.completed
