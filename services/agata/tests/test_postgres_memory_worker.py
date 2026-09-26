"""PostgreSQL end-to-end coverage for encrypted shadow extraction and default deny."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

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
    Message,
    OutboxJob,
    Scope,
    ShadowMemoryCandidate,
    Tenant,
    User,
)
from app.jobs.outbox import OutboxRepository
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.shadow import DeterministicFakeExtractor
from app.memory.worker import MemoryWorker
from app.messages.crypto import AeadCrypto, CiphertextEnvelope
from app.messages.service import MessageIngestionService

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
@pytest.mark.asyncio(loop_scope="module")
async def test_memory_worker_extracts_encrypted_shadow_and_keeps_promotion_default_denied():
    tenant_id = uuid4()
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    hmac_key = settings.shadow_candidate_hmac_key.get_secret_value().encode()
    suffix = uuid4().hex
    user_id, character_id, conversation_id, control_id, message_id = (
        uuid4() for _ in range(5)
    )
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            session.add_all(
                [
                    User(id=user_id, tenant_id=tenant_id, external_id=f"worker-{suffix}"),
                    Character(
                        id=character_id,
                        tenant_id=tenant_id,
                        external_id=f"worker-{suffix}",
                        name="Synthetic worker character",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Consent(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        purpose="long_term_memory",
                        granted_at=now,
                        policy_version="test-v1",
                    ),
                    Conversation(
                        id=conversation_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                        started_at=now,
                    ),
                    MemoryControlState(
                        id=control_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                    ),
                ]
            )
            await session.flush()
            encrypted = crypto.encrypt(
                "Я люблю чай",
                MessageIngestionService._aad_for(
                    tenant_id, user_id, conversation_id, message_id
                ),
            )
            session.add(
                Message(
                    id=message_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    idempotency_key=f"worker-message-{suffix}",
                    role="user",
                    content_ciphertext=encrypted.ciphertext,
                    content_nonce=encrypted.nonce,
                    content_key_version=encrypted.key_version,
                    occurred_at=now,
                )
            )
            await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="memory_extract",
                dedupe_key=f"worker-extract-{suffix}",
                aggregate_id=message_id,
                memory_control_state_id=control_id,
                creation_epoch=0,
                payload={"message_id": str(message_id)},
            )

    worker = MemoryWorker(
        SessionFactory,
        tenant_id=tenant_id,
        provider=HuggingFaceFreeEmbeddingProvider(),
        crypto=crypto,
        shadow_hmac_key=hmac_key,
        extraction_provider=DeterministicFakeExtractor(),
        batch_size=4,
    )
    first_cycle = await worker.run_once()
    assert first_cycle.claimed == 1 and first_cycle.completed == 1 and first_cycle.failed == 0
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            candidate = await session.scalar(
                select(ShadowMemoryCandidate).where(
                    ShadowMemoryCandidate.tenant_id == tenant_id,
                    ShadowMemoryCandidate.source_message_id == message_id,
                )
            )
            assert candidate is not None and candidate.status == "shadow"
            assert candidate.value_ciphertext and "чай".encode() not in candidate.value_ciphertext
            assert crypto.decrypt(
                CiphertextEnvelope(
                    candidate.value_ciphertext,
                    candidate.value_nonce,
                    candidate.value_key_version,
                ),
                f"shadow:{tenant_id}:{user_id}:{message_id}:{candidate.id}".encode(),
            ) == "чай"
            extract_job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"worker-extract-{suffix}")
            )
            promotion_job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"promote:{candidate.id}")
            )
            assert extract_job is not None and extract_job.state == JobState.completed
            assert promotion_job is not None and promotion_job.state == JobState.pending

    second_cycle = await worker.run_once()
    assert second_cycle.claimed == 1 and second_cycle.completed == 1 and second_cycle.failed == 0
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            candidate = await session.scalar(
                select(ShadowMemoryCandidate).where(
                    ShadowMemoryCandidate.tenant_id == tenant_id,
                    ShadowMemoryCandidate.source_message_id == message_id,
                )
            )
            memories = list(
                (
                    await session.scalars(
                        select(Memory).where(
                            Memory.tenant_id == tenant_id,
                            Memory.user_id == user_id,
                            Memory.character_id == character_id,
                            Memory.lifecycle_state == LifecycleState.active,
                        )
                    )
                ).all()
            )
            promotion_job = await session.scalar(
                select(OutboxJob).where(OutboxJob.dedupe_key == f"promote:{candidate.id}")
            )
            assert candidate is not None and candidate.status == "shadow"
            assert memories == []
            assert promotion_job is not None and promotion_job.state == JobState.completed
