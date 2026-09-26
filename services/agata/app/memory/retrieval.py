"""Privacy-gated embedding execution and deterministic hybrid memory retrieval."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    Consent,
    Conversation,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    Message,
    OutboxJob,
    Relationship,
    ResponseMemoryRef,
    RetentionPolicy,
    RetrievalCandidate,
    RetrievalRun,
    Scope,
)
from app.domain.principal import PrincipalContext
from app.jobs.outbox import OutboxRepository, StaleCreationEpoch
from app.messages.crypto import canonical_tagged_fields, derive_hmac_subkey
from app.messages.protected_content import (
    decrypt_text_from_storage,
    memory_value_aad,
    opaque_search_text,
)
from app.messages.service import DomainError

EMBEDDING_DIMENSIONS = 1536
QUERY_DOMAIN = b"gptgirls/retrieval-query-fingerprint/v1"


@dataclass(frozen=True, slots=True)
class RRFConfig:
    """Stable reciprocal-rank fusion configuration.

    Kept as a typed value so benchmarks and callers cannot silently diverge from
    the production fusion constant.
    """

    k: int = 60

    def __post_init__(self) -> None:
        if isinstance(self.k, bool) or not isinstance(self.k, int) or self.k <= 0:
            raise ValueError("rrf_config_invalid")


RRF_K = RRFConfig().k


class EmbeddingProvider(Protocol):
    model_name: str

    async def embed(self, text: str) -> object: ...


class DeterministicFakeEmbeddingProvider:
    """Offline fixture only; it is intentionally not an external provider adapter."""

    model_name = "deterministic-embedding-v1"

    async def embed(self, text: str) -> object:
        if not isinstance(text, str):
            raise ValueError("invalid embedding input")
        digest = sha256(text.encode("utf-8")).digest()
        return [
            ((digest[index % len(digest)] / 127.5) - 1.0) for index in range(EMBEDDING_DIMENSIONS)
        ]


class OpenAICompatibleEmbeddingProvider:
    """Production embedding adapter for an OpenAI-compatible endpoint."""

    model_name = "text-embedding-3-small"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not base_url.startswith("https://"):
            raise ValueError("embedding_configuration_invalid")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 30:
            raise ValueError("embedding_timeout_invalid")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def embed(self, text: str) -> object:
        if not isinstance(text, str) or not 1 <= len(text) <= 8192:
            raise ValueError("embedding_input_invalid")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model_name, "input": text, "encoding_format": "float"},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ValueError("embedding_provider_transport") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError("embedding_provider_failure")
        try:
            body = response.json()
            vector = body["data"][0]["embedding"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("embedding_provider_malformed") from exc
        return vector


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    character_id: UUID
    relationship_id: UUID
    scope: Scope
    scenario_id: UUID | None
    query: str
    as_of: datetime
    per_list_k: int = 10
    final_k: int = 8
    max_characters: int = 2048
    kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory_id: UUID
    version: int
    kind: str
    subject: str | None
    predicate: str | None
    value: str | None  # Explicitly untrusted data, never an instruction channel.
    evidence_state: str
    lifecycle_state: str
    observed_at: datetime
    valid_from: datetime | None
    valid_until: datetime | None
    confidence: float
    importance: float
    source_message_ids: tuple[UUID, ...]
    component_ranks: tuple[int | None, int | None, int | None, int | None]
    fused_score: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    results: tuple[RetrievedMemory, ...]
    reason_code: str
    run_id: UUID | None


def _finite_vector(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        raise DomainError("embedding_output_invalid")
    output: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (float, int))
            or not math.isfinite(float(item))
        ):
            raise DomainError("embedding_output_invalid")
        output.append(float(item))
    return output


class EmbeddingWorker:
    def __init__(self, session: AsyncSession, provider: EmbeddingProvider) -> None:
        self.session, self.provider = session, provider

    async def process_claimed(self, job: OutboxJob) -> bool:
        if (
            not isinstance(job, OutboxJob)
            or job.job_type != "memory_embed"
            or not isinstance(job.aggregate_id, UUID)
        ):
            raise DomainError("embedding_job_invalid")
        memory_id = job.aggregate_id
        assert isinstance(memory_id, UUID)
        async with self.session.begin():
            await set_tenant_context(self.session, str(job.tenant_id))
            try:
                async with self.session.begin_nested():

                    async def callback(_: AsyncSession) -> None:
                        await self._embed_guarded(job, memory_id)

                    await OutboxRepository(self.session).complete_memory_job(job, callback)
                    return True
            except StaleCreationEpoch:
                await OutboxRepository(self.session).fail(job, "embedding_stale")
                return False
            except Exception as exc:
                # Store a finite implementation code only; provider/message data never crosses this boundary.
                await OutboxRepository(self.session).fail(
                    job, f"embedding_rejected:{type(exc).__name__}"
                )
                return False

    async def _embed_guarded(self, job: OutboxJob, memory_id: UUID) -> None:
        memory = await self.session.scalar(
            self._eligible_memory_statement(
                job.tenant_id, memory_id, job.memory_control_state_id
            ).with_for_update(of=Memory)
        )
        if memory is None or memory.created_epoch != job.creation_epoch:
            raise StaleCreationEpoch("memory is no longer eligible")
        value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory))
        if not isinstance(value, str) or not value:
            raise DomainError("embedding_source_invalid")
        try:
            vector = _finite_vector(
                await asyncio.wait_for(self.provider.embed(value), timeout=2.0)
            )
        except TimeoutError:
            raise
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("embedding_provider_failure") from exc
        # Re-read under the same transaction immediately before persisting provider output.
        current = await self.session.scalar(
            self._eligible_memory_statement(
                job.tenant_id, memory.id, job.memory_control_state_id
            ).with_for_update(of=Memory)
        )
        if current is None or current.created_epoch != job.creation_epoch:
            raise StaleCreationEpoch("embedding source changed")
        current.embedding = vector
        current.embedding_model = self.provider.model_name
        current.embedded_at = datetime.now(UTC)
        await self.session.flush()

    @staticmethod
    def _eligible_memory_statement(tenant_id: UUID, memory_id: UUID, control_id: UUID | None):
        if control_id is None:
            return select(Memory).where(false())
        return (
            select(Memory)
            .join(
                MemoryControlState,
                and_(
                    MemoryControlState.tenant_id == Memory.tenant_id,
                    MemoryControlState.id == control_id,
                    MemoryControlState.relationship_id == Memory.relationship_id,
                    MemoryControlState.user_id == Memory.user_id,
                    MemoryControlState.character_id == Memory.character_id,
                    MemoryControlState.scope == Memory.scope,
                    MemoryControlState.scenario_key == Memory.scenario_key,
                    MemoryControlState.deletion_epoch == Memory.created_epoch,
                ),
            )
            .join(
                Consent,
                and_(
                    Consent.tenant_id == Memory.tenant_id,
                    Consent.user_id == Memory.user_id,
                    Consent.purpose == "long_term_memory",
                    Consent.revoked_at.is_(None),
                ),
            )
            .outerjoin(
                RetentionPolicy,
                and_(
                    RetentionPolicy.tenant_id == Memory.tenant_id,
                    RetentionPolicy.id == Memory.retention_policy_id,
                ),
            )
            .where(
                Memory.tenant_id == tenant_id,
                Memory.id == memory_id,
                Memory.lifecycle_state.in_([LifecycleState.active, LifecycleState.historical]),
                Memory.deleted_at.is_(None),
                or_(
                    RetentionPolicy.memory_days.is_(None),
                    Memory.created_at + func.make_interval(0, 0, 0, RetentionPolicy.memory_days)
                    > func.now(),
                ),
                or_(
                    exists(
                        select(MemorySource.id)
                        .join(
                            Message,
                            and_(
                                Message.tenant_id == MemorySource.tenant_id,
                                Message.id == MemorySource.message_id,
                                Message.deleted_at.is_(None),
                            ),
                        )
                        .where(
                            MemorySource.tenant_id == Memory.tenant_id,
                            MemorySource.memory_id == Memory.id,
                        )
                    ),
                    and_(
                        Memory.kind == "user_fact",
                        Memory.evidence_state == EvidenceState.explicit,
                    ),
                ),
            )
        )


class HybridRetrievalService:
    """Read-only hybrid retrieval; every list starts from the same SQL hard filter."""

    def __init__(
        self, session: AsyncSession, provider: EmbeddingProvider, *, hmac_key: bytes
    ) -> None:
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise DomainError("retrieval_key_invalid")
        self.session, self.provider = session, provider
        self._search_key = hmac_key
        self._query_key = derive_hmac_subkey(hmac_key, QUERY_DOMAIN)

    async def retrieve(
        self, principal: PrincipalContext, request: RetrievalRequest
    ) -> RetrievalResult:
        if not isinstance(principal, PrincipalContext):
            return RetrievalResult((), "invalid_request", None)
        if not self._valid_request(request):
            return RetrievalResult((), "invalid_request", None)
        started = time.monotonic()
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            relationship = await self.session.scalar(
                select(Relationship).where(
                    Relationship.tenant_id == principal.tenant_id,
                    Relationship.id == request.relationship_id,
                    Relationship.user_id == principal.user_id,
                    Relationship.character_id == request.character_id,
                    Relationship.scope == request.scope,
                    Relationship.scenario_id == request.scenario_id,
                    Relationship.status == "ACTIVE",
                )
            )
            if relationship is None:
                return RetrievalResult((), "invalid_request", None)
            base = self._hard_filter(principal, request)
            exact = list(
                (
                    await self.session.scalars(
                        base.where(
                            or_(Memory.slot_key == request.query, Memory.predicate == request.query)
                        )
                        .order_by(Memory.importance.desc(), Memory.id)
                        .limit(request.per_list_k)
                    )
                ).all()
            )
            fts = list(
                (
                    await self.session.scalars(
                        base.where(
                            func.to_tsvector("simple", func.coalesce(Memory.search_text, "")).op(
                                "@@"
                            )(
                                func.plainto_tsquery(
                                    "simple", opaque_search_text(request.query, self._search_key)
                                )
                            )
                        )
                        .order_by(Memory.id)
                        .limit(request.per_list_k)
                    )
                ).all()
            )
            temporal = list(
                (
                    await self.session.scalars(
                        base.order_by(Memory.observed_at.desc(), Memory.id).limit(
                            request.per_list_k
                        )
                    )
                ).all()
            )
            vector: list[Memory] = []
            reason = "ok"
            try:
                query_vector = _finite_vector(
                    await asyncio.wait_for(self.provider.embed(request.query), timeout=2.0)
                )
                vector = list(
                    (
                        await self.session.scalars(
                            base.where(
                                Memory.embedding.is_not(None),
                                Memory.embedding_model == self.provider.model_name,
                            )
                            .order_by(Memory.embedding.cosine_distance(query_vector), Memory.id)
                            .limit(request.per_list_k)
                        )
                    ).all()
                )
            except Exception:
                reason = "vector_degraded"
            ranks: dict[UUID, list[int | None]] = {}
            memories: dict[UUID, Memory] = {}
            for index, rows in enumerate((exact, fts, vector, temporal)):
                for rank, memory in enumerate(rows, 1):
                    memories[memory.id] = memory
                    ranks.setdefault(memory.id, [None, None, None, None])[index] = rank
            ordered = sorted(
                memories.values(),
                key=lambda item: (
                    -sum(1 / (RRF_K + rank) for rank in ranks[item.id] if rank is not None),
                    -float(item.importance),
                    str(item.id),
                ),
            )
            selected: list[RetrievedMemory] = []
            used = 0
            for memory in ordered[: request.final_k]:
                value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory))
                size = len(value or "")
                if used + size > request.max_characters:
                    continue
                used += size
                sources = tuple(
                    (
                        await self.session.scalars(
                            select(MemorySource.message_id).where(
                                MemorySource.tenant_id == principal.tenant_id,
                                MemorySource.memory_id == memory.id,
                            )
                        )
                    ).all()
                )
                component = ranks[memory.id]
                component_tuple = (component[0], component[1], component[2], component[3])
                score = sum(1 / (RRF_K + rank) for rank in component if rank is not None)
                selected.append(
                    RetrievedMemory(
                        memory.id,
                        memory.version,
                        memory.kind,
                        memory.subject_type,
                        memory.predicate,
                        value,
                        memory.evidence_state.value,
                        memory.lifecycle_state.value,
                        memory.observed_at,
                        memory.valid_from,
                        memory.valid_until,
                        float(memory.confidence),
                        float(memory.importance),
                        sources,
                        component_tuple,
                        score,
                        ("untrusted_memory_data",),
                    )
                )
            if not selected and reason == "ok":
                reason = "no_results"
            run = await self._telemetry(
                principal, request, selected, ranks, memories, reason, time.monotonic() - started
            )
            return RetrievalResult(tuple(selected), reason, run.id)

    def _hard_filter(self, principal: PrincipalContext, request: RetrievalRequest):
        return (
            select(Memory)
            .join(
                MemoryControlState,
                and_(
                    MemoryControlState.tenant_id == Memory.tenant_id,
                    MemoryControlState.relationship_id == Memory.relationship_id,
                    MemoryControlState.user_id == Memory.user_id,
                    MemoryControlState.character_id == Memory.character_id,
                    MemoryControlState.scope == Memory.scope,
                    MemoryControlState.scenario_key == Memory.scenario_key,
                    MemoryControlState.deletion_epoch == Memory.created_epoch,
                ),
            )
            .join(
                Consent,
                and_(
                    Consent.tenant_id == Memory.tenant_id,
                    Consent.user_id == Memory.user_id,
                    Consent.purpose == "long_term_memory",
                    Consent.revoked_at.is_(None),
                ),
            )
            .outerjoin(
                RetentionPolicy,
                and_(
                    RetentionPolicy.tenant_id == Memory.tenant_id,
                    RetentionPolicy.id == Memory.retention_policy_id,
                ),
            )
            .where(
                Memory.tenant_id == principal.tenant_id,
                Memory.user_id == principal.user_id,
                Memory.relationship_id == request.relationship_id,
                Memory.character_id == request.character_id,
                Memory.scope == request.scope,
                Memory.scenario_id == request.scenario_id,
                Memory.lifecycle_state.in_([LifecycleState.active, LifecycleState.historical]),
                Memory.deleted_at.is_(None),
                Memory.sensitivity == "normal",
                or_(Memory.valid_from.is_(None), Memory.valid_from <= request.as_of),
                or_(Memory.valid_until.is_(None), Memory.valid_until > request.as_of),
                or_(
                    RetentionPolicy.memory_days.is_(None),
                    Memory.created_at + func.make_interval(0, 0, 0, RetentionPolicy.memory_days)
                    > request.as_of,
                ),
            )
        )

    async def _telemetry(
        self,
        principal: PrincipalContext,
        request: RetrievalRequest,
        selected: list[RetrievedMemory],
        ranks: dict[UUID, list[int | None]],
        memories: dict[UUID, Memory],
        reason: str,
        elapsed: float,
    ) -> RetrievalRun:
        fingerprint = new(
            self._query_key,
            canonical_tagged_fields(
                (
                    (b"version", QUERY_DOMAIN),
                    (b"tenant", principal.tenant_id.bytes),
                    (b"user", principal.user_id.bytes),
                    (b"relationship", request.relationship_id.bytes),
                    (b"character", request.character_id.bytes),
                    (b"scope", request.scope.value.encode()),
                    (b"scenario", request.scenario_id.bytes if request.scenario_id else b""),
                    (b"query", request.query.encode("utf-8")),
                )
            ),
            sha256,
        ).hexdigest()
        bucket = (
            "lt_10ms"
            if elapsed < 0.01
            else "lt_100ms"
            if elapsed < 0.1
            else "lt_1s"
            if elapsed < 1
            else "gte_1s"
        )
        run = RetrievalRun(
            tenant_id=principal.tenant_id,
            relationship_id=request.relationship_id,
            user_id=principal.user_id,
            character_id=request.character_id,
            scope=request.scope,
            scenario_id=request.scenario_id,
            query_fingerprint=fingerprint,
            retrieval_policy_version="rrf-v1",
            index_version="pg-v1",
            embedding_model=self.provider.model_name,
            result_count=len(memories),
            selected_count=len(selected),
            latency_bucket=bucket,
            reason_code=reason,
        )
        self.session.add(run)
        await self.session.flush()
        selected_ids = {entry.memory_id for entry in selected}
        for memory_id, memory in memories.items():
            component = ranks[memory_id]
            self.session.add(
                RetrievalCandidate(
                    tenant_id=principal.tenant_id,
                    run_id=run.id,
                    relationship_id=request.relationship_id,
                    memory_id=memory_id,
                    memory_version=memory.version,
                    user_id=memory.user_id,
                    character_id=memory.character_id,
                    scope=memory.scope,
                    scenario_key=memory.scenario_key,
                    exact_rank=component[0],
                    fts_rank=component[1],
                    vector_rank=component[2],
                    temporal_rank=component[3],
                    fused_score=sum(1 / (RRF_K + rank) for rank in component if rank is not None),
                    selected=memory_id in selected_ids,
                    reason_code="rrf_selected" if memory_id in selected_ids else "budget_excluded",
                )
            )
        return run

    @staticmethod
    def _valid_request(request: object) -> bool:
        if (
            not isinstance(request, RetrievalRequest)
            or not isinstance(request.character_id, UUID)
            or not isinstance(request.relationship_id, UUID)
            or not isinstance(request.scope, Scope)
            or not isinstance(request.query, str)
            or not 1 <= len(request.query) <= 512
            or "\x00" in request.query
            or not isinstance(request.as_of, datetime)
            or request.as_of.tzinfo is None
            or request.as_of.utcoffset() is None
        ):
            return False
        if request.scope == Scope.real_world and request.scenario_id is not None:
            return False
        if request.scope == Scope.roleplay and not isinstance(request.scenario_id, UUID):
            return False
        return (
            all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in (request.per_list_k, request.final_k, request.max_characters)
            )
            and request.per_list_k <= 50
            and request.final_k <= 20
            and request.max_characters <= 8192
            and isinstance(request.kinds, tuple)
            and all(isinstance(item, str) and item in {"semantic_fact"} for item in request.kinds)
        )

    async def attach_supplied(
        self,
        principal: PrincipalContext,
        assistant_message_id: UUID,
        entries: tuple[RetrievedMemory, ...],
    ) -> None:
        if (
            not isinstance(principal, PrincipalContext)
            or not isinstance(assistant_message_id, UUID)
            or not isinstance(entries, tuple)
        ):
            raise DomainError("attribution_invalid")
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            assistant_relationship_id = await self.session.scalar(
                select(Conversation.relationship_id)
                .join(
                    Message,
                    and_(
                        Message.tenant_id == Conversation.tenant_id,
                        Message.conversation_id == Conversation.id,
                    ),
                )
                .where(
                    Message.tenant_id == principal.tenant_id,
                    Message.id == assistant_message_id,
                    Message.user_id == principal.user_id,
                    Message.role == "assistant",
                )
            )
            if assistant_relationship_id is None:
                raise DomainError("attribution_invalid")
            for entry in entries:
                if not isinstance(entry, RetrievedMemory):
                    raise DomainError("attribution_invalid")
                memory = await self.session.scalar(
                    select(Memory)
                    .where(
                        Memory.tenant_id == principal.tenant_id,
                        Memory.id == entry.memory_id,
                        Memory.user_id == principal.user_id,
                        Memory.relationship_id == assistant_relationship_id,
                    )
                    .with_for_update()
                )
                if (
                    memory is None
                    or memory.lifecycle_state
                    not in (LifecycleState.active, LifecycleState.historical)
                    or memory.deleted_at is not None
                ):
                    raise DomainError("attribution_memory_ineligible")
                self.session.add(
                    ResponseMemoryRef(
                        tenant_id=principal.tenant_id,
                        assistant_message_id=assistant_message_id,
                        relationship_id=memory.relationship_id,
                        memory_id=memory.id,
                        user_id=memory.user_id,
                        character_id=memory.character_id,
                        scope=memory.scope,
                        scenario_key=memory.scenario_key,
                        memory_version=memory.version,
                        usage_type="supplied",
                    )
                )
