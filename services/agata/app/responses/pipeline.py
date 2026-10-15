"""Fail-closed response orchestration over principal-bound retrieval results.

This module deliberately has no network adapter: the local provider is a deterministic
test seam and production routes are disabled unless an operator creates an exact route.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    AuditLog,
    Character,
    CharacterProfileVersion,
    Consent,
    Conversation,
    ConversationSegment,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySnapshot,
    Message,
    ResponseMemoryRef,
    ResponseRoutePolicy,
    ResponseRun,
    RetentionPolicy,
    UserCharacterSettings,
)
from app.domain.principal import PrincipalContext
from app.i18n.response_context import LocaleResponseFragment, resolve_locale_response_fragment
from app.jobs.outbox import OutboxRepository
from app.memory.persistent_graph import PersistentMemoryGraphService
from app.memory.precedence import TemporalMode
from app.memory.retrieval import (
    HybridRetrievalService,
    RetrievalRequest,
    RetrievalResult,
    RetrievedMemory,
)
from app.memory.segments import segment_hierarchy, segment_interval, summary_relevance
from app.memory.shared_history import (
    SharedHistoryContext,
    SharedHistoryEvidence,
    SharedHistoryService,
)
from app.memory.snapshots import (
    MemorySnapshotService,
    enqueue_relationship_note_refresh,
    snapshot_fingerprint,
)
from app.messages.crypto import (
    AeadCrypto,
    CiphertextEnvelope,
    canonical_tagged_fields,
    derive_hmac_subkey,
)
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    decrypt_text_from_storage,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
    memory_snapshot_content_aad,
    memory_value_aad,
    segment_summary_aad,
)
from app.messages.service import DomainError, MessageIngestionService
from app.responses.guard import ResponseGuard
from app.responses.protocols import PsychologyProtocol, select_psychology_protocol

RESPONSE_DOMAIN = b"gptgirls/response-request-fingerprint/v1"
CONTEXT_SCHEMA_VERSION = "context-v1"


def assistant_message_timestamp(
    user_timestamp: datetime, *, now: datetime | None = None
) -> datetime:
    """Order a generated reply after its incoming message in durable history."""
    generated_at = now or datetime.now(UTC)
    return max(generated_at, user_timestamp + timedelta(microseconds=1))


@dataclass(frozen=True, slots=True)
class ResponseRequest:
    conversation_id: UUID
    user_message_id: UUID
    idempotency_key: str
    as_of: datetime
    image_data_url: str | None = None


@dataclass(frozen=True, slots=True)
class QueryPlan:
    temporal_mode: TemporalMode
    exact_lookup: bool
    max_characters: int
    abstain: bool
    reason_code: str
    response_language: str


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class DialogueState:
    """Ephemeral, finite conversation orientation; it is never a memory write."""

    topic_code: str
    unresolved_question: bool
    response_goal: str


@dataclass(frozen=True, slots=True)
class CommunicationSettings:
    """Authoritative user-controlled expression settings, never Identity Core."""

    language: str | None = None
    response_length: str | None = None
    tone: str | None = None
    use_name: str | None = None
    initiative: str | None = None
    reminders: str | None = None
    roleplay: str | None = None
    media: str | None = None


@dataclass(frozen=True, slots=True)
class UntrustedMemoryBlock:
    memory_id: UUID
    version: int
    subject: str | None
    predicate: str | None
    value: str | None
    observed_at: datetime | None = None
    reason_code: str = "untrusted_memory_data"
    evidence_state: str = "inferred"
    lifecycle_state: str = "active"
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    truth_role: str = "current"


@dataclass(frozen=True, slots=True)
class ResponseContext:
    profile_version: int
    route_tuple: tuple[str, str, str, str, str]
    current_message: str
    memories: tuple[UntrustedMemoryBlock, ...] = ()
    recall_context: tuple[UntrustedMemoryBlock, ...] = ()
    conversation_history: tuple[ConversationEntry, ...] = ()
    schema_version: str = CONTEXT_SCHEMA_VERSION
    locale_fragment: LocaleResponseFragment | None = None
    personality: dict[str, object] | None = None
    speech_style: dict[str, object] | None = None
    backstory: dict[str, object] | None = None
    boundaries: dict[str, object] | None = None
    response_language: str = "ru"
    image_data_url: str | None = None
    conversation_summaries: tuple[str, ...] = ()
    relationship_note: str | None = None
    relationship_summary: str | None = None
    current_context_summary: str | None = None
    snapshot_refs: tuple[tuple[UUID, int, str], ...] = ()
    graph_context: tuple[str, ...] = ()
    as_of: datetime | None = None
    dialogue_state: DialogueState = DialogueState("general", False, "answer")
    psychology_protocol: PsychologyProtocol | None = None
    shared_history: tuple[UntrustedMemoryBlock, ...] = ()
    communication_settings: CommunicationSettings = CommunicationSettings()
    character_name: str = "Персонаж"
    source_message_id: UUID | None = None
    source_idempotency_key: str | None = None
    background_source_message_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderEnvelope:
    text: str
    cited_memory_ids: tuple[UUID, ...]
    provider_request_id: str


def _bounded_strings(values: object, *, count: int, each: int) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        return ()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.replace("\x00", "").replace("<|", "＜|")[:each]
        if cleaned:
            output.append(cleaned)
        if len(output) >= count:
            break
    return tuple(output)


def _bounded_untrusted_text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = (
        value.replace("\x00", "").replace("<|", "＜|").replace("\r", " ").replace("\n", " ")
    )[:limit]
    return cleaned if cleaned else None


def _bounded_history(values: object) -> tuple[ConversationEntry, ...]:
    if not isinstance(values, tuple):
        return ()
    # The database loader returns chronological history. Bound from the newest
    # end so the provider sees the current conversation state after long chats.
    output: list[ConversationEntry] = []
    used = 0
    for value in reversed(values[-12:]):
        if not isinstance(value, ConversationEntry) or value.role not in {
            "user",
            "assistant",
            "system",
        }:
            continue
        content = value.content.replace("\x00", "").replace("<|", "＜|")[:600]
        if not content or used + len(content) > 6000:
            continue
        output.append(ConversationEntry(value.role, content))
        used += len(content)
    return tuple(reversed(output))


def _derive_dialogue_state(
    current_message: str, history: tuple[ConversationEntry, ...]
) -> DialogueState:
    """Build a small deterministic orientation without persisting user text."""
    lowered = current_message.casefold()
    topic = (
        "wellbeing"
        if any(token in lowered for token in ("тревог", "одинок", "стресс", "panic", "lonely"))
        else "relationship"
        if any(token in lowered for token in ("отношен", "расстав", "ссор", "relationship"))
        else "work"
        if any(token in lowered for token in ("работ", "проект", "job", "work"))
        else "general"
    )
    unresolved = "?" in current_message or current_message.rstrip().endswith("…")
    if not unresolved and history:
        last = history[-1]
        unresolved = last.role == "user" and "?" in last.content
    goal = "clarify" if unresolved else "support" if topic == "wellbeing" else "answer"
    return DialogueState(topic, unresolved, goal)


class ResponseProvider(Protocol):
    provider: str
    model: str

    async def complete(self, context: ResponseContext) -> object: ...


class DeterministicFakeResponseProvider:
    provider = "local_fake"
    model = "deterministic-response-v1"

    async def complete(self, context: ResponseContext) -> object:
        if not isinstance(context, ResponseContext):
            raise ValueError("invalid_context")
        # The fake deliberately treats all memory as data and never executes it.
        citations = tuple(item.memory_id for item in context.memories[:1])
        fragment = context.locale_fragment
        if fragment is None:
            # Preserve the transport-neutral Stage-7 unit contract for callers
            # that exercise the provider in isolation. Production orchestration
            # always supplies the authoritative fragment before this call.
            return ProviderEnvelope("I can help with that.", citations, "local-deterministic")
        return ProviderEnvelope(
            f"[{fragment.locale_code}] {fragment.expression} I can help with that.",
            citations,
            "local-deterministic",
        )


class QueryPlanner:
    """Conservative planner: no provider/model text may select identity or scope."""

    def plan(self, query: object, as_of: object) -> QueryPlan:
        if not isinstance(query, str) or not 1 <= len(query) <= 512 or "\x00" in query:
            return QueryPlan(TemporalMode.CURRENT, False, 0, True, "invalid_query", "unknown")
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            return QueryPlan(TemporalMode.CURRENT, False, 0, True, "invalid_time_input", "unknown")
        lowered = query.casefold()
        historical_cues = (
            "раньше", "до этого", "прежде", "в прошлом", "когда я работал",
            "когда работал", "was", "used to", "previously", "before",
        )
        temporal = TemporalMode.HISTORICAL if any(cue in lowered for cue in historical_cues) else TemporalMode.CURRENT
        return QueryPlan(temporal, len(query) <= 128, 2048, False, "ok", _language_code(query))


class ContextBuilder:
    @staticmethod
    def build(
        profile: CharacterProfileVersion,
        current_message: str,
        results: tuple[RetrievedMemory, ...],
        plan: QueryPlan,
        locale_fragment: LocaleResponseFragment | None = None,
        recall_context: tuple[UntrustedMemoryBlock, ...] = (),
        conversation_history: tuple[ConversationEntry, ...] = (),
        image_data_url: str | None = None,
        conversation_summaries: tuple[str, ...] = (),
        relationship_note: str | None = None,
        relationship_summary: str | None = None,
        current_context_summary: str | None = None,
        snapshot_refs: tuple[tuple[UUID, int, str], ...] = (),
        graph_context: tuple[str, ...] = (),
        as_of: datetime | None = None,
        shared_history: tuple[UntrustedMemoryBlock, ...] = (),
        communication_settings: CommunicationSettings = CommunicationSettings(),
        character_name: str = "Персонаж",
        source_message_id: UUID | None = None,
        source_idempotency_key: str | None = None,
    ) -> ResponseContext:
        if (
            not isinstance(profile, CharacterProfileVersion)
            or not isinstance(current_message, str)
            or not isinstance(results, tuple)
            or plan.abstain
        ):
            raise DomainError("context_invalid")
        blocks: list[UntrustedMemoryBlock] = []
        used = 0
        for item in results:
            if not isinstance(item, RetrievedMemory) or item.value is None:
                continue
            # A challenged record remains in the authoritative ledger for
            # provenance, but it is not context evidence and must never shape a
            # response as fact.
            if item.evidence_state == EvidenceState.disputed.value:
                continue
            # Control delimiters cannot construct a new control channel.
            value = item.value.replace("\x00", "").replace("<|", "＜|")
            if used + len(value) > plan.max_characters:
                continue
            used += len(value)
            blocks.append(
                UntrustedMemoryBlock(
                    item.memory_id,
                    item.version,
                    _bounded_untrusted_text(item.subject, limit=80),
                    _bounded_untrusted_text(item.predicate, limit=80),
                    value,
                    item.observed_at,
                    evidence_state=item.evidence_state,
                    lifecycle_state=item.lifecycle_state,
                    valid_from=item.valid_from,
                    valid_until=item.valid_until,
                    truth_role="historical" if item.lifecycle_state == LifecycleState.historical.value else "current",
                )
            )
        bounded_history = _bounded_history(conversation_history)
        bounded_summaries = _bounded_strings(conversation_summaries, count=3, each=500)
        bounded_note = _bounded_untrusted_text(relationship_note, limit=1800)
        bounded_relationship_summary = _bounded_untrusted_text(relationship_summary, limit=1800)
        bounded_current_context = _bounded_untrusted_text(current_context_summary, limit=1200)
        bounded_graph = _bounded_strings(graph_context, count=8, each=160)
        dialogue = _derive_dialogue_state(current_message, bounded_history)
        return ResponseContext(
            profile_version=profile.version,
            route_tuple=("", "", "", "", ""),
            current_message=current_message.replace("\x00", ""),
            memories=tuple(blocks),
            recall_context=tuple(
                UntrustedMemoryBlock(
                    item.memory_id,
                    item.version,
                    _bounded_untrusted_text(item.subject, limit=80),
                    _bounded_untrusted_text(item.predicate, limit=80),
                    _bounded_untrusted_text(item.value, limit=300),
                    item.observed_at,
                    reason_code="recall_memory_data",
                    evidence_state=item.evidence_state,
                    lifecycle_state=item.lifecycle_state,
                    valid_from=item.valid_from,
                    valid_until=item.valid_until,
                    truth_role=item.truth_role,
                )
                for item in recall_context[:5]
                if isinstance(item, UntrustedMemoryBlock)
                and item.evidence_state
                in {
                    EvidenceState.explicit.value,
                    EvidenceState.confirmed.value,
                    EvidenceState.inferred.value,
                }
                and _bounded_untrusted_text(item.value, limit=300) is not None
            ),
            response_language=plan.response_language,
            locale_fragment=locale_fragment,
            personality=_bounded_profile_data(profile.personality),
            speech_style=_bounded_profile_data(profile.speech_style),
            backstory=_bounded_profile_data(profile.backstory),
            boundaries=_bounded_profile_data(profile.boundaries),
            conversation_history=bounded_history,
            image_data_url=image_data_url,
            conversation_summaries=bounded_summaries,
            relationship_note=bounded_note,
            relationship_summary=bounded_relationship_summary,
            current_context_summary=bounded_current_context,
            snapshot_refs=tuple(snapshot_refs[:4]),
            graph_context=bounded_graph,
            as_of=as_of,
            dialogue_state=dialogue,
            psychology_protocol=select_psychology_protocol(current_message),
            shared_history=tuple(
                UntrustedMemoryBlock(
                    item.memory_id,
                    item.version,
                    _bounded_untrusted_text(item.subject, limit=80),
                    _bounded_untrusted_text(item.predicate, limit=80),
                    _bounded_untrusted_text(item.value, limit=300),
                    item.observed_at,
                    reason_code="shared_history_data",
                    evidence_state=item.evidence_state,
                    lifecycle_state=item.lifecycle_state,
                    truth_role="relationship_history",
                )
                for item in shared_history[:10]
                if isinstance(item, UntrustedMemoryBlock)
                and item.evidence_state
                in {EvidenceState.explicit.value, EvidenceState.confirmed.value}
                and item.lifecycle_state == LifecycleState.active.value
            ),
            communication_settings=(
                communication_settings
                if isinstance(communication_settings, CommunicationSettings)
                else CommunicationSettings()
            ),
            character_name=(
                character_name[:255]
                if isinstance(character_name, str) and 1 <= len(character_name) <= 255
                else "Персонаж"
            ),
            source_message_id=source_message_id,
            source_idempotency_key=source_idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class ResponseResult:
    assistant_message_id: UUID | None
    status: str
    reason_code: str
    replayed: bool = False


class ResponseOrchestrator:
    """Atomic assistant message + supplied/cited refs after a final eligibility recheck."""

    def __init__(
        self,
        session: AsyncSession,
        crypto: AeadCrypto,
        retriever: HybridRetrievalService,
        provider: ResponseProvider,
        *,
        hmac_key: bytes,
        provider_timeout_seconds: int = 2,
    ) -> None:
        if (
            not isinstance(provider_timeout_seconds, int)
            or isinstance(provider_timeout_seconds, bool)
            or not 1 <= provider_timeout_seconds <= 30
        ):
            raise ValueError("provider_timeout_invalid")
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise DomainError("response_key_invalid")
        self.session, self.crypto, self.retriever, self.provider = (
            session,
            crypto,
            retriever,
            provider,
        )
        self.planner = QueryPlanner()
        self._key = derive_hmac_subkey(hmac_key, RESPONSE_DOMAIN)
        self.provider_timeout_seconds = provider_timeout_seconds

    async def respond(
        self, principal: PrincipalContext, request: ResponseRequest
    ) -> ResponseResult:
        if not isinstance(principal, PrincipalContext) or not self._valid_request(request):
            return ResponseResult(None, "rejected", "invalid_request")
        fingerprint = self._fingerprint(principal, request)
        # Read/decrypt only after server-side conversation/message ownership checks.
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            conversation, user_message, profile, route = await self._load_base(principal, request)
            existing = await self.session.scalar(
                select(ResponseRun).where(
                    ResponseRun.tenant_id == principal.tenant_id,
                    ResponseRun.user_id == principal.user_id,
                    ResponseRun.request_fingerprint == fingerprint,
                )
            )
            if existing is not None:
                return ResponseResult(
                    existing.assistant_message_id, existing.status, existing.reason_code, True
                )
            if conversation is None or user_message is None or profile is None or route is None:
                return ResponseResult(None, "abstained", "route_disabled")
            character_name = await self.session.scalar(
                select(Character.name).where(
                    Character.tenant_id == principal.tenant_id,
                    Character.id == conversation.character_id,
                )
            )
            if not await self._active_consent(principal, "chat_history"):
                return ResponseResult(None, "rejected", "guard_denied")
            plaintext = self._decrypt_message(principal, user_message)
            if plaintext is None:
                return ResponseResult(None, "rejected", "guard_denied")
            locale_fragment = await resolve_locale_response_fragment(
                self.session, principal, conversation.character_id
            )
            conversation_history = await self._recent_messages(
                principal, conversation, current_message_id=user_message.id
            )
            conversation_summaries = await self._recent_summaries(
                principal, conversation, plaintext, request.as_of
            )
            snapshot_service = MemorySnapshotService(self.session, self.crypto)
            resolved_snapshots: list[MemorySnapshot] = []
            snapshot_text: dict[str, str] = {}
            snapshot_specs = (
                ("relationship_note", "relationship", "long_term_memory"),
                ("relationship_summary", "relationship", "long_term_memory"),
                ("current_context_summary", f"conversation:{conversation.id}", "chat_history"),
                ("conversation_summary", f"conversation:{conversation.id}", "chat_history"),
            )
            for snapshot_type, snapshot_key, consent_name in snapshot_specs:
                snapshot = await snapshot_service.resolve_current(
                    tenant_id=principal.tenant_id,
                    relationship_id=conversation.relationship_id,
                    snapshot_type=snapshot_type,
                    snapshot_key=snapshot_key,
                    user_id=principal.user_id,
                    required_consent=consent_name,
                )
                if snapshot is None:
                    continue
                payload = decrypt_json_from_storage(
                    snapshot.content, memory_snapshot_content_aad(snapshot), crypto=self.crypto
                )
                if not isinstance(payload, dict):
                    continue
                if snapshot_type == "relationship_note":
                    facts = payload.get("stable_facts", [])
                    parts = [f"Имя: {payload['preferred_name']}" for _ in [0] if isinstance(payload.get("preferred_name"), str)]
                    if isinstance(facts, list):
                        parts.extend(f"{item.get('predicate', 'факт')}: {item.get('value', '')}" for item in facts[:10] if isinstance(item, dict) and isinstance(item.get("value"), str))
                    relationship_context = payload.get("important_relationship_context", [])
                    if isinstance(relationship_context, list):
                        parts.extend(str(item)[:180] for item in relationship_context[:4] if isinstance(item, str))
                    rendered = "; ".join(parts)[:1800]
                elif snapshot_type in {"relationship_summary", "conversation_summary"}:
                    rendered = str(payload.get("recent_summary") or payload.get("summary") or "")[:1000]
                else:
                    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1200]
                if rendered:
                    snapshot_text[snapshot_type] = rendered
                    resolved_snapshots.append(snapshot)
            if "relationship_note" not in snapshot_text and await snapshot_service.consent_allowed(
                principal.tenant_id, principal.user_id, "long_term_memory"
            ):
                control = await self.session.scalar(select(MemoryControlState).where(
                    MemoryControlState.tenant_id == principal.tenant_id,
                    MemoryControlState.relationship_id == conversation.relationship_id,
                ).limit(1))
                if control is not None:
                    await enqueue_relationship_note_refresh(
                        self.session,
                        tenant_id=principal.tenant_id,
                        relationship_id=conversation.relationship_id,
                        creation_epoch=control.deletion_epoch,
                    )
            communication_settings = await self._communication_settings(principal, conversation)
            disputed_memory_values = await self._disputed_memory_values(principal, conversation)
            shared_context = SharedHistoryContext(
                principal.tenant_id,
                principal.user_id,
                conversation.character_id,
                conversation.scope,
                conversation.scenario_id,
                conversation.relationship_id,
            )
            shared_history = await SharedHistoryService(self.session).load_in_transaction(
                shared_context
            )
        plan = self.planner.plan(plaintext, request.as_of)
        if plan.abstain:
            return ResponseResult(None, "abstained", plan.reason_code)
        try:
            retrieval = await self.retriever.retrieve(
                principal,
                RetrievalRequest(
                    conversation.character_id,
                    conversation.relationship_id,
                    conversation.scope,
                    conversation.scenario_id,
                    plaintext,
                    request.as_of,
                    max_characters=plan.max_characters,
                    temporal_mode=plan.temporal_mode,
                ),
            )
        except Exception:
            # Memory retrieval is an enrichment layer, never a reason to drop a
            # valid chat turn. Continue with an empty, typed result; the
            # authoritative message remains stored and the next turn can retry
            # retrieval after the provider/index recovers.
            retrieval = RetrievalResult((), "retrieval_unavailable", None)
        # Second broader retrieval pass (recall tool) — lower confidence threshold
        recall_results: tuple[RetrievedMemory, ...] = ()
        if len(retrieval.results) <= 2:
            try:
                recall_retrieval = await self.retriever.retrieve(
                    principal,
                    RetrievalRequest(
                        conversation.character_id,
                        conversation.relationship_id,
                        conversation.scope,
                        conversation.scenario_id,
                        plaintext,
                        request.as_of,
                        max_characters=plan.max_characters * 3,
                        temporal_mode=plan.temporal_mode,
                    ),
                )
                recall_results = recall_retrieval.results
            except Exception:
                pass
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            graph_context = await PersistentMemoryGraphService(
                self.session, crypto=self.crypto
            ).load_context(
                tenant_id=principal.tenant_id,
                relationship_id=conversation.relationship_id,
                user_id=principal.user_id,
                character_id=conversation.character_id,
                scope=conversation.scope,
                scenario_key=conversation.scenario_key,
                as_of=request.as_of,
                temporal_mode=plan.temporal_mode.value,
                seeds=retrieval.results,
            )
        context = ContextBuilder.build(
            profile,
            plaintext,
            retrieval.results,
            plan,
            locale_fragment,
            image_data_url=request.image_data_url,
            recall_context=tuple(
                UntrustedMemoryBlock(
                    item.memory_id,
                    item.version,
                    _bounded_untrusted_text(item.subject, limit=80),
                    _bounded_untrusted_text(item.predicate, limit=80),
                    _bounded_untrusted_text(item.value, limit=300),
                    item.observed_at,
                    reason_code="recall_memory_data",
                    evidence_state=item.evidence_state,
                    lifecycle_state=item.lifecycle_state,
                    valid_from=item.valid_from,
                    valid_until=item.valid_until,
                    truth_role="historical" if item.lifecycle_state == LifecycleState.historical.value else "current",
                )
                for item in recall_results[:5]
                if isinstance(item, RetrievedMemory)
                and isinstance(item.value, str)
                and item.evidence_state != EvidenceState.disputed.value
            ),
            conversation_history=conversation_history,
            graph_context=graph_context,
            as_of=request.as_of,
            shared_history=tuple(
                UntrustedMemoryBlock(
                    item.memory_id,
                    item.memory_version,
                    None,
                    "shared_history",
                    item.value,
                    item.observed_at,
                    evidence_state=item.evidence_state,
                    lifecycle_state="active",
                    truth_role="relationship_history",
                )
                for item in shared_history
            ),
            communication_settings=communication_settings,
            character_name=character_name if isinstance(character_name, str) else "Персонаж",
            source_message_id=request.user_message_id,
            source_idempotency_key=request.idempotency_key,
            relationship_note=snapshot_text.get("relationship_note"),
            relationship_summary=snapshot_text.get("relationship_summary"),
            current_context_summary=snapshot_text.get("current_context_summary"),
            conversation_summaries=((snapshot_text["conversation_summary"],) if snapshot_text.get("conversation_summary") else ()) + conversation_summaries,
            snapshot_refs=tuple((item.id, item.generation, item.snapshot_type) for item in resolved_snapshots),
        )
        try:
            envelope = await asyncio.wait_for(
                self.provider.complete(context),
                timeout=float(self.provider_timeout_seconds),
            )
        except Exception as exc:
            from app.responses.mac_worker import WorkerInferencePending, WorkerInferenceUnavailable

            if isinstance(exc, WorkerInferencePending):
                return ResponseResult(None, "pending", "inference_pending")
            if isinstance(exc, WorkerInferenceUnavailable):
                return ResponseResult(None, "rejected", "provider_unavailable")
            try:
                envelope = await asyncio.wait_for(
                    self.provider.complete(context),
                    timeout=float(self.provider_timeout_seconds),
                )
            except Exception:
                return ResponseResult(None, "rejected", "provider_unavailable")
        if not self._valid_envelope(envelope):
            return ResponseResult(None, "rejected", "provider_invalid")
        envelope = cast(ProviderEnvelope, envelope)
        # Identity is an invariant, not a prompt preference. Do not persist
        # output that leaks provider branding.
        if not self._identity_allowed(context, envelope.text):
            try:
                envelope = await asyncio.wait_for(
                    self.provider.complete(context),
                    timeout=float(self.provider_timeout_seconds),
                )
            except Exception:
                return ResponseResult(None, "rejected", "identity_violation")
            envelope = cast(ProviderEnvelope, envelope)
            if not self._valid_envelope(envelope) or not self._identity_allowed(
                context, envelope.text
            ):
                return ResponseResult(None, "rejected", "identity_violation")
        # Response Guard — post-generation safety/accuracy filter
        guard = ResponseGuard(
            known_facts=[m.value for m in retrieval.results if m.value and len(m.value) < 200],
            disputed_memory_values=disputed_memory_values,
        )
        guard_result = guard.check(envelope.text)
        if not guard_result.passed:
            # A provider may add an unsupported factual detail on its first
            # attempt. Retry the same configured provider once with the same
            # authoritative context; never deliver the rejected text.
            try:
                retry_envelope = await asyncio.wait_for(
                    self.provider.complete(context),
                    timeout=float(self.provider_timeout_seconds),
                )
            except Exception:
                return ResponseResult(None, "rejected", f"guard_{guard_result.reason}")
            if not self._valid_envelope(retry_envelope):
                return ResponseResult(None, "rejected", "provider_invalid")
            retry_envelope = cast(ProviderEnvelope, retry_envelope)
            if not self._identity_allowed(context, retry_envelope.text):
                return ResponseResult(None, "rejected", "identity_violation")
            retry_guard = guard.check(retry_envelope.text)
            if not retry_guard.passed:
                return ResponseResult(None, "rejected", f"guard_{retry_guard.reason}")
            envelope = retry_envelope
        result = await self._commit(
            principal,
            request,
            fingerprint,
            envelope,
            retrieval.results,
            retrieval.run_id,
            locale_fragment,
            shared_history,
            shared_context,
            context.snapshot_refs,
        )
        # Memory extraction is durable and asynchronous.  MessageIngestionService
        # already enqueues a typed memory_extract job; the memory worker owns the
        # shadow -> promotion pipeline.  Never write authoritative Memory rows from
        # the response request itself.
        if result.assistant_message_id is not None:
            # Keep lifecycle cleanup on the request transaction boundary.  A
            # detached task could outlive the session and race its close.
            await self._run_retention_if_needed(principal)
        return result

    @staticmethod
    def _identity_allowed(context: ResponseContext, text: str) -> bool:
        lowered = text.casefold()
        forbidden = (
            "chatgpt",
            "openai",
            "я ии-ассистент",
            "я и-ассистент",
            "языковая модель",
            "language model",
        )
        return not any(marker in lowered for marker in forbidden)

    async def _commit(
        self,
        principal: PrincipalContext,
        request: ResponseRequest,
        fingerprint: str,
        envelope: ProviderEnvelope,
        supplied: tuple[RetrievedMemory, ...],
        retrieval_run_id: UUID | None,
        locale_fragment: LocaleResponseFragment,
        shared_history: tuple[SharedHistoryEvidence, ...],
        shared_context: SharedHistoryContext,
        snapshot_refs: tuple[tuple[UUID, int, str], ...] = (),
    ) -> ResponseResult:
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": (
                        f"response:{principal.tenant_id}:{principal.user_id}:"
                        f"{request.idempotency_key}"
                    )
                },
            )
            existing = await self.session.scalar(
                select(ResponseRun).where(
                    ResponseRun.tenant_id == principal.tenant_id,
                    ResponseRun.user_id == principal.user_id,
                    ResponseRun.request_fingerprint == fingerprint,
                )
            )
            if existing is not None:
                return ResponseResult(
                    existing.assistant_message_id, existing.status, existing.reason_code, True
                )
            existing_message = await self.session.scalar(
                select(Message).where(
                    Message.tenant_id == principal.tenant_id,
                    Message.idempotency_key == f"assistant:{request.idempotency_key}",
                )
            )
            if existing_message is not None:
                if existing_message.request_fingerprint == fingerprint:
                    return ResponseResult(existing_message.id, "completed", "ok", True)
                return ResponseResult(None, "rejected", "idempotency_conflict")
            conversation, user_message, profile, route = await self._load_base(
                principal, request, lock=True
            )
            if (
                conversation is None
                or user_message is None
                or profile is None
                or route is None
                or not await self._active_consent(principal, "chat_history")
            ):
                return ResponseResult(None, "rejected", "guard_denied")
            current_fragment = await resolve_locale_response_fragment(
                self.session, principal, conversation.character_id
            )
            if current_fragment != locale_fragment:
                return ResponseResult(None, "rejected", "stale_locale")
            if not await SharedHistoryService(self.session).verify_in_transaction(
                shared_context, shared_history
            ):
                return ResponseResult(None, "rejected", "stale_shared_history")
            if snapshot_refs:
                current_epoch = await self.session.scalar(select(MemoryControlState.deletion_epoch).where(
                    MemoryControlState.tenant_id == principal.tenant_id,
                    MemoryControlState.relationship_id == conversation.relationship_id,
                ))
                now = datetime.now(UTC)
                required_consents = {
                    "relationship_note": "long_term_memory",
                    "relationship_summary": "long_term_memory",
                    "current_context_summary": "chat_history",
                    "conversation_summary": "chat_history",
                }
                for snapshot_id, generation, snapshot_type in snapshot_refs:
                    allowed = await self._active_consent(principal, required_consents.get(snapshot_type, ""))
                    current_snapshot = await self.session.scalar(select(MemorySnapshot).where(
                        MemorySnapshot.tenant_id == principal.tenant_id,
                        MemorySnapshot.id == snapshot_id,
                        MemorySnapshot.relationship_id == conversation.relationship_id,
                        MemorySnapshot.user_id == principal.user_id,
                        MemorySnapshot.generation == generation,
                        MemorySnapshot.creation_epoch == current_epoch,
                        MemorySnapshot.invalidated_at.is_(None),
                        or_(MemorySnapshot.expires_at.is_(None), MemorySnapshot.expires_at > now),
                    ).with_for_update())
                    if not allowed or current_snapshot is None:
                        return ResponseResult(None, "rejected", "stale_snapshot")
            verified = await self._verify_supplied(principal, conversation, supplied)
            supplied_ids = {item.memory_id for item in verified}
            if not set(envelope.cited_memory_ids).issubset(supplied_ids):
                return ResponseResult(None, "rejected", "citation_invalid")
            assistant_id = uuid4()
            encrypted = self.crypto.encrypt(
                envelope.text,
                MessageIngestionService._aad_for(
                    principal.tenant_id, principal.user_id, conversation.id, assistant_id
                ),
            )
            language = "ru" if locale_fragment.locale_code == "ru-RU" else "en"
            message = Message(
                id=assistant_id,
                tenant_id=principal.tenant_id,
                conversation_id=conversation.id,
                user_id=principal.user_id,
                character_id=conversation.character_id,
                scope=conversation.scope,
                scenario_id=conversation.scenario_id,
                idempotency_key=f"assistant:{request.idempotency_key}",
                role="assistant",
                channel_delivery_status="draft" if conversation.source_system=="gsales" else "confirmed",
                content_ciphertext=encrypted.ciphertext,
                content_nonce=encrypted.nonce,
                content_key_version=encrypted.key_version,
                request_fingerprint=fingerprint,
                consent_id=user_message.consent_id,
                retention_policy_id=user_message.retention_policy_id,
                occurred_at=assistant_message_timestamp(user_message.occurred_at),
                language_code=language,
            )
            self.session.add(message)
            run = ResponseRun(
                tenant_id=principal.tenant_id,
                relationship_id=conversation.relationship_id,
                user_id=principal.user_id,
                character_id=conversation.character_id,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                assistant_message_id=assistant_id,
                scope=conversation.scope,
                scenario_id=conversation.scenario_id,
                route_policy_id=route.id,
                retrieval_run_id=retrieval_run_id,
                request_fingerprint=fingerprint,
                status="completed",
                reason_code="ok",
                language_code=language,
            )
            self.session.add(run)
            await self.session.flush()
            for item in verified:
                self.session.add(
                    ResponseMemoryRef(
                        tenant_id=principal.tenant_id,
                        assistant_message_id=assistant_id,
                        relationship_id=conversation.relationship_id,
                        memory_id=item.memory_id,
                        user_id=principal.user_id,
                        character_id=conversation.character_id,
                        scope=conversation.scope,
                        scenario_key=conversation.scenario_key,
                        memory_version=item.version,
                        usage_type="supplied",
                        response_run_id=run.id,
                    )
                )
                if item.memory_id in envelope.cited_memory_ids:
                    self.session.add(
                        ResponseMemoryRef(
                            tenant_id=principal.tenant_id,
                            assistant_message_id=assistant_id,
                            relationship_id=conversation.relationship_id,
                            memory_id=item.memory_id,
                            user_id=principal.user_id,
                            character_id=conversation.character_id,
                            scope=conversation.scope,
                            scenario_key=conversation.scenario_key,
                            memory_version=item.version,
                            usage_type="cited",
                            response_run_id=run.id,
                        )
                    )
            for snapshot_id, generation, _snapshot_type in snapshot_refs:
                self.session.add(ResponseMemoryRef(
                    tenant_id=principal.tenant_id,
                    assistant_message_id=assistant_id,
                    relationship_id=conversation.relationship_id,
                    snapshot_id=snapshot_id,
                    user_id=principal.user_id,
                    character_id=conversation.character_id,
                    scope=conversation.scope,
                    scenario_key=conversation.scenario_key,
                    snapshot_generation=generation,
                    ref_type="snapshot",
                    usage_type="supplied",
                    response_run_id=run.id,
                ))
            self.session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor_type="internal",
                    actor_id=principal.actor_id,
                    action="response_committed",
                    object_id=assistant_id,
                    metadata_json={"response_run_id": str(run.id), "reason": "ok"},
                )
            )
            control = await self.session.scalar(
                select(MemoryControlState)
                .where(
                    MemoryControlState.tenant_id == principal.tenant_id,
                    MemoryControlState.relationship_id == conversation.relationship_id,
                    MemoryControlState.user_id == principal.user_id,
                )
                .limit(1)
            )
            if control is not None:
                await OutboxRepository(self.session).enqueue(
                    tenant_id=principal.tenant_id,
                    job_type="conversation_summary",
                    dedupe_key=f"summary:{conversation.id}:{assistant_id}",
                    aggregate_id=conversation.id,
                    creation_epoch=control.deletion_epoch,
                    memory_control_state_id=control.id,
                    payload={
                        "conversation_id": str(conversation.id),
                        "user_id": str(principal.user_id),
                        "character_id": str(conversation.character_id),
                    },
                )
            return ResponseResult(assistant_id, "completed", "ok")

    async def _extract_facts(
        self,
        principal: PrincipalContext,
        conversation: Conversation,
        user_message_plaintext: str,
        assistant_response: str,
    ) -> None:
        """Retired direct-write path; durable shadow promotion is authoritative."""
        del principal, conversation, user_message_plaintext, assistant_response
        return

    async def _summarize_conversation(
        self,
        principal: PrincipalContext,
        conversation: Conversation,
    ) -> None:
        """Create conversation segment summary every ~10 messages.

        Architecture ref: GPTGIRLS_ARCHITECTURE_V2.md section 3.4 - History.
        """
        # V7 summary creation is an outbox worker concern.  This predecessor
        # must not construct a provider prompt or write a derived view from a
        # response-session side path.
        return
        try:
            from app.db.base import SessionFactory
            from app.db.models import MemoryControlState

            async with SessionFactory() as db_session:
                async with db_session.begin():
                    await set_tenant_context(db_session, str(principal.tenant_id))

                    # Check if we have enough messages for a new segment
                    existing_segments = await db_session.scalar(
                        select(func.count())
                        .select_from(ConversationSegment)
                        .where(
                            ConversationSegment.tenant_id == principal.tenant_id,
                            ConversationSegment.conversation_id == conversation.id,
                        )
                    )
                    if existing_segments is not None and existing_segments >= 3:
                        return  # Already have enough segments

                    # Get last 10 messages for summarization
                    rows = (
                        await db_session.scalars(
                            select(Message)
                            .where(
                                Message.tenant_id == principal.tenant_id,
                                Message.conversation_id == conversation.id,
                                Message.deleted_at.is_(None),
                            )
                            .order_by(Message.occurred_at.desc())
                            .limit(10)
                        )
                    ).all()

                    if len(rows) < 5:
                        return  # Not enough messages to summarize

                    # Decrypt messages
                    messages_text = []
                    for msg in reversed(rows):
                        try:
                            if msg.content_nonce is None or msg.content_key_version is None:
                                continue
                            content = self.crypto.decrypt(
                                CiphertextEnvelope(
                                    msg.content_ciphertext,
                                    msg.content_nonce,
                                    msg.content_key_version,
                                ),
                                MessageIngestionService._aad_for(
                                    principal.tenant_id, msg.user_id, conversation.id, msg.id
                                ),
                            )
                            prefix = "User" if msg.role == "user" else "Character"
                            messages_text.append(f"{prefix}: {content[:200]}")
                        except Exception:
                            continue

                    if not messages_text:
                        return

                    # Ask DeepSeek to summarize
                    prompt = (
                        "Summarize this conversation in 2-3 sentences in Russian. "
                        "Focus on topics discussed, user's emotional state, and any important decisions. "
                        "Be concise.\n\n" + "\n".join(messages_text[-10:])
                    )

                    ctx = ResponseContext(
                        profile_version=0,
                        route_tuple=("", "", "", "", ""),
                        current_message=prompt,
                        memories=(),
                        response_language="ru",
                        personality={"name": "Summarizer"},
                        speech_style={},
                    )
                    envelope = await asyncio.wait_for(
                        self.provider.complete(ctx),
                        timeout=float(self.provider_timeout_seconds),
                    )

                    if not isinstance(envelope, ProviderEnvelope) or not envelope.text:
                        return

                    summary = envelope.text[:1000]
                    keyphrases = [w for w in summary.split() if len(w) > 3][:5]
                    started_at, ended_at = segment_interval(row.occurred_at for row in rows)
                    hierarchy = segment_hierarchy(summary, started_at, ended_at)

                    # Save segment
                    ctrl = await db_session.scalar(
                        select(MemoryControlState)
                        .where(
                            MemoryControlState.tenant_id == principal.tenant_id,
                            MemoryControlState.relationship_id == conversation.relationship_id,
                            MemoryControlState.user_id == principal.user_id,
                        )
                        .limit(1)
                    )
                    epoch = ctrl.deletion_epoch if ctrl is not None else 0

                    segment_id = uuid4()
                    db_session.add(
                        ConversationSegment(
                            id=segment_id,
                            tenant_id=principal.tenant_id,
                            conversation_id=conversation.id,
                            summary=encrypt_text_for_storage(
                                summary,
                                content_aad(
                                    entity="conversation_segments",
                                    tenant_id=principal.tenant_id,
                                    user_id=principal.user_id,
                                    character_id=conversation.character_id,
                                    scope=conversation.scope,
                                    scenario_id=conversation.scenario_id,
                                    record_id=segment_id,
                                    field="summary",
                                ),
                                crypto=self.crypto,
                            ),
                            keyphrases=encrypt_json_for_storage(
                                keyphrases,
                                content_aad(
                                    entity="conversation_segments",
                                    tenant_id=principal.tenant_id,
                                    user_id=principal.user_id,
                                    character_id=conversation.character_id,
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
                    )
                    await db_session.flush()

                    payload = {
                        "summary": summary,
                        "keyphrases": keyphrases,
                        "hierarchy": hierarchy,
                        "segment_id": str(segment_id),
                    }
                    await MemorySnapshotService(db_session, self.crypto).upsert(
                        tenant_id=principal.tenant_id,
                        relationship=conversation,
                        snapshot_type="conversation_summary",
                        snapshot_key=f"conversation:{conversation.id}",
                        content=payload,
                        source_fingerprint=snapshot_fingerprint(
                            "conversation_summary", conversation.id, epoch, str(segment_id)
                        ),
                        creation_epoch=epoch,
                        source_segment_ids=[segment_id],
                        conversation_id=conversation.id,
                    )
        except Exception:
            pass  # Non-critical: never block main flow

    async def _run_retention_if_needed(self, principal: PrincipalContext) -> None:
        """Run retention cleanup at most once per 24 hours."""
        import time as _time

        last_key = f"_last_retention_run_{principal.tenant_id}"
        now = _time.time()
        last = getattr(self, last_key, 0.0)
        if now - last < 86400:  # 24 hours
            return
        try:
            async with self.session.begin():
                await set_tenant_context(self.session, str(principal.tenant_id))
                await run_retention_cleanup(self.session, principal.tenant_id)
        except Exception:
            pass
        setattr(self, last_key, now)

    async def _load_base(
        self, principal: PrincipalContext, request: ResponseRequest, lock: bool = False
    ):
        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == principal.tenant_id,
                Conversation.id == request.conversation_id,
                Conversation.user_id == principal.user_id,
                Conversation.closed_at.is_(None),
            )
            .with_for_update()
            if lock
            else select(Conversation).where(
                Conversation.tenant_id == principal.tenant_id,
                Conversation.id == request.conversation_id,
                Conversation.user_id == principal.user_id,
                Conversation.closed_at.is_(None),
            )
        )
        if conversation is None:
            return None, None, None, None
        message = await self.session.scalar(
                select(Message)
                .where(
                    Message.tenant_id == principal.tenant_id,
                    Message.id == request.user_message_id,
                    Message.conversation_id == conversation.id,
                    Message.user_id == principal.user_id,
                    Message.character_id == conversation.character_id,
                    Message.scope == conversation.scope,
                    Message.scenario_id == conversation.scenario_id,
                    Message.role == "user",
                    Message.deleted_at.is_(None),
                )
                # Retention is an optional relation. PostgreSQL cannot lock the
                .with_for_update()
                if lock
                else select(Message).where(
                    Message.tenant_id == principal.tenant_id,
                    Message.id == request.user_message_id,
                    Message.conversation_id == conversation.id,
                    Message.user_id == principal.user_id,
                    Message.character_id == conversation.character_id,
                    Message.scope == conversation.scope,
                    Message.scenario_id == conversation.scenario_id,
                    Message.role == "user",
                    Message.deleted_at.is_(None),
                )
        )
        now = datetime.now(UTC)
        profile = await self.session.scalar(
            select(CharacterProfileVersion)
            .where(
                CharacterProfileVersion.tenant_id == principal.tenant_id,
                CharacterProfileVersion.character_id == conversation.character_id,
                CharacterProfileVersion.admin_draft.is_(False),
                CharacterProfileVersion.effective_at <= now,
                or_(
                    CharacterProfileVersion.retired_at.is_(None),
                    CharacterProfileVersion.retired_at > now,
                ),
            )
            .order_by(CharacterProfileVersion.version.desc())
            .limit(1)
        )
        route = (
            None
            if profile is None
            else await self.session.scalar(
                select(ResponseRoutePolicy)
                .where(
                    ResponseRoutePolicy.tenant_id == principal.tenant_id,
                    ResponseRoutePolicy.character_id == conversation.character_id,
                    ResponseRoutePolicy.profile_version == profile.version,
                    ResponseRoutePolicy.provider == self.provider.provider,
                    ResponseRoutePolicy.model == self.provider.model,
                    ResponseRoutePolicy.enabled.is_(True),
                )
                .limit(1)
            )
        )
        return conversation, message, profile, route

    async def _active_consent(self, principal: PrincipalContext, purpose: str) -> bool:
        return (
            await self.session.scalar(
                select(Consent.id)
                .where(
                    Consent.tenant_id == principal.tenant_id,
                    Consent.user_id == principal.user_id,
                    Consent.purpose == purpose,
                    Consent.revoked_at.is_(None),
                )
                .with_for_update()
                .limit(1)
            )
            is not None
        )

    async def _recent_messages(
        self,
        principal: PrincipalContext,
        conversation: Conversation,
        *,
        current_message_id: UUID,
    ) -> tuple[ConversationEntry, ...]:
        """Return decrypted recent messages for conversation history."""
        rows = (
            await self.session.scalars(
                select(Message)
                .where(
                    Message.tenant_id == principal.tenant_id,
                    Message.conversation_id == conversation.id,
                    Message.user_id == principal.user_id,
                    Message.character_id == conversation.character_id,
                    Message.scope == conversation.scope,
                    Message.id != current_message_id,
                    or_(Message.role != "assistant", Message.channel_delivery_status == "confirmed"),
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.occurred_at.desc())
                .limit(30)
            )
        ).all()
        entries: list[ConversationEntry] = []
        for msg in reversed(rows):
            try:
                if msg.content_nonce is None or msg.content_key_version is None:
                    continue
                aad = f"{msg.tenant_id}:{msg.user_id}:{msg.conversation_id}:{msg.id}".encode()
                content = self.crypto.decrypt(
                    CiphertextEnvelope(
                        msg.content_ciphertext,
                        msg.content_nonce,
                        msg.content_key_version,
                    ),
                    aad,
                )
                entries.append(ConversationEntry(role=msg.role, content=content))
            except Exception:
                continue
        return tuple(entries)

    async def _disputed_memory_values(
        self, principal: PrincipalContext, conversation: Conversation
    ) -> tuple[str, ...]:
        """Return a small in-process set that may not be stated as settled fact."""
        rows = list(
            (
                await self.session.scalars(
                    select(Memory)
                    .where(
                        Memory.tenant_id == principal.tenant_id,
                        Memory.relationship_id == conversation.relationship_id,
                        Memory.user_id == principal.user_id,
                        Memory.character_id == conversation.character_id,
                        Memory.scope == conversation.scope,
                        Memory.scenario_id == conversation.scenario_id,
                        Memory.evidence_state == EvidenceState.disputed,
                        Memory.deleted_at.is_(None),
                    )
                    .order_by(Memory.updated_at.desc(), Memory.id)
                    .limit(8)
                )
            ).all()
        )
        values = []
        for memory in rows:
            value = decrypt_text_from_storage(
                memory.value_text, memory_value_aad(memory), crypto=self.crypto
            )
            if isinstance(value, str) and 1 <= len(value) <= 500:
                values.append(value)
        return tuple(values)

    async def _recent_summaries(
        self,
        principal: PrincipalContext,
        conversation: Conversation,
        query: str,
        as_of: datetime,
    ) -> tuple[str, ...]:
        """Rank bounded medium-term summaries by relevance and freshness."""
        rows = list(
            (
                await self.session.scalars(
                    select(ConversationSegment)
                    .where(
                        ConversationSegment.tenant_id == principal.tenant_id,
                        ConversationSegment.conversation_id == conversation.id,
                        ConversationSegment.summary.is_not(None),
                    )
                    .order_by(ConversationSegment.ended_at.desc(), ConversationSegment.id.desc())
                    .limit(24)
                )
            ).all()
        )
        eligible: list[tuple[ConversationSegment, str]] = []
        for row in rows:
            summary = decrypt_text_from_storage(
                row.summary, segment_summary_aad(row, conversation), crypto=self.crypto
            )
            if isinstance(summary, str) and 1 <= len(summary) <= 1200:
                eligible.append((row, summary))
        eligible.sort(
            key=lambda row: (
                summary_relevance(row[1], query, row[0].ended_at, now=as_of),
                row[0].ended_at,
                row[0].id,
            ),
            reverse=True,
        )
        return tuple(summary[:1200] for _, summary in eligible[:3])

    async def _communication_settings(
        self, principal: PrincipalContext, conversation: Conversation
    ) -> CommunicationSettings:
        row = await self.session.scalar(
            select(UserCharacterSettings)
            .where(
                UserCharacterSettings.tenant_id == principal.tenant_id,
                UserCharacterSettings.user_id == principal.user_id,
                UserCharacterSettings.character_id == conversation.character_id,
            )
            .limit(1)
        )
        raw = row.preferences if row is not None and isinstance(row.preferences, dict) else {}

        def value(key: str) -> str | None:
            item = raw.get(key)
            if not isinstance(item, str) or not 1 <= len(item) <= 128:
                return None
            if any(marker in item for marker in ("\x00", "\n", "\r", "<|")):
                return None
            return item

        return CommunicationSettings(
            language=value("language"),
            response_length=value("response_length"),
            tone=value("tone"),
            use_name=value("use_name"),
            initiative=value("initiative"),
            reminders=value("reminders"),
            roleplay=value("roleplay"),
            media=value("media"),
        )

    async def _verify_supplied(
        self,
        principal: PrincipalContext,
        conversation: Conversation,
        supplied: tuple[RetrievedMemory, ...],
    ) -> tuple[RetrievedMemory, ...]:
        verified: list[RetrievedMemory] = []
        for item in supplied:
            if not isinstance(item, RetrievedMemory):
                raise DomainError("citation_invalid")
            eligible = await self.session.scalar(
                select(Memory)
                .join(
                    MemoryControlState,
                    and_(
                        MemoryControlState.tenant_id == Memory.tenant_id,
                        MemoryControlState.user_id == Memory.user_id,
                        MemoryControlState.character_id == Memory.character_id,
                        MemoryControlState.scope == Memory.scope,
                        MemoryControlState.scenario_key == Memory.scenario_key,
                        MemoryControlState.deletion_epoch == Memory.created_epoch,
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
                    Memory.relationship_id == conversation.relationship_id,
                    Memory.id == item.memory_id,
                    Memory.version == item.version,
                    Memory.user_id == principal.user_id,
                    Memory.character_id == conversation.character_id,
                    Memory.scope == conversation.scope,
                    Memory.scenario_id == conversation.scenario_id,
                    Memory.lifecycle_state == LifecycleState.active,
                    Memory.deleted_at.is_(None),
                    or_(
                        RetentionPolicy.memory_days.is_(None),
                        Memory.created_at
                        + __import__("sqlalchemy").func.make_interval(
                            0, 0, 0, RetentionPolicy.memory_days
                        )
                        > datetime.now(UTC),
                    ),
                )
                # The retention join is optional. Lock only the authoritative
                # memory row; PostgreSQL rejects FOR UPDATE of an outer-joined
                # nullable relation.
                .with_for_update(of=Memory)
            )
            if eligible is None:
                raise DomainError("citation_invalid")
            verified.append(item)
        return tuple(verified)

    def _fingerprint(self, principal: PrincipalContext, request: ResponseRequest) -> str:
        return new(
            self._key,
            canonical_tagged_fields(
                (
                    (b"version", RESPONSE_DOMAIN),
                    (b"tenant", principal.tenant_id.bytes),
                    (b"user", principal.user_id.bytes),
                    (b"conversation", request.conversation_id.bytes),
                    (b"message", request.user_message_id.bytes),
                    (b"key", request.idempotency_key.encode()),
                    (
                        b"as_of",
                        request.as_of.astimezone(UTC)
                        .isoformat(timespec="microseconds")
                        .encode(),
                    ),
                )
            ),
            sha256,
        ).hexdigest()

    def _decrypt_message(self, principal: PrincipalContext, message: Message) -> str | None:
        try:
            return self.crypto.decrypt(
                CiphertextEnvelope(
                    message.content_ciphertext,
                    message.content_nonce or b"",
                    message.content_key_version or "",
                ),
                MessageIngestionService._aad_for(
                    principal.tenant_id, principal.user_id, message.conversation_id, message.id
                ),
            )
        except Exception:
            return None

    @staticmethod
    def _valid_request(request: object) -> bool:
        return (
            isinstance(request, ResponseRequest)
            and isinstance(request.conversation_id, UUID)
            and isinstance(request.user_message_id, UUID)
            and isinstance(request.idempotency_key, str)
            and 1 <= len(request.idempotency_key) <= 128
            and "\x00" not in request.idempotency_key
            and isinstance(request.as_of, datetime)
            and request.as_of.tzinfo is not None
            and request.as_of.utcoffset() is not None
        )

    @staticmethod
    def _valid_envelope(value: object) -> bool:
        return (
            isinstance(value, ProviderEnvelope)
            and isinstance(value.text, str)
            and 1 <= len(value.text) <= 4096
            and "\x00" not in value.text
            and isinstance(value.cited_memory_ids, tuple)
            and all(isinstance(item, UUID) for item in value.cited_memory_ids)
            and isinstance(value.provider_request_id, str)
            and len(value.provider_request_id) <= 128
        )


def _language_code(value: object) -> str:
    """Finite, deterministic script heuristic; provider output cannot choose this code."""
    if not isinstance(value, str) or len(value) > 16_384:
        return "unknown"
    cyrillic = sum("\u0400" <= char <= "\u052f" for char in value)
    latin = sum(("a" <= char.casefold() <= "z") for char in value)
    if cyrillic and latin:
        return "mixed"
    if cyrillic:
        return "ru"
    if latin:
        return "en"
    return "unknown"


def _bounded_profile_data(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            continue
        if isinstance(item, str) and 1 <= len(item) <= 512 and "\x00" not in item:
            result[key] = item.replace("<|", "＜|")
        elif isinstance(item, dict):
            nested = _bounded_profile_data(item)
            if nested:
                result[key] = nested
        if len(result) >= 16:
            break
    return result


async def run_retention_cleanup(session: AsyncSession, tenant_id: UUID) -> int:
    """Purge expired narrative bytes and invalidate work that could resurrect them."""
    from datetime import datetime, timedelta

    from app.db.models import RetentionPolicy

    now = datetime.now(UTC)
    policies = list(
        (
            await session.scalars(
                select(RetentionPolicy).where(RetentionPolicy.tenant_id == tenant_id)
            )
        ).all()
    )
    message_cutoffs = {
        policy.id: now - timedelta(days=policy.message_days)
        for policy in policies
        if not policy.legal_hold and policy.message_days is not None
    }
    memory_cutoffs = {
        policy.id: now - timedelta(days=policy.memory_days)
        for policy in policies
        if not policy.legal_hold and policy.memory_days is not None
    }
    message_ids: list[UUID] = []
    conversation_ids: set[UUID] = set()
    for policy_id, cutoff in message_cutoffs.items():
        rows = (
            await session.execute(
                text(
                    "SELECT id,conversation_id FROM agata.messages "
                    "WHERE tenant_id=:tid AND retention_policy_id=:policy AND deleted_at IS NULL "
                    "AND occurred_at < :cutoff"
                ),
                {"tid": tenant_id, "policy": policy_id, "cutoff": cutoff},
            )
        ).all()
        message_ids.extend(row.id for row in rows)
        conversation_ids.update(row.conversation_id for row in rows)

    memory_expiry = " OR ".join(
        f"(m.retention_policy_id=:memory_policy_{index} AND m.observed_at<:memory_cutoff_{index})"
        for index in range(len(memory_cutoffs))
    ) or "FALSE"
    memory_params = {"tid": tenant_id, "now": now, "message_ids": message_ids}
    for index, (policy_id, cutoff) in enumerate(memory_cutoffs.items()):
        memory_params[f"memory_policy_{index}"] = policy_id
        memory_params[f"memory_cutoff_{index}"] = cutoff
    candidate_expiry = " OR ".join(
        f"(retention_policy_id=:candidate_policy_{index} AND created_at<:candidate_cutoff_{index})"
        for index in range(len(memory_cutoffs))
    ) or "FALSE"
    candidate_params = {"tid": tenant_id, "message_ids": message_ids}
    for index, (policy_id, cutoff) in enumerate(memory_cutoffs.items()):
        candidate_params[f"candidate_policy_{index}"] = policy_id
        candidate_params[f"candidate_cutoff_{index}"] = cutoff

    control_relationship_ids: set[UUID] = set()
    if conversation_ids:
        control_relationship_ids.update(
            (await session.scalars(select(Conversation.relationship_id).where(
                Conversation.tenant_id == tenant_id,
                Conversation.id.in_(conversation_ids),
            ))).all()
        )
    memory_relationship_rows = await session.execute(
        text(
            "SELECT DISTINCT m.relationship_id FROM agata.memories m "
            "WHERE m.tenant_id=:tid AND m.deleted_at IS NULL AND ("
            "EXISTS (SELECT 1 FROM agata.memory_sources s WHERE s.tenant_id=m.tenant_id "
            "AND s.memory_id=m.id AND s.message_id=ANY(CAST(:message_ids AS uuid[]))) OR "
            f"({memory_expiry})"
            ")"
        ),
        memory_params,
    )
    control_relationship_ids.update(row.relationship_id for row in memory_relationship_rows)
    candidate_relationship_rows = await session.execute(
        text(
            "SELECT DISTINCT relationship_id FROM agata.shadow_memory_candidates "
            "WHERE tenant_id=:tid AND status IN ('shadow','promoting') AND ("
            "source_message_id=ANY(CAST(:message_ids AS uuid[])) OR "
            f"({candidate_expiry})"
            ")"
        ),
        candidate_params,
    )
    control_relationship_ids.update(row.relationship_id for row in candidate_relationship_rows)
    if control_relationship_ids:
        # Promotion, memory completion and inference authority lock control
        # before source messages, candidates or parent outbox rows. Retention
        # takes the same locks before mutating any of those records.
        locked_controls = await session.scalars(
            select(MemoryControlState.id)
            .where(
                MemoryControlState.tenant_id == tenant_id,
                MemoryControlState.relationship_id.in_(control_relationship_ids),
            )
            .order_by(MemoryControlState.id)
            .with_for_update()
        )
        locked_controls.all()

    for policy_id, cutoff in message_cutoffs.items():
        rows = (
            await session.execute(
                text(
                    "UPDATE agata.messages SET deleted_at=:now, content_ciphertext=NULL, "
                    "content_nonce=NULL, content_key_version=NULL "
                    "WHERE tenant_id=:tid AND retention_policy_id=:policy AND deleted_at IS NULL "
                    "AND occurred_at < :cutoff RETURNING id,conversation_id"
                ),
                {"tid": tenant_id, "policy": policy_id, "cutoff": cutoff, "now": now},
            )
        ).all()
        message_ids.extend(row.id for row in rows if row.id not in message_ids)
        conversation_ids.update(row.conversation_id for row in rows)
    memory_params["message_ids"] = message_ids
    candidate_params["message_ids"] = message_ids

    purged = await session.execute(
        text(
            "UPDATE agata.memories m SET lifecycle_state='deleted', deleted_at=:now, valid_until=:now, "
            "value_text=NULL, value_json=NULL, search_text=NULL, embedding=NULL, "
            "embedding_model=NULL, embedded_at=NULL "
            "WHERE m.tenant_id=:tid AND m.deleted_at IS NULL AND ("
            "EXISTS (SELECT 1 FROM agata.memory_sources s WHERE s.tenant_id=m.tenant_id "
            "AND s.memory_id=m.id AND s.message_id=ANY(CAST(:message_ids AS uuid[]))) OR "
            f"({memory_expiry})"
            ") RETURNING m.id,m.relationship_id,m.user_id,m.character_id,m.scope,m.scenario_id"
        ),
        memory_params,
    )
    purged_rows = purged.all()
    for relationship_id in sorted({row.relationship_id for row in purged_rows}):
        await session.execute(
            text(
                "UPDATE agata.memory_control_state SET deletion_epoch=deletion_epoch+1 "
                "WHERE tenant_id=:tid AND relationship_id=:relationship_id"
            ),
            {"tid": tenant_id, "relationship_id": relationship_id},
        )
        # The deletion epoch invalidates every graph projection for the relationship;
        # remove derived rows rather than retaining unreachable user topology.
        await session.execute(text(
            "DELETE FROM agata.memory_graph_evidence WHERE tenant_id=:tid AND relationship_id=:relationship_id"
        ), {"tid": tenant_id, "relationship_id": relationship_id})
        await session.execute(text(
            "DELETE FROM agata.memory_relations WHERE tenant_id=:tid AND relationship_id=:relationship_id"
        ), {"tid": tenant_id, "relationship_id": relationship_id})
        await session.execute(text(
            "DELETE FROM agata.memory_entities WHERE tenant_id=:tid AND relationship_id=:relationship_id"
        ), {"tid": tenant_id, "relationship_id": relationship_id})
        await session.execute(
            text(
                "UPDATE agata.memory_snapshots SET content='{}'::jsonb, "
                "source_memory_ids='[]'::jsonb, source_segment_ids='[]'::jsonb, "
                "source_message_ids='[]'::jsonb, invalidated_at=COALESCE(invalidated_at,:now), "
                "generation=generation+1 "
                "WHERE tenant_id=:tid AND relationship_id=:relationship_id"
            ),
            {"tid": tenant_id, "relationship_id": relationship_id, "now": now},
        )
    # Promotion completion also holds MemoryControlState before it writes a
    # shadow candidate and completes its parent job, so invalidate candidates
    # only after the relationship control rows above are locked.
    invalidated_candidates = (
        await session.execute(
            text(
                "UPDATE agata.shadow_memory_candidates SET status='invalidated', invalidated_at=now(), "
                "value_ciphertext=NULL,value_nonce=NULL,value_key_version=NULL "
                "WHERE tenant_id=:tid AND status IN ('shadow','promoting') AND ("
                "source_message_id=ANY(CAST(:message_ids AS uuid[])) OR "
                f"({candidate_expiry})"
                ") RETURNING id"
            ),
            candidate_params,
        )
    ).all()
    if invalidated_candidates:
        await session.execute(
            text(
                "UPDATE agata.outbox_jobs SET state='dead', claimed_at=NULL, lease_expires_at=NULL, "
                "last_error='retention_purged' WHERE tenant_id=:tid "
                "AND state IN ('pending','running','failed') "
                "AND payload->>'candidate_id'=ANY(CAST(:ids AS text[]))"
            ),
            {
                "tid": tenant_id,
                "ids": [str(row.id) for row in invalidated_candidates],
            },
        )
    # Memory authority and deferral paths lock MemoryControlState before their
    # parent outbox row. Lock/update controls before invalidating jobs here too,
    # or a retention transaction can hold an outbox row while waiting for the
    # control row that the worker already holds.
    if message_ids:
        await session.execute(
            text(
                "UPDATE agata.outbox_jobs SET state='dead', claimed_at=NULL, lease_expires_at=NULL, "
                "last_error='retention_purged', payload=payload - 'reply_context_ciphertext' "
                "- 'reply_context_nonce' - 'reply_context_key_version' "
                "WHERE tenant_id=:tid AND state IN ('pending','running','failed') "
                "AND (payload->>'message_id' = ANY(CAST(:ids AS text[])) "
                "OR payload->>'conversation_id' = ANY(CAST(:conversations AS text[])))"
            ),
            {
                "tid": tenant_id,
                "ids": [str(value) for value in message_ids],
                "conversations": [str(value) for value in conversation_ids],
            },
        )
    if conversation_ids:
        await session.execute(
            text(
                "UPDATE agata.conversation_segments SET summary=NULL,keyphrases=NULL "
                "WHERE tenant_id=:tid AND conversation_id = ANY(:ids)"
            ),
            {"tid": tenant_id, "ids": list(conversation_ids)},
        )
    return len(message_ids) + len(purged_rows)
