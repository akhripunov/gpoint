"""PostgreSQL end-to-end coverage for encrypted shadow extraction and default deny."""

import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from conftest import ensure_test_relationship
from fastapi import FastAPI
from sqlalchemy import select
from starlette.requests import Request

from app.api.internal import (
    WorkerCompleteRequest,
    claim_inference_job,
    complete_inference_job,
)
from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    AutoWriteReleasePolicy,
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
from app.domain.principal import PrincipalContext
from app.jobs.outbox import OutboxRepository
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.mac_worker import MacWorkerExtractionProvider, MacWorkerMemoryTaskClient
from app.memory.promotion import AutoWritePromotionService
from app.memory.retrieval import (
    DeterministicFakeEmbeddingProvider,
    HybridRetrievalService,
    RetrievalRequest,
)
from app.memory.shadow import ProviderResult
from app.memory.worker import MemoryWorker
from app.messages.crypto import AeadCrypto, CiphertextEnvelope
from app.messages.service import MessageIngestionService
from app.responses.pipeline import ProviderEnvelope, ResponseContext

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")


class _EmptyExtractionProvider:
    provider_name = "synthetic"
    model_name = "synthetic-model"

    async def extract(self, _request):
        return ProviderResult((), "synthetic no-op")


@pytest.mark.asyncio(loop_scope="module")
async def test_memory_worker_claims_provider_jobs_one_at_a_time():
    tenant_id = uuid4()
    user_id, character_id, conversation_id, control_id = (uuid4() for _ in range(4))
    suffix = uuid4().hex
    now = datetime.now(UTC)
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            session.add_all([
                User(id=user_id, tenant_id=tenant_id, external_id=f"batch-{suffix}"),
                Character(
                    id=character_id, tenant_id=tenant_id,
                    external_id=f"batch-{suffix}", name="Synthetic batch character",
                ),
            ])
            await session.flush()
            relationship = await ensure_test_relationship(
                session, tenant_id, user_id, character_id, Scope.real_world, None
            )
            session.add_all([
                Conversation(
                    id=conversation_id, tenant_id=tenant_id,
                    relationship_id=relationship.id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None, started_at=now,
                ),
                MemoryControlState(
                    id=control_id, tenant_id=tenant_id,
                    relationship_id=relationship.id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None,
                ),
                Consent(
                    tenant_id=tenant_id, user_id=user_id,
                    purpose="long_term_memory", granted_at=now,
                    policy_version="test-v1",
                ),
            ])
            await session.flush()
            for index in range(2):
                message_id = uuid4()
                envelope = crypto.encrypt(
                    f"synthetic batch message {index}",
                    MessageIngestionService._aad_for(
                        tenant_id, user_id, conversation_id, message_id
                    ),
                )
                session.add(Message(
                    id=message_id, tenant_id=tenant_id,
                    conversation_id=conversation_id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None, idempotency_key=f"batch-{suffix}-{index}",
                    role="user", content_ciphertext=envelope.ciphertext,
                    content_nonce=envelope.nonce,
                    content_key_version=envelope.key_version,
                    occurred_at=now + timedelta(seconds=index), deleted_at=None,
                ))
                await session.flush()
                await OutboxRepository(session).enqueue(
                    tenant_id=tenant_id,
                    job_type="memory_extract",
                    dedupe_key=f"batch-parent:{suffix}:{index}",
                    aggregate_id=message_id,
                    memory_control_state_id=control_id,
                    creation_epoch=0,
                    payload={"message_id": str(message_id)},
                )

    worker = MemoryWorker(
        SessionFactory,
        tenant_id=tenant_id,
        provider=DeterministicFakeEmbeddingProvider(),
        crypto=crypto,
        shadow_hmac_key=b"b" * 32,
        extraction_provider=_EmptyExtractionProvider(),
        batch_size=4,
    )
    cycle = await worker.run_once()
    assert cycle.claimed == cycle.completed == 1
    async with SessionFactory() as session:
        await set_tenant_context(session, str(tenant_id))
        states = list((await session.execute(
            select(OutboxJob.state).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.dedupe_key.like(f"batch-parent:{suffix}:%"),
            )
        )).scalars())
        assert states.count(JobState.completed) == 1
        assert states.count(JobState.pending) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_mac_memory_task_claim_route_returns_summary_contract_until_delayed_completion(monkeypatch):
    settings = get_settings()
    # The shared synthetic database may contain pending jobs from earlier
    # tests. Keep this contract test independent of that test-order state.
    monkeypatch.setattr(
        "app.responses.mac_worker.get_settings",
        lambda: settings.model_copy(update={
            "max_pending_background_inference_jobs": 64,
            "max_pending_inference_jobs": 128,
        }),
    )
    tenant_id = UUID(settings.tenant_id)
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    user_id, character_id, conversation_id, control_id, message_id = (
        uuid4() for _ in range(5)
    )
    suffix = uuid4().hex
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            session.add_all([
                User(id=user_id, tenant_id=tenant_id, external_id=f"delayed-{suffix}"),
                Character(
                    id=character_id, tenant_id=tenant_id,
                    external_id=f"delayed-{suffix}", name="Synthetic delayed character",
                ),
            ])
            await session.flush()
            relationship = await ensure_test_relationship(
                session, tenant_id, user_id, character_id, Scope.real_world, None
            )
            session.add_all([
                Conversation(
                    id=conversation_id, tenant_id=tenant_id,
                    relationship_id=relationship.id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None, started_at=now,
                ),
                MemoryControlState(
                    id=control_id, tenant_id=tenant_id,
                    relationship_id=relationship.id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None,
                ),
                Consent(
                    tenant_id=tenant_id, user_id=user_id,
                    purpose="chat_history", granted_at=now,
                    policy_version="test-v1",
                ),
                    Consent(
                        tenant_id=tenant_id, user_id=user_id,
                        purpose="long_term_memory", granted_at=now,
                        policy_version="test-v1",
                    ),
                Message(
                    id=message_id, tenant_id=tenant_id,
                    conversation_id=conversation_id, user_id=user_id,
                    character_id=character_id, scope=Scope.real_world,
                    scenario_id=None, idempotency_key=f"delayed-{suffix}",
                    role="user", occurred_at=now, deleted_at=None,
                ),
            ])
            await session.flush()
            parent = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="conversation_summary",
                dedupe_key=f"delayed-parent:{suffix}",
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
            parent.claimed_at = now
            parent.lease_expires_at = now + timedelta(seconds=60)
            parent_id = parent.id

    context = ResponseContext(
        profile_version=0,
        route_tuple=("", "", "", "", ""),
        current_message="synthetic extraction payload",
        source_message_id=message_id,
        source_idempotency_key=f"delayed-{suffix}",
        background_source_message_ids=(message_id,),
    )
    client = MacWorkerMemoryTaskClient(
        SessionFactory, crypto=crypto, tenant_id=tenant_id,
        model=settings.llm_model, wait_seconds=1.0,
    )
    dedupe_key = f"memory-task:conversation_summary:{parent_id}"

    async def claim_only_this_test_task(repository, **_kwargs):
        child = await repository.session.scalar(
            select(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.dedupe_key == dedupe_key,
                OutboxJob.state == JobState.pending,
            )
            .with_for_update(skip_locked=True)
        )
        if child is None:
            return []
        child.state = JobState.running
        child.attempts += 1
        child.claimed_at = datetime.now(UTC)
        child.lease_expires_at = datetime.now(UTC) + timedelta(seconds=300)
        child.claim_token = uuid4().hex
        return [child]

    monkeypatch.setattr(OutboxRepository, "claim", claim_only_this_test_task)

    async def delayed_product_worker():
        for _ in range(100):
            async with SessionFactory() as session:
                await set_tenant_context(session, str(tenant_id))
                child = await session.scalar(select(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.dedupe_key == dedupe_key,
                ))
                if child is not None:
                    child_id = child.id
                    assert child.payload.get("purpose") == "conversation_summary"
                    assert child.payload.get("source_message_ids") == json.dumps(
                        [str(message_id)], separators=(",", ":")
                    )
                    break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("memory inference child was not enqueued")

        request = Request({
            "type": "http", "app": FastAPI(), "headers": [], "query_string": b"",
            "server": ("test", 80), "client": ("test", 1), "scheme": "http",
        })
        async with SessionFactory() as session:
            claim = await claim_inference_job(request, session)
        worker_job = claim["job"]
        assert worker_job["id"] == str(child_id)
        assert worker_job["purpose"] == "conversation_summary"
        assert worker_job["target"] == "GPOINT"
        assert worker_job["timeout_seconds"] == settings.background_inference_timeout_seconds
        assert worker_job["request"]["user_message"] == "synthetic extraction payload"
        lease_token = worker_job["leaseToken"]
        await asyncio.sleep(1.2)
        async with SessionFactory() as session:
            result = await complete_inference_job(
                WorkerCompleteRequest(
                    job_id=child_id,
                    lease_token=lease_token,
                    text='{"abstain_reason":"uncertain","candidates":[]}',
                    model=settings.llm_model,
                ),
                session,
            )
        assert result["completed"] is True and not result.get("discarded", False)

    provider_result, _ = await asyncio.gather(
        client.complete(context, purpose="conversation_summary", parent_job_id=parent_id),
        delayed_product_worker(),
    )
    assert provider_result.text == '{"abstain_reason":"uncertain","candidates":[]}'
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            parent = await session.scalar(select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.id == parent_id,
            ))
            assert parent is not None and parent.state == JobState.running
            assert parent.lease_expires_at is not None and parent.lease_expires_at > datetime.now(UTC)
            await OutboxRepository(session).complete_memory_job(parent)

    # Race a second completion against a consent revocation holding the same
    # User → control → consent locks used by the production consent endpoint.
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            parent = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="memory_extract",
                dedupe_key=f"delayed-revoked-parent:{suffix}",
                aggregate_id=message_id,
                memory_control_state_id=control_id,
                creation_epoch=0,
                payload={"message_id": str(message_id)},
            )
            parent.state = JobState.running
            parent.claimed_at = datetime.now(UTC)
            parent.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
            revoked_parent_id = parent.id
            revoked_child = OutboxJob(
                tenant_id=tenant_id,
                job_type="inference",
                aggregate_id=message_id,
                dedupe_key=f"delayed-revoked-child:{suffix}",
                payload={
                    "purpose": "memory_extract",
                    "parent_job_id": str(revoked_parent_id),
                },
                state=JobState.running,
                claim_token=uuid4().hex,
                claimed_at=datetime.now(UTC),
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(revoked_child)
            await session.flush()
            revoked_child_id = revoked_child.id
            revoked_lease_token = revoked_child.claim_token

    async with SessionFactory() as revoke_session:
        async with revoke_session.begin():
            await set_tenant_context(revoke_session, str(tenant_id))
            await revoke_session.scalar(select(User).where(
                User.tenant_id == tenant_id,
                User.id == user_id,
            ).with_for_update())
            await revoke_session.scalar(select(MemoryControlState).where(
                MemoryControlState.tenant_id == tenant_id,
                MemoryControlState.id == control_id,
            ).with_for_update())
            consent = await revoke_session.scalar(select(Consent).where(
                Consent.tenant_id == tenant_id,
                Consent.user_id == user_id,
                Consent.purpose == "long_term_memory",
                Consent.revoked_at.is_(None),
            ).with_for_update())
            assert consent is not None
            async def complete_during_revocation():
                async with SessionFactory() as completion_session:
                    return await complete_inference_job(
                        WorkerCompleteRequest(
                            job_id=revoked_child_id,
                            lease_token=revoked_lease_token,
                            text='{"abstain_reason":"uncertain","candidates":[]}',
                            model=settings.llm_model,
                        ),
                        completion_session,
                    )

            completion = asyncio.create_task(complete_during_revocation())
            await asyncio.sleep(0.1)
            consent.revoked_at = datetime.now(UTC)
    rejected = await completion
    assert rejected["discarded"] is True
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            revoked_child = await session.scalar(select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.id == revoked_child_id,
            ))
            assert revoked_child is not None and revoked_child.state == JobState.dead
            assert "result_ciphertext" not in revoked_child.payload


class _MockMacMemoryClient:
    model = "codex-default"

    async def complete(self, context, *, purpose, parent_job_id):
        assert purpose == "memory_extract"
        assert parent_job_id
        payload = json.loads(context.current_message)
        assert payload["schema_version"] == "shadow-schema-v1"
        assert payload["text"] == "Я люблю чай"
        return ProviderEnvelope(
            '{"abstain_reason":null,"candidates":[{"category":"preference",'
            '"subject":"user","predicate":"likes","value":"чай",'
            '"confidence":0.95,"temporal_hint":"stable"}]}',
            (),
            "mocked-product-worker",
        )
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
            relationship = await ensure_test_relationship(
                session, tenant_id, user_id, character_id, Scope.real_world, None
            )
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
                        relationship_id=relationship.id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                        started_at=now,
                    ),
                    MemoryControlState(
                        id=control_id,
                        tenant_id=tenant_id,
                        relationship_id=relationship.id,
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
        extraction_provider=MacWorkerExtractionProvider(_MockMacMemoryClient()),
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

            # This enabled policy is synthetic and exists only in the disposable test DB.
            session.add(AutoWriteReleasePolicy(
                tenant_id=tenant_id,
                category="preference",
                extractor_provider="mac_worker",
                extractor_model="codex-default",
                extractor_prompt_version="shadow-prompt-v1",
                extractor_schema_version="shadow-schema-v1",
                policy_version="promotion-v1",
                min_confidence=0.8,
                min_precision=0.9,
                min_labelled_support=10,
                measured_precision=0.99,
                measured_support=100,
                enabled=True,
            ))
            await session.flush()
            promoted = await AutoWritePromotionService(
                session, crypto, hmac_key=hmac_key
            ).promote(tenant_id=tenant_id, candidate_id=candidate.id)
            assert promoted.status == "promoted" and promoted.memory_id is not None

    async with SessionFactory() as session:
        result = await HybridRetrievalService(
            session,
            DeterministicFakeEmbeddingProvider(),
            hmac_key=base64.b64decode(settings.retrieval_query_hmac_key.get_secret_value()),
        ).retrieve(
            PrincipalContext(tenant_id, user_id, user_id, "internal"),
            RetrievalRequest(
                character_id, relationship.id, Scope.real_world, None, "чай", now
            ),
        )
        assert any(item.value == "чай" for item in result.results)
