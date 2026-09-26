"""Durable worker for epoch-guarded memory embedding jobs."""

from __future__ import annotations

import asyncio
import base64
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Conversation,
    ConversationSegment,
    DerivedMemoryView,
    MemoryControlState,
    Message,
)
from app.jobs.outbox import OutboxRepository, StaleCreationEpoch
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.promotion import AutoWritePromotionService
from app.memory.retrieval import EmbeddingWorker
from app.memory.segments import segment_hierarchy, segment_interval
from app.memory.shadow import (
    ExtractionProvider,
    ShadowExtractionWorker,
    StructuredExtractionProvider,
)
from app.messages.crypto import AeadCrypto, CiphertextEnvelope
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    derived_view_content_aad,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
)
from app.messages.service import MessageIngestionService
from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.pipeline import ProviderEnvelope, ResponseContext

_HEARTBEAT_PATH = Path("/tmp/agata-worker-heartbeat")


@dataclass(frozen=True, slots=True)
class MemoryWorkerCycle:
    claimed: int
    completed: int
    failed: int


class SummaryProcessingError(ValueError):
    """Finite summary-worker failure; content is never copied into the code."""


class MemoryWorker:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        tenant_id: UUID,
        provider: HuggingFaceFreeEmbeddingProvider,
        crypto: AeadCrypto,
        shadow_hmac_key: bytes,
        extraction_provider: ExtractionProvider | None = None,
        summary_provider: DeepSeekResponseProvider | None = None,
        batch_size: int = 4,
    ) -> None:
        self.session_factory = session_factory
        self.tenant_id = tenant_id
        self.provider = provider
        self.crypto = crypto
        self.shadow_hmac_key = shadow_hmac_key
        self.extraction_provider = extraction_provider
        self.summary_provider = summary_provider
        self.batch_size = batch_size

    async def run_once(self) -> MemoryWorkerCycle:
        owned_job_types = frozenset(
            {
                "conversation_summary",
                "memory_extract",
                "memory_promote",
                "memory_embed",
            }
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

    async def _fail_claimed(self, job, code: str, *, max_attempts: int = 5) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await set_tenant_context(session, str(self.tenant_id))
                await OutboxRepository(session).fail(job, code, max_attempts=max_attempts)

    async def _process_summary(self, job) -> None:
        """Build one durable segment/view and complete the epoch-guarded job."""
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
                if control is None:
                    raise StaleCreationEpoch("summary control unavailable")
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
                view = await session.scalar(
                    select(DerivedMemoryView)
                    .where(
                        DerivedMemoryView.tenant_id == self.tenant_id,
                        DerivedMemoryView.user_id == user_id,
                        DerivedMemoryView.character_id == character_id,
                        DerivedMemoryView.scope == conversation.scope,
                        DerivedMemoryView.scenario_id == conversation.scenario_id,
                        DerivedMemoryView.view_type == "conversation_summary",
                    )
                    .with_for_update()
                )
                previous_segments: list[dict[str, object]] = []
                previous_parent_ids: list[str] = []
                if view is not None and isinstance(view.content, dict) and view.content:
                    previous_content = decrypt_json_from_storage(
                        view.content, derived_view_content_aad(view), crypto=self.crypto
                    )
                    stored = (
                        previous_content.get("segments")
                        if isinstance(previous_content, dict)
                        else None
                    )
                    if isinstance(stored, list):
                        previous_segments = [item for item in stored if isinstance(item, dict)][
                            -23:
                        ]
                    previous_parent_ids = [
                        item for item in view.parent_memory_ids if isinstance(item, str)
                    ][-23:]
                segment_projection = {
                    "summary": summary,
                    "segment_id": str(segment.id),
                    "hierarchy": hierarchy,
                    "created_at": now.isoformat(),
                }
                segments = previous_segments + [segment_projection]
                topic_days: dict[str, list[str]] = {}
                project_topics: list[str] = []
                for item in segments:
                    item_hierarchy = item.get("hierarchy")
                    if not isinstance(item_hierarchy, dict):
                        continue
                    topic_day = item_hierarchy.get("topic_day")
                    if isinstance(topic_day, dict):
                        day = topic_day.get("day")
                        topics = topic_day.get("topics")
                        if isinstance(day, str) and isinstance(topics, list):
                            topic_days[day] = [value for value in topics if isinstance(value, str)][
                                :12
                            ]
                    relationship = item_hierarchy.get("relationship_project")
                    if isinstance(relationship, dict):
                        topics = relationship.get("project_topics")
                        if isinstance(topics, list):
                            for value in topics:
                                if isinstance(value, str) and value not in project_topics:
                                    project_topics.append(value)
                payload = {
                    "latest_summary": summary,
                    "segments": segments,
                    "topic_days": topic_days,
                    "relationship_project": {
                        "relationship": "user_character",
                        "project_topics": project_topics[:16],
                    },
                    "updated_at": now.isoformat(),
                }
                if view is None:
                    view_id = uuid4()
                    session.add(
                        DerivedMemoryView(
                            id=view_id,
                            tenant_id=self.tenant_id,
                            user_id=user_id,
                            character_id=character_id,
                            scope=conversation.scope,
                            scenario_id=conversation.scenario_id,
                            view_type="conversation_summary",
                            content=encrypt_json_for_storage(
                                payload,
                                content_aad(
                                    entity="derived_memory_views",
                                    tenant_id=self.tenant_id,
                                    user_id=user_id,
                                    character_id=character_id,
                                    scope=conversation.scope,
                                    scenario_id=conversation.scenario_id,
                                    record_id=view_id,
                                    field="content",
                                ),
                                crypto=self.crypto,
                            ),
                            parent_memory_ids=[str(segment.id)],
                            creation_epoch=control.deletion_epoch,
                        )
                    )
                else:
                    view.generation += 1
                    view.content = encrypt_json_for_storage(
                        payload, derived_view_content_aad(view), crypto=self.crypto
                    )
                    view.parent_memory_ids = previous_parent_ids + [str(segment.id)]
                    view.creation_epoch = control.deletion_epoch
                await OutboxRepository(session).complete_memory_job(job)

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
    if not settings.embedding_api_key:
        return 2
    provider = HuggingFaceFreeEmbeddingProvider(
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
    )
    summary_provider = None
    extraction_provider: ExtractionProvider | None = None
    if settings.llm_api_key:
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
        return 0 if revision == "agata0002" else 1
    except Exception:
        return 1


def main() -> int:
    if "--healthcheck" in sys.argv[1:]:
        return asyncio.run(_healthcheck())
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
