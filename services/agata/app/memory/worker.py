"""Durable worker for epoch-guarded memory embedding jobs."""

from __future__ import annotations

import asyncio
import base64
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    Conversation,
    ConversationSegment,
    MemoryControlState,
    MemorySnapshot,
    Message,
    OutboxJob,
    Relationship,
    User,
)
from app.domain.principal import PrincipalContext
from app.jobs.outbox import OutboxRepository, StaleCreationEpoch
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.persistent_graph import PersistentMemoryGraphService
from app.memory.promotion import AutoWritePromotionService
from app.memory.retrieval import EmbeddingWorker, HybridRetrievalService
from app.memory.segments import segment_hierarchy, segment_interval
from app.memory.shadow import (
    ExtractionProvider,
    ShadowExtractionWorker,
    StructuredExtractionProvider,
)
from app.memory.shared_history import SharedHistoryContext, SharedHistoryService
from app.memory.snapshots import MemorySnapshotService, snapshot_fingerprint
from app.messages.crypto import AeadCrypto, CiphertextEnvelope
from app.messages.protected_content import (
    content_aad,
    decrypt_text_from_storage,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
    segment_summary_aad,
)
from app.messages.service import MessageIngestionService
from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.pipeline import (
    ProviderEnvelope,
    ResponseContext,
    ResponseOrchestrator,
    ResponseRequest,
)
from app.responses.provider_factory import build_response_provider

_HEARTBEAT_PATH = Path("/tmp/agata-worker-heartbeat")
_EXPECTED_ALEMBIC_HEAD = "agata0011"


@dataclass(frozen=True, slots=True)
class MemoryWorkerCycle:
    claimed: int
    completed: int
    failed: int


class SummaryProcessingError(ValueError):
    """Finite summary-worker failure; content is never copied into the code."""


def claimed_job_types(*, embedding_available: bool) -> frozenset[str]:
    job_types = {
        "conversation_summary",
        "relationship_note_refresh",
        "snapshot_refresh",
        "memory_extract",
        "memory_promote",
        "memory_graph_sync",
        "response_retry",
    }
    if embedding_available:
        job_types.add("memory_embed")
    return frozenset(job_types)


class MemoryWorker:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        tenant_id: UUID,
        provider: HuggingFaceFreeEmbeddingProvider,
        crypto: AeadCrypto,
        shadow_hmac_key: bytes,
        suppression_hmac_roots: tuple[bytes, ...] = (),
        extraction_provider: ExtractionProvider | None = None,
        summary_provider: DeepSeekResponseProvider | None = None,
        batch_size: int = 4,
    ) -> None:
        self.session_factory = session_factory
        self.tenant_id = tenant_id
        self.provider = provider
        self.crypto = crypto
        self.shadow_hmac_key = shadow_hmac_key
        self.suppression_hmac_roots = suppression_hmac_roots or (shadow_hmac_key,)
        self.extraction_provider = extraction_provider
        self.summary_provider = summary_provider
        self.batch_size = batch_size

    async def run_once(self) -> MemoryWorkerCycle:
        owned_job_types = claimed_job_types(
            embedding_available=getattr(self.provider, "available", True)
        )
        async with self.session_factory() as claim_session:
            async with claim_session.begin():
                await set_tenant_context(claim_session, str(self.tenant_id))
                repository = OutboxRepository(claim_session)
                await repository.requeue_expired(
                    tenant_id=self.tenant_id, job_types=owned_job_types
                )
                jobs = await repository.claim(
                    tenant_id=self.tenant_id,
                    limit=self.batch_size,
                    job_types=owned_job_types,
                )
            memory_jobs = jobs
        completed = failed = 0
        for job in memory_jobs:
            async with self.session_factory() as session:
                if job.job_type == "relationship_note_refresh":
                    try:
                        await self._process_relationship_note(job)
                        completed += 1
                    except Exception:
                        await self._fail_claimed(job, "snapshot_refresh_failed", max_attempts=3)
                        failed += 1
                    continue
                if job.job_type == "snapshot_refresh":
                    try:
                        await self._process_snapshot_refresh(job)
                        completed += 1
                    except (SummaryProcessingError, StaleCreationEpoch) as exc:
                        await self._fail_claimed(job, str(exc), max_attempts=3)
                        failed += 1
                    continue
                if job.job_type == "conversation_summary":
                    try:
                        await self._process_summary(job)
                        completed += 1
                    except StaleCreationEpoch:
                        await self._fail_claimed(job, "summary_stale")
                        failed += 1
                    except SummaryProcessingError as exc:
                        await self._fail_claimed(job, str(exc), max_attempts=3)
                        failed += 1
                    continue
                if job.job_type == "memory_embed":
                    ok = await EmbeddingWorker(session, self.provider).process_claimed(job)
                    if ok:
                        completed += 1
                    else:
                        failed += 1
                    continue
                if job.job_type == "memory_graph_sync":
                    try:
                        await self._process_graph_sync(job)
                        completed += 1
                    except StaleCreationEpoch:
                        # Epoch races are terminal no-ops, never graph resurrection.
                        failed += 1
                    except Exception:
                        await self._fail_claimed(job, "memory_graph_sync_failed", max_attempts=3)
                        failed += 1
                    continue
                if job.job_type == "response_retry":
                    try:
                        await self._process_response_retry(job, session)
                        completed += 1
                    except Exception:
                        await self._fail_claimed(job, "response_finalization_failed")
                        failed += 1
                    continue
                if job.job_type == "memory_extract":
                    if self.extraction_provider is None:
                        await self._fail_claimed(job, "extractor_unavailable", max_attempts=3)
                        failed += 1
                        continue
                    try:
                        outcome = await ShadowExtractionWorker(
                            session,
                            crypto=self.crypto,
                            hmac_key=self.shadow_hmac_key,
                            provider=self.extraction_provider,
                        ).process_claimed(job)
                    except StaleCreationEpoch:
                        # A deletion/epoch race makes the claimed job obsolete;
                        # it must not take down the long-running worker.
                        failed += 1
                        continue
                    if not outcome.completed:
                        failed += 1
                        continue
                    if outcome.candidate_ids:
                        async with session.begin():
                            await set_tenant_context(session, str(self.tenant_id))
                            for candidate_id in outcome.candidate_ids:
                                await OutboxRepository(session).enqueue(
                                    tenant_id=self.tenant_id,
                                    job_type="memory_promote",
                                    dedupe_key=f"promote:{candidate_id}",
                                    aggregate_id=candidate_id,
                                    creation_epoch=job.creation_epoch,
                                    memory_control_state_id=job.memory_control_state_id,
                                    payload={"candidate_id": str(candidate_id)},
                                )
                    completed += 1
                    continue
                try:
                    result = await AutoWritePromotionService(
                        session,
                        self.crypto,
                        hmac_key=self.shadow_hmac_key,
                    ).process_claimed(job)
                except StaleCreationEpoch:
                    failed += 1
                    continue
                if result.status in {"promoted", "identical", "denied", "conflict"}:
                    completed += 1
                else:
                    failed += 1
        return MemoryWorkerCycle(len(memory_jobs), completed, failed)

    async def _process_response_retry(self, job, session: AsyncSession) -> None:
        """Finalize a completed Mac inference using AGATA's normal response pipeline."""
        try:
            message_id = UUID(str(job.payload["message_id"]))
            request_id = job.payload["request_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SummaryProcessingError("response_retry_invalid") from exc
        if job.aggregate_id != message_id or not isinstance(request_id, str):
            raise SummaryProcessingError("response_retry_invalid")

        async with session.begin():
            await set_tenant_context(session, str(self.tenant_id))
            row = (await session.execute(
                select(Message, User, Character, Conversation)
                .join(User, (User.tenant_id == Message.tenant_id) & (User.id == Message.user_id))
                .join(Character, (Character.tenant_id == Message.tenant_id) & (Character.id == Message.character_id))
                .join(Conversation, (Conversation.tenant_id == Message.tenant_id) & (Conversation.id == Message.conversation_id))
                .where(
                    Message.tenant_id == self.tenant_id,
                    Message.id == message_id,
                    Message.idempotency_key == request_id,
                    Message.role == "user",
                    Message.deleted_at.is_(None),
                    User.source_system == get_settings().source_system,
                    User.deleted_at.is_(None),
                    Character.source_system == get_settings().source_system,
                    Character.status == "active",
                    Conversation.source_system == get_settings().source_system,
                )
            )).one_or_none()
            if row is None:
                raise SummaryProcessingError("response_source_unavailable")
            message, user, character, conversation = row
            principal = PrincipalContext(self.tenant_id, user.id, user.id, "internal")
            request = ResponseRequest(
                conversation_id=conversation.id,
                user_message_id=message.id,
                idempotency_key=request_id,
                as_of=message.occurred_at,
            )

        settings = get_settings()
        retrieval_key = base64.b64decode(settings.retrieval_query_hmac_key.get_secret_value(), validate=True)
        response_key = base64.b64decode(settings.response_fingerprint_hmac_key.get_secret_value(), validate=True)
        orchestrator = ResponseOrchestrator(
            session,
            self.crypto,
            HybridRetrievalService(session, self.provider, hmac_key=retrieval_key),
            build_response_provider(session, settings),
            hmac_key=response_key,
            provider_timeout_seconds=settings.llm_timeout_seconds,
        )
        result = await orchestrator.respond(principal, request)
        if result.status != "completed" or result.assistant_message_id is None:
            raise SummaryProcessingError("response_not_finalized")
        async with session.begin():
            await set_tenant_context(session, str(self.tenant_id))
            current_job = await session.scalar(
                select(OutboxJob).where(
                    OutboxJob.tenant_id == self.tenant_id,
                    OutboxJob.id == job.id,
                ).with_for_update()
            )
            if current_job is None:
                raise SummaryProcessingError("response_retry_unavailable")
            await OutboxRepository(session).complete(current_job)

    async def _process_graph_sync(self, job) -> None:
        try:
            memory_id = UUID(str(job.payload["memory_id"]))
            memory_version = int(job.payload["memory_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SummaryProcessingError("memory_graph_job_invalid") from exc
        if job.aggregate_id != memory_id or memory_version < 1:
            raise SummaryProcessingError("memory_graph_job_invalid")
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    await set_tenant_context(session, str(self.tenant_id))

                    async def project(_session):
                        await PersistentMemoryGraphService(
                            session, crypto=self.crypto, hmac_root=self.shadow_hmac_key,
                            suppression_hmac_roots=self.suppression_hmac_roots,
                        ).project_memory(
                            memory_id=memory_id, memory_version=memory_version,
                            tenant_id=self.tenant_id, control_id=job.memory_control_state_id,
                            creation_epoch=job.creation_epoch,
                        )

                    await OutboxRepository(session).complete_memory_job(job, project)
            except StaleCreationEpoch:
                async with session.begin():
                    await set_tenant_context(session, str(self.tenant_id))
                    await OutboxRepository(session).fail(job, "stale_epoch", max_attempts=1)

    async def _fail_claimed(self, job, code: str, *, max_attempts: int = 5) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                await OutboxRepository(session).fail(job, code, max_attempts=max_attempts)

    async def _process_summary(self, job) -> None:
        """Build one durable segment and conversation-scoped snapshots."""
        if self.summary_provider is None:
            raise SummaryProcessingError("summary_provider_unavailable")
        try:
            conversation_id = UUID(str(job.payload["conversation_id"]))
            user_id = UUID(str(job.payload["user_id"]))
            character_id = UUID(str(job.payload["character_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SummaryProcessingError("summary_job_invalid") from exc
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                conversation = await session.scalar(
                    select(Conversation).where(
                        Conversation.tenant_id == self.tenant_id,
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                        Conversation.character_id == character_id,
                    )
                )
                character_name = await session.scalar(
                    select(Character.name).where(
                        Character.tenant_id == self.tenant_id,
                        Character.id == character_id,
                    )
                )
                consent = await session.scalar(select(Consent.id).where(
                    Consent.tenant_id == self.tenant_id, Consent.user_id == user_id,
                    Consent.purpose == "chat_history", Consent.revoked_at.is_(None),
                ).limit(1))
                snapshot_generations = {}
                if conversation is not None:
                    current_snapshots = list((await session.execute(select(
                        MemorySnapshot.snapshot_type, MemorySnapshot.generation,
                    ).where(
                        MemorySnapshot.tenant_id == self.tenant_id,
                        MemorySnapshot.relationship_id == conversation.relationship_id,
                        MemorySnapshot.snapshot_key.in_(
                            (f"conversation:{conversation.id}", "relationship")
                        ),
                        MemorySnapshot.snapshot_type.in_(
                            ("conversation_summary", "current_context_summary", "relationship_summary")
                        ),
                    ))).all())
                    snapshot_generations = {
                        (row.snapshot_type, row.generation) for row in current_snapshots
                    }
                    snapshot_generations = {
                        snapshot_type: max(
                            (generation for kind, generation in snapshot_generations if kind == snapshot_type),
                            default=0,
                        )
                        for snapshot_type in (
                            "conversation_summary", "current_context_summary", "relationship_summary"
                        )
                    }
                segment_count = await session.scalar(
                    select(func.count())
                    .select_from(ConversationSegment)
                    .where(
                        ConversationSegment.tenant_id == self.tenant_id,
                        ConversationSegment.conversation_id == conversation_id,
                    )
                )
                rows = list(
                    (
                        await session.scalars(
                            select(Message)
                            .where(
                                Message.tenant_id == self.tenant_id,
                                Message.conversation_id == conversation_id,
                                Message.user_id == user_id,
                                Message.character_id == character_id,
                                or_(Message.role != "assistant", Message.channel_delivery_status == "confirmed"),
                                Message.deleted_at.is_(None),
                            )
                            .order_by(Message.occurred_at, Message.id)
                            .offset(int(segment_count or 0) * 10)
                            .limit(10)
                        )
                    ).all()
                )
            if conversation is None:
                raise StaleCreationEpoch("summary conversation unavailable")
            if consent is None:
                async with session.begin():
                    await set_tenant_context(session, str(self.tenant_id))
                    snapshots = MemorySnapshotService(session, self.crypto)
                    await snapshots.invalidate_relationship(
                        self.tenant_id, conversation.relationship_id,
                        snapshot_types=("conversation_summary", "current_context_summary", "relationship_summary"),
                    )
                    await OutboxRepository(session).complete_memory_job(job)
                return
            if len(rows) < 5:
                async with session.begin():
                    await set_tenant_context(session, str(self.tenant_id))
                    await OutboxRepository(session).complete_memory_job(job)
                return
            dialogue: list[str] = []
            for message in reversed(rows):
                try:
                    assert message.content_ciphertext is not None
                    assert message.content_nonce is not None
                    assert message.content_key_version is not None
                    content = self.crypto.decrypt(
                        CiphertextEnvelope(
                            message.content_ciphertext,
                            message.content_nonce,
                            message.content_key_version,
                        ),
                        MessageIngestionService._aad_for(
                            self.tenant_id, user_id, conversation_id, message.id
                        ),
                    )
                except Exception:
                    continue
                dialogue.append(
                    ("Пользователь" if message.role == "user" else (character_name or "Персонаж"))
                    + ": "
                    + content[:400]
                )
        if not dialogue:
            raise SummaryProcessingError("summary_source_unavailable")
        prompt = (
            "Сделай краткую нейтральную сводку последних сообщений разговора на русском, "
            "2-3 предложения. Не добавляй фактов, которых нет в тексте.\n" + "\n".join(dialogue)
        )
        try:
            envelope = await self.summary_provider.complete(
                ResponseContext(
                    profile_version=0,
                    route_tuple=("", "", "", "", ""),
                    current_message=prompt,
                    response_language="ru",
                    personality={"name": "conversation_summarizer"},
                )
            )
        except Exception as exc:
            raise SummaryProcessingError("summary_provider_failed") from exc
        summary = envelope.text[:1000] if isinstance(envelope, ProviderEnvelope) else ""
        if not summary or "\x00" in summary:
            raise SummaryProcessingError("summary_provider_invalid")
        now = datetime.now(UTC)
        started_at, ended_at = segment_interval(message.occurred_at for message in rows)
        hierarchy = segment_hierarchy(summary, started_at, ended_at)
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                control = await session.scalar(
                    select(MemoryControlState)
                    .where(
                        MemoryControlState.id == job.memory_control_state_id,
                        MemoryControlState.tenant_id == self.tenant_id,
                    )
                    .with_for_update()
                )
                if control is None or control.relationship_id != conversation.relationship_id:
                    raise StaleCreationEpoch("summary control unavailable")
                if control.deletion_epoch != job.creation_epoch:
                    raise StaleCreationEpoch("summary_creation_epoch_stale")
                current_chat_consent = await session.scalar(select(Consent.id).where(
                    Consent.tenant_id == self.tenant_id, Consent.user_id == user_id,
                    Consent.purpose == "chat_history", Consent.revoked_at.is_(None),
                ).limit(1))
                if current_chat_consent is None:
                    await MemorySnapshotService(session, self.crypto).invalidate_relationship(
                        self.tenant_id, conversation.relationship_id,
                        snapshot_types=("conversation_summary", "current_context_summary"),
                    )
                    await OutboxRepository(session).complete_memory_job(job)
                    return
                segment_id = uuid4()
                segment = ConversationSegment(
                    id=segment_id,
                    tenant_id=self.tenant_id,
                    conversation_id=conversation_id,
                    summary=encrypt_text_for_storage(
                        summary,
                        content_aad(
                            entity="conversation_segments",
                            tenant_id=self.tenant_id,
                            user_id=user_id,
                            character_id=character_id,
                            scope=conversation.scope,
                            scenario_id=conversation.scenario_id,
                            record_id=segment_id,
                            field="summary",
                        ),
                        crypto=self.crypto,
                    ),
                    keyphrases=encrypt_json_for_storage(
                        [word for word in summary.split() if len(word) > 3][:8],
                        content_aad(
                            entity="conversation_segments",
                            tenant_id=self.tenant_id,
                            user_id=user_id,
                            character_id=character_id,
                            scope=conversation.scope,
                            scenario_id=conversation.scenario_id,
                            record_id=segment_id,
                            field="keyphrases",
                        ),
                        crypto=self.crypto,
                    ),
                    started_at=started_at,
                    ended_at=ended_at,
                )
                session.add(segment)
                await session.flush()
                snapshots = MemorySnapshotService(session, self.crypto)
                fingerprint = snapshot_fingerprint(
                    "conversation_summary", conversation.id, control.deletion_epoch,
                    [(str(row.id), row.occurred_at.isoformat()) for row in rows],
                )
                await snapshots.upsert(
                    tenant_id=self.tenant_id, relationship=conversation,
                    snapshot_type="conversation_summary", snapshot_key=f"conversation:{conversation.id}",
                    content={"summary": summary, "segment_id": str(segment.id), "hierarchy": hierarchy},
                    source_fingerprint=fingerprint, creation_epoch=control.deletion_epoch,
                    source_segment_ids=[segment.id], conversation_id=conversation.id,
                    expected_generation=snapshot_generations.get("conversation_summary", 0),
                )
                await snapshots.upsert(
                    tenant_id=self.tenant_id, relationship=conversation,
                    snapshot_type="current_context_summary", snapshot_key=f"conversation:{conversation.id}",
                    content={"active_topic": summary[:400], "open_loops": [], "recent_decisions": [], "temporary_context": summary[:700], "pending_questions": []},
                    source_fingerprint=snapshot_fingerprint("current_context", conversation.id, fingerprint),
                    creation_epoch=control.deletion_epoch, source_segment_ids=[segment.id],
                    conversation_id=conversation.id, expires_at=now + timedelta(days=7),
                    expected_generation=snapshot_generations.get("current_context_summary", 0),
                )
                long_term_consent = await session.scalar(select(Consent.id).where(
                    Consent.tenant_id == self.tenant_id, Consent.user_id == user_id,
                    Consent.purpose == "long_term_memory", Consent.revoked_at.is_(None),
                ).limit(1))
                if long_term_consent is not None:
                    await snapshots.upsert(
                        tenant_id=self.tenant_id, relationship=conversation,
                        snapshot_type="relationship_summary", snapshot_key="relationship",
                        content={"recent_summary": summary},
                        source_fingerprint=snapshot_fingerprint("relationship_summary", conversation.relationship_id, fingerprint),
                        creation_epoch=control.deletion_epoch, source_segment_ids=[segment.id],
                        expected_generation=snapshot_generations.get("relationship_summary", 0),
                    )
                await OutboxRepository(session).complete_memory_job(job)

    async def _process_relationship_note(self, job) -> None:
        relationship_id = UUID(str(job.payload["relationship_id"]))
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                from app.db.models import Relationship
                relationship = await session.scalar(select(Relationship).where(
                    Relationship.tenant_id == self.tenant_id,
                    Relationship.id == relationship_id,
                ).with_for_update())
                if relationship is None:
                    await OutboxRepository(session).complete(job)
                    return
                control = await session.scalar(select(MemoryControlState).where(
                    MemoryControlState.tenant_id == self.tenant_id,
                    MemoryControlState.relationship_id == relationship_id,
                ).with_for_update())
                if control is None or control.deletion_epoch != job.creation_epoch:
                    raise StaleCreationEpoch("snapshot_creation_epoch_stale")
                await MemorySnapshotService(session, self.crypto).refresh_relationship_note(
                    tenant_id=self.tenant_id, relationship=relationship,
                )
                await OutboxRepository(session).complete(job)

    async def _process_snapshot_refresh(self, job) -> None:
        try:
            relationship_id = UUID(str(job.payload["relationship_id"]))
            snapshot_type = str(job.payload["snapshot_type"])
            conversation_value = job.payload.get("conversation_id")
            conversation_id = UUID(str(conversation_value)) if conversation_value else None
        except (KeyError, TypeError, ValueError) as exc:
            raise SummaryProcessingError("snapshot_refresh_job_invalid") from exc
        if conversation_id is not None:
            raise SummaryProcessingError("snapshot_refresh_type_unsupported")
        if snapshot_type == "relationship_note":
            await self._process_relationship_note(job)
            return
        if snapshot_type != "relationship_summary":
            raise SummaryProcessingError("snapshot_refresh_type_unsupported")
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                relationship = await session.scalar(select(Relationship).where(
                    Relationship.tenant_id == self.tenant_id,
                    Relationship.id == relationship_id,
                ).with_for_update())
                control = await session.scalar(select(MemoryControlState).where(
                    MemoryControlState.tenant_id == self.tenant_id,
                    MemoryControlState.relationship_id == relationship_id,
                ).with_for_update())
                if relationship is None or control is None:
                    await OutboxRepository(session).complete(job)
                    return
                if control.deletion_epoch != job.creation_epoch:
                    raise StaleCreationEpoch("snapshot_creation_epoch_stale")
                consents = list((await session.scalars(select(Consent).where(
                    Consent.tenant_id == self.tenant_id,
                    Consent.user_id == relationship.user_id,
                    Consent.purpose.in_(("chat_history", "long_term_memory")),
                    Consent.revoked_at.is_(None),
                ).with_for_update())).all())
                if {consent.purpose for consent in consents} != {"chat_history", "long_term_memory"}:
                    await MemorySnapshotService(session, self.crypto).invalidate_relationship(
                        self.tenant_id, relationship_id, snapshot_types=("relationship_summary",)
                    )
                    await OutboxRepository(session).complete(job)
                    return
                conversation_rows = list((await session.execute(
                    select(ConversationSegment, Conversation)
                    .join(Conversation, (Conversation.tenant_id == ConversationSegment.tenant_id) & (Conversation.id == ConversationSegment.conversation_id))
                    .where(
                        ConversationSegment.tenant_id == self.tenant_id,
                        Conversation.relationship_id == relationship_id,
                        ConversationSegment.summary.is_not(None),
                    )
                    .order_by(ConversationSegment.ended_at.desc(), ConversationSegment.id)
                    .limit(3)
                )).all())
                note = await MemorySnapshotService(session, self.crypto).refresh_relationship_note(
                    tenant_id=self.tenant_id, relationship=relationship,
                )
                rendered: list[str] = []
                source_segment_ids: list[UUID] = []
                for segment, conversation in conversation_rows:
                    summary = decrypt_text_from_storage(
                        segment.summary, segment_summary_aad(segment, conversation), crypto=self.crypto
                    )
                    if isinstance(summary, str) and summary.strip():
                        rendered.append(summary[:500])
                        source_segment_ids.append(segment.id)
                shared_context = SharedHistoryContext(
                    self.tenant_id, relationship.user_id, relationship.character_id,
                    relationship.scope, relationship.scenario_id, relationship.id,
                )
                shared = await SharedHistoryService(session).load_in_transaction(shared_context)
                shared_text = [item.value[:200] for item in shared[:4]]
                pieces = []
                if rendered:
                    pieces.append("Conversation history (not authoritative current facts): " + " ".join(rendered))
                if shared_text:
                    pieces.append("Confirmed relationship history (not current facts): " + "; ".join(shared_text))
                if not pieces:
                    await OutboxRepository(session).complete(job)
                    return
                source_segments = [
                    (str(segment.id), segment.ended_at.isoformat())
                    for segment, _conversation in conversation_rows if segment.id in source_segment_ids
                ]
                fingerprint = snapshot_fingerprint(
                    "relationship_summary", relationship_id, control.deletion_epoch,
                    note.generation if note is not None else 0, source_segments,
                    [(str(item.memory_id), item.memory_version) for item in shared],
                )
                await MemorySnapshotService(session, self.crypto).upsert(
                    tenant_id=self.tenant_id, relationship=relationship,
                    snapshot_type="relationship_summary", snapshot_key="relationship",
                    content={"recent_summary": " ".join(pieces)[:2400]},
                    source_fingerprint=fingerprint,
                    creation_epoch=control.deletion_epoch,
                    source_memory_ids=[item.memory_id for item in shared],
                    source_segment_ids=source_segment_ids,
                )
                await OutboxRepository(session).complete(job)

    async def run_forever(self, stop_event: asyncio.Event, poll_seconds: float = 5) -> None:
        while not stop_event.is_set():
            await self.run_once()
            _HEARTBEAT_PATH.write_text(datetime.now(UTC).isoformat(), encoding="ascii")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue


async def _amain() -> int:
    settings: Settings = get_settings()
    provider = HuggingFaceFreeEmbeddingProvider(
        api_key=settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else "",
        timeout=settings.llm_timeout_seconds,
    )
    summary_provider = None
    extraction_provider: ExtractionProvider | None = None
    if settings.llm_provider not in {"disabled", "none"} and settings.llm_api_key:
        summary_provider = DeepSeekResponseProvider(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_api_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            provider=settings.llm_provider,
            model=settings.llm_model,
        )
        extraction_provider = StructuredExtractionProvider(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_api_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    runtime = MemoryWorker(
        SessionFactory,
        tenant_id=UUID(settings.tenant_id),
        provider=provider,
        crypto=AeadCrypto(
            encoded_key=settings.message_aead_key.get_secret_value(),
            key_version=settings.message_aead_key_version,
        ),
        shadow_hmac_key=base64.b64decode(
            settings.shadow_candidate_hmac_key.get_secret_value(), validate=True
        ),
        suppression_hmac_roots=(
            base64.b64decode(settings.shadow_candidate_hmac_key.get_secret_value(), validate=True),
            base64.b64decode(settings.request_fingerprint_hmac_key.get_secret_value(), validate=True),
        ),
        extraction_provider=extraction_provider,
        summary_provider=summary_provider,
        batch_size=settings.worker_batch_size,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await runtime.run_forever(stop_event, poll_seconds=settings.worker_poll_seconds)
    return 0


async def _healthcheck() -> int:
    settings = get_settings()
    try:
        age = datetime.now(UTC).timestamp() - _HEARTBEAT_PATH.stat().st_mtime
        if age < 0 or age > max(90.0, settings.worker_poll_seconds * 4):
            return 1
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, settings.tenant_id)
                revision = await session.scalar(
                    text("SELECT version_num FROM agata.alembic_version")
                )
                await session.scalar(text("SELECT 1 FROM agata.outbox_jobs LIMIT 1"))
        return 0 if revision == _EXPECTED_ALEMBIC_HEAD else 1
    except Exception:
        return 1


def main() -> int:
    if "--healthcheck" in sys.argv[1:]:
        return asyncio.run(_healthcheck())
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
