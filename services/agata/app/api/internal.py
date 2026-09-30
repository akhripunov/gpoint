from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.policy import PolicyService
from app.compliance.receipt import current_disclosure
from app.config import get_settings
from app.db.base import get_session, set_tenant_context
from app.db.models import (
    AgeAssurance,
    AIDisclosureReceipt,
    Character,
    CharacterProfileVersion,
    Consent,
    Conversation,
    JobState,
    MemorySnapshot,
    Message,
    OutboxJob,
    RegionalPolicyProfile,
    Relationship,
    ResponseRoutePolicy,
    Scope,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.domain.relationships import (
    RelationshipUnavailable,
    record_accepted_interaction,
    resolve_relationship,
)
from app.jobs.outbox import OutboxRepository
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.retrieval import EmbeddingProvider, HybridRetrievalService
from app.messages.crypto import AeadCrypto, RequestFingerprinter
from app.messages.service import IngestMessage, MessageIngestionService
from app.responses.mac_worker import decrypt_claimed_request, encrypt_worker_result
from app.responses.pipeline import ResponseOrchestrator, ResponseProvider, ResponseRequest
from app.responses.provider_factory import build_response_provider

router = APIRouter(prefix="/internal/v1", tags=["internal"])


def require_internal_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {get_settings().internal_api_token.get_secret_value()}"
    if not isinstance(authorization, str) or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "unauthorized"}
        )


def _encode_message_cursor(message: Message) -> str:
    value = json.dumps([message.occurred_at.isoformat(), str(message.id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_message_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        occurred_at, message_id = json.loads(raw)
        timestamp = datetime.fromisoformat(occurred_at)
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, UUID(message_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"}) from exc


def _decrypt_message(message: Message, tenant_id: UUID, *, user_id: UUID) -> str:
    if not message.content_ciphertext or not message.content_nonce or not message.content_key_version:
        raise HTTPException(status_code=503, detail={"code": "message_content_unavailable"})
    try:
        crypto = AeadCrypto(
            encoded_key=get_settings().message_aead_key.get_secret_value(), key_version="v1"
        )
        from app.messages.crypto import CiphertextEnvelope

        return crypto.decrypt(
            CiphertextEnvelope(message.content_ciphertext, message.content_nonce, message.content_key_version),
            MessageIngestionService._aad_for(tenant_id, user_id, message.conversation_id, message.id),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "message_content_unavailable"}) from exc


@router.get("/users/{gpoint_user_id}/chat-state", dependencies=[Depends(require_internal_auth)])
async def chat_state(gpoint_user_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_user_id) <= 255 or "\x00" in gpoint_user_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_user_id"})
    now = datetime.now(UTC)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        user = await session.scalar(select(User).where(
            User.tenant_id == tenant_id, User.source_system == "gpoint",
            User.external_id == gpoint_user_id, User.deleted_at.is_(None),
        ))
        policy_query = select(RegionalPolicyProfile).where(
            RegionalPolicyProfile.tenant_id == tenant_id,
            RegionalPolicyProfile.status == "active",
            RegionalPolicyProfile.effective_from <= now,
            RegionalPolicyProfile.effective_to.is_(None) | (RegionalPolicyProfile.effective_to > now),
        ).order_by(RegionalPolicyProfile.effective_from.desc(), RegionalPolicyProfile.id.desc()).limit(1)
        policy = await session.scalar(policy_query)
        if policy is None:
            raise HTTPException(status_code=503, detail={"code": "policy_unavailable"})
        if user is None:
            return {
                "chatHistory": False, "longTermMemory": False,
                "disclosureRequired": bool(policy.rules.get("ai_disclosure_required", False)),
                "ageAssuranceRequired": True,
                "ageBand": "unknown", "ageStatus": "pending", "ageMethod": "unknown",
                "verifiedAt": None, "expiresAt": None, "jurisdiction": policy.jurisdiction,
            }
        consent_rows = (await session.scalars(select(Consent).where(
            Consent.tenant_id == tenant_id, Consent.user_id == user.id,
            Consent.revoked_at.is_(None), Consent.purpose.in_(("chat_history", "long_term_memory")),
        ))).all()
        age = await session.scalar(select(AgeAssurance).where(
            AgeAssurance.tenant_id == tenant_id, AgeAssurance.user_id == user.id,
        ).order_by(AgeAssurance.version.desc()).limit(1))
        from app.compliance.policy import PolicyService

        policy_service = PolicyService()
        age_assurance_required = not await policy_service.age_assurance_eligible(
            session, tenant_id=tenant_id, user_id=user.id, now=now
        )
        disclosure = await current_disclosure(session, tenant_id=tenant_id, user_id=user.id, now=now)
        disclosure_decision = None
        if disclosure is not None:
            disclosure_decision = policy_service.disclosure_required(
                rules=disclosure.policy.rules, policy_version=disclosure.policy.version,
                last_displayed_at=disclosure.receipt.displayed_at if disclosure.receipt else None,
                now=now,
            )
        active = {row.purpose for row in consent_rows}
        return {
            "chatHistory": "chat_history" in active,
            "longTermMemory": "long_term_memory" in active,
            "disclosureRequired": bool(disclosure_decision and disclosure_decision.allowed),
            "ageAssuranceRequired": age_assurance_required,
            "ageBand": age.age_band if age else "unknown",
            "ageStatus": age.status if age else "pending",
            "ageMethod": age.method if age else "unknown",
            "verifiedAt": age.verified_at.isoformat() if age and age.verified_at else None,
            "expiresAt": age.expires_at.isoformat() if age and age.expires_at else None,
            "jurisdiction": age.jurisdiction if age else policy.jurisdiction,
        }


@router.get("/users/{gpoint_user_id}/age-assurance", dependencies=[Depends(require_internal_auth)])
async def age_assurance_state(
    gpoint_user_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_user_id) <= 255 or "\x00" in gpoint_user_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_user_id"})
    now = datetime.now(UTC)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        user = await session.scalar(select(User).where(
            User.tenant_id == tenant_id, User.source_system == "gpoint",
            User.external_id == gpoint_user_id, User.deleted_at.is_(None),
        ))
        policy = await session.scalar(select(RegionalPolicyProfile).where(
            RegionalPolicyProfile.tenant_id == tenant_id,
            RegionalPolicyProfile.status == "active",
            RegionalPolicyProfile.effective_from <= now,
            RegionalPolicyProfile.effective_to.is_(None) | (RegionalPolicyProfile.effective_to > now),
        ).order_by(RegionalPolicyProfile.effective_from.desc(), RegionalPolicyProfile.id.desc()).limit(1))
        if policy is None:
            raise HTTPException(status_code=503, detail={"code": "policy_unavailable"})
        age = None if user is None else await session.scalar(select(AgeAssurance).where(
            AgeAssurance.tenant_id == tenant_id, AgeAssurance.user_id == user.id,
        ).order_by(AgeAssurance.version.desc()).limit(1))
        eligible = False
        if user is not None:
            from app.compliance.policy import PolicyService
            eligible = await PolicyService().age_assurance_eligible(
                session, tenant_id=tenant_id, user_id=user.id, now=now
            )
        return {
            "required": not eligible,
            "eligible": eligible,
            "status": age.status if age else "pending",
            "method": age.method if age else "unknown",
            "jurisdiction": age.jurisdiction if age else policy.jurisdiction,
            "assertedAt": age.asserted_at.isoformat() if age and age.asserted_at else None,
            "expiresAt": age.expires_at.isoformat() if age and age.expires_at else None,
            "selfAttestationAllowed": policy.rules.get("self_attestation_allowed") is True,
        }


@router.post(
    "/users/{gpoint_user_id}/age-assurance/self-attest",
    dependencies=[Depends(require_internal_auth)],
)
async def self_attest_age(
    gpoint_user_id: str,
    payload: SelfAttestationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_user_id) <= 255 or "\x00" in gpoint_user_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_user_id"})
    now = datetime.now(UTC)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        policy = await session.scalar(select(RegionalPolicyProfile).where(
            RegionalPolicyProfile.tenant_id == tenant_id,
            RegionalPolicyProfile.status == "active",
            RegionalPolicyProfile.effective_from <= now,
            RegionalPolicyProfile.effective_to.is_(None) | (RegionalPolicyProfile.effective_to > now),
        ).order_by(RegionalPolicyProfile.effective_from.desc(), RegionalPolicyProfile.id.desc()).limit(1).with_for_update())
        if policy is None:
            raise HTTPException(status_code=503, detail={"code": "policy_unavailable"})
        if policy.rules.get("self_attestation_allowed") is not True:
            raise HTTPException(status_code=403, detail={"code": "self_attestation_not_allowed"})
        user = await session.scalar(select(User).where(
            User.tenant_id == tenant_id, User.source_system == "gpoint",
            User.external_id == gpoint_user_id, User.deleted_at.is_(None),
        ).with_for_update())
        if user is None:
            user = User(tenant_id=tenant_id, source_system="gpoint", external_id=gpoint_user_id)
            session.add(user)
            await session.flush()
        prior = await session.scalar(select(AgeAssurance).where(
            AgeAssurance.tenant_id == tenant_id, AgeAssurance.user_id == user.id,
        ).order_by(AgeAssurance.version.desc()).limit(1).with_for_update())
        if prior is not None and (prior.status == "rejected" or prior.age_band == "minor"):
            raise HTTPException(status_code=403, detail={"code": "age_assurance_rejected"})
        if prior is not None and await PolicyService().age_assurance_eligible(
            session, tenant_id=tenant_id, user_id=user.id, now=now
        ):
            return {"eligible": True, "status": prior.status, "method": prior.method}
        session.add(AgeAssurance(
            tenant_id=tenant_id,
            user_id=user.id,
            age_band="adult",
            status="self_attested",
            method="self_attested",
            jurisdiction=policy.jurisdiction,
            verified_at=None,
            asserted_at=now,
            expires_at=None,
            version=(prior.version + 1) if prior else 1,
        ))
        await session.flush()
        return {"eligible": True, "status": "self_attested", "method": "self_attested"}


@router.get("/users/{gpoint_user_id}/conversations", dependencies=[Depends(require_internal_auth)])
async def list_gpoint_conversations(
    gpoint_user_id: str,
    limit: int = Query(default=30, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    try:
        external_user_id = gpoint_user_id
        if not 1 <= len(external_user_id) <= 255 or "\x00" in external_user_id:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_user_id"}) from exc
    decoded = _decode_message_cursor(cursor)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        user = await session.scalar(select(User).where(
            User.tenant_id == tenant_id, User.source_system == "gpoint",
            User.external_id == external_user_id, User.deleted_at.is_(None),
        ))
        if user is None:
            return {"items": [], "nextCursor": None}
        has_history_consent = await session.scalar(select(Consent.id).where(
            Consent.tenant_id == tenant_id, Consent.user_id == user.id,
            Consent.purpose == "chat_history", Consent.revoked_at.is_(None),
        ).limit(1))
        if has_history_consent is None:
            raise HTTPException(status_code=403, detail={"code": "chat_history_consent_required"})
        latest = select(
            Message.conversation_id.label("conversation_id"),
            Message.id.label("message_id"),
            Message.occurred_at.label("occurred_at"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(Message.occurred_at.desc(), Message.id.desc()),
            ).label("row_num"),
        ).where(
            Message.tenant_id == tenant_id, Message.user_id == user.id,
            Message.scope == Scope.real_world, Message.scenario_id.is_(None),
            Message.role.in_(("user", "assistant")), Message.deleted_at.is_(None),
        ).subquery()
        latest_query = select(latest).where(latest.c.row_num == 1)
        if decoded:
            latest_query = latest_query.where(
                tuple_(latest.c.occurred_at, latest.c.message_id) < tuple_(decoded[0], decoded[1])
            )
        latest_page = latest_query.subquery()
        rows = (await session.execute(
            select(Conversation, Message, Character.external_id)
            .join(latest_page, latest_page.c.conversation_id == Conversation.id)
            .join(Message, Message.id == latest_page.c.message_id)
            .join(Character, and_(
                Character.tenant_id == Conversation.tenant_id,
                Character.id == Conversation.character_id,
                Character.source_system == "gpoint",
            ))
            .where(
                Conversation.tenant_id == tenant_id, Conversation.user_id == user.id,
                Conversation.source_system == "gpoint", Conversation.scope == Scope.real_world,
                Conversation.scenario_id.is_(None),
                Conversation.external_id.startswith(f"gpoint-chat:{gpoint_user_id}:", autoescape=True),
            )
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .limit(limit + 1)
        )).all()
        page = rows[:limit]
        items = []
        for conversation, message, gpoint_character_id in page:
            items.append({
                "gpointCharacterId": gpoint_character_id,
                "externalConversationId": conversation.external_id,
                "lastMessagePreview": _decrypt_message(message, tenant_id, user_id=user.id)[:120],
                "lastMessageRole": message.role,
                "lastMessageAt": message.occurred_at.isoformat(),
            })
        next_cursor = None
        if len(rows) > limit and page:
            next_cursor = _encode_message_cursor(page[-1][1])
        return {"items": items, "nextCursor": next_cursor}


@router.get("/users/{gpoint_user_id}/characters/{gpoint_character_id}/messages", dependencies=[Depends(require_internal_auth)])
async def list_gpoint_messages(
    gpoint_user_id: str,
    gpoint_character_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_user_id) <= 255 or not 1 <= len(gpoint_character_id) <= 255:
        raise HTTPException(status_code=422, detail={"code": "invalid_identity"})
    decoded = _decode_message_cursor(cursor)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        user = await session.scalar(select(User).where(
            User.tenant_id == tenant_id, User.source_system == "gpoint",
            User.external_id == gpoint_user_id, User.deleted_at.is_(None),
        ))
        character = await session.scalar(select(Character).where(
            Character.tenant_id == tenant_id, Character.source_system == "gpoint",
            Character.external_id == gpoint_character_id, Character.status == "active",
        ))
        if user is None or character is None:
            return {"items": [], "nextCursor": None}
        has_history_consent = await session.scalar(select(Consent.id).where(
            Consent.tenant_id == tenant_id, Consent.user_id == user.id,
            Consent.purpose == "chat_history", Consent.revoked_at.is_(None),
        ).limit(1))
        if has_history_consent is None:
            raise HTTPException(status_code=403, detail={"code": "chat_history_consent_required"})
        relationship = await session.scalar(select(Relationship.id).where(
            Relationship.tenant_id == tenant_id, Relationship.user_id == user.id,
            Relationship.character_id == character.id, Relationship.scope == Scope.real_world,
            Relationship.scenario_id.is_(None),
        ).limit(1))
        if relationship is None:
            return {"items": [], "nextCursor": None}
        query = select(Message).join(Conversation, and_(
            Conversation.tenant_id == Message.tenant_id,
            Conversation.id == Message.conversation_id,
            Conversation.relationship_id == relationship,
        )).where(
            Message.tenant_id == tenant_id, Message.user_id == user.id,
            Message.character_id == character.id, Message.scope == Scope.real_world,
            Message.scenario_id.is_(None), Message.role.in_(("user", "assistant")),
            Message.deleted_at.is_(None), Conversation.source_system == "gpoint",
            Conversation.external_id == f"gpoint-chat:{gpoint_user_id}:{gpoint_character_id}",
        )
        if decoded:
            query = query.where(tuple_(Message.occurred_at, Message.id) < tuple_(decoded[0], decoded[1]))
        rows = list((await session.scalars(query.order_by(Message.occurred_at.desc(), Message.id.desc()).limit(limit + 1))).all())
        page = rows[:limit]
        items = [{
            "id": str(message.id), "role": message.role,
            **({"clientMessageId": message.idempotency_key} if message.role == "user" else {}),
            "text": _decrypt_message(message, tenant_id, user_id=user.id),
            "occurredAt": message.occurred_at.isoformat(),
        } for message in reversed(page)]
        next_cursor = _encode_message_cursor(page[-1]) if len(rows) > limit and page else None
        return {"items": items, "nextCursor": next_cursor}


class RespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    gpoint_user_id: str = Field(min_length=1, max_length=255)
    gpoint_character_id: str = Field(min_length=1, max_length=255)
    conversation_id: str | None = Field(default=None, max_length=255)
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=16_384)
    locale: str | None = Field(default=None, max_length=32)


class RespondResponse(BaseModel):
    status: Literal["completed", "pending"] = "completed"
    request_id: str
    conversation_id: str
    message_id: str | None = None
    character_id: str
    text: str | None = None


class CharacterSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    personality: dict = Field(default_factory=dict)
    speech_style: dict = Field(default_factory=dict)
    backstory: dict = Field(default_factory=dict)
    boundaries: dict = Field(default_factory=dict)


class UserConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_history: bool
    long_term_memory: bool = False
    age_band: Literal["unknown", "minor", "adult"]
    age_status: Literal["pending", "verified", "self_attested", "expired", "rejected"]
    age_method: str = Field(min_length=1, max_length=32)
    jurisdiction: str = Field(min_length=2, max_length=32)
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    disclosure_accepted: bool
    disclosure_receipt_id: str = Field(min_length=1, max_length=128)


class SelfAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_18_plus: Literal[True]


class WorkerCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    lease_token: str = Field(min_length=32, max_length=64)
    text: str = Field(min_length=1, max_length=16_000)
    model: str = Field(min_length=1, max_length=128)


class WorkerFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    lease_token: str = Field(min_length=32, max_length=64)
    error_code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


def _language_code(locale: str | None) -> str:
    """Map BCP-47 locale tags to the finite language codes stored by AGATA."""
    if not locale:
        return "unknown"
    language = locale.split("-", 1)[0].lower()
    return language if language in {"ru", "en"} else "mixed"


def get_response_provider(
    session: AsyncSession = Depends(get_session),
) -> ResponseProvider | None:
    return build_response_provider(session)


def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if not settings.embedding_api_key:
        if settings.llm_provider in {"disabled", "none", "mac_worker"}:
            return UnavailableEmbeddingProvider()
        raise HTTPException(status_code=503, detail={"code": "embedding_provider_unconfigured"})
    return HuggingFaceFreeEmbeddingProvider(
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
    )


class UnavailableEmbeddingProvider:
    """Fail retrieval closed while allowing the configured Mac inference provider."""

    model_name = "unavailable"

    async def embed(self, _text: str) -> object:
        raise RuntimeError("embedding_provider_unavailable")


@router.post("/worker/claim", dependencies=[Depends(require_internal_auth)])
async def claim_inference_job(
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        repository = OutboxRepository(session)
        await repository.expire_stale_inference_jobs(
            tenant_id=tenant_id,
            ttl_seconds=settings.inference_job_ttl_seconds,
        )
        await repository.requeue_expired(
            tenant_id=tenant_id, job_types=frozenset({"inference"})
        )
        jobs = await repository.claim(
            tenant_id=tenant_id,
            limit=1,
            lease_seconds=300,
            job_types=frozenset({"inference"}),
        )
        if not jobs:
            return {"job": None}
        job = jobs[0]
        request_text = decrypt_claimed_request(job)
        return {
            "job": {
                "id": str(job.id),
                "leaseToken": job.claim_token,
                "attempt": job.attempts,
                "request": json.loads(request_text),
            }
        }


@router.post("/worker/complete", dependencies=[Depends(require_internal_auth)])
async def complete_inference_job(
    payload: WorkerCompleteRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    now = datetime.now(UTC)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        job = await session.scalar(
            select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.id == payload.job_id,
                OutboxJob.job_type == "inference",
            ).with_for_update()
        )
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "inference_job_unavailable"})
        if job.state == JobState.completed:
            return {"completed": True, "duplicate": True}
        if (
            job.state != JobState.running
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
            or not isinstance(job.claim_token, str)
            or not hmac.compare_digest(job.claim_token, payload.lease_token)
        ):
            raise HTTPException(status_code=409, detail={"code": "inference_lease_stale"})
        job.payload = {
            **job.payload,
            **encrypt_worker_result(
                job,
                json.dumps(
                    {"text": payload.text, "model": payload.model},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        }
        job.state = JobState.completed
        job.claim_token = None
        job.lease_expires_at = None
        job.last_error = None
        source_message = await session.scalar(select(Message).where(
            Message.tenant_id == tenant_id,
            Message.id == job.aggregate_id,
            Message.role == "user",
            Message.deleted_at.is_(None),
        ).with_for_update())
        if source_message is None:
            raise HTTPException(status_code=409, detail={"code": "inference_source_unavailable"})
        await OutboxRepository(session).enqueue(
            tenant_id=tenant_id,
            job_type="response_retry",
            dedupe_key=f"response-retry:{source_message.id}",
            aggregate_id=source_message.id,
            payload={
                "request_id": source_message.idempotency_key,
                "message_id": str(source_message.id),
            },
        )
        await session.flush()
        return {"completed": True, "duplicate": False, "finalizationQueued": True}


@router.post("/worker/fail", dependencies=[Depends(require_internal_auth)])
async def fail_inference_job(
    payload: WorkerFailRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        job = await session.scalar(
            select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.id == payload.job_id,
                OutboxJob.job_type == "inference",
            ).with_for_update()
        )
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "inference_job_unavailable"})
        if (
            job.state != JobState.running
            or job.lease_expires_at is None
            or job.lease_expires_at <= datetime.now(UTC)
            or not isinstance(job.claim_token, str)
            or not hmac.compare_digest(job.claim_token, payload.lease_token)
        ):
            raise HTTPException(status_code=409, detail={"code": "inference_lease_stale"})
        await OutboxRepository(session).fail(job, payload.error_code, max_attempts=2)
        return {"accepted": True, "state": job.state.value}


@router.put("/characters/{gpoint_character_id}", dependencies=[Depends(require_internal_auth)])
async def sync_character(
    gpoint_character_id: str,
    payload: CharacterSyncRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str | int]:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_character_id) <= 255 or "\x00" in gpoint_character_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_character_id"})
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=503, detail={"code": "tenant_not_provisioned"})
        character = await session.scalar(
            select(Character)
            .where(
                Character.tenant_id == tenant_id,
                Character.source_system == "gpoint",
                Character.external_id == gpoint_character_id,
            )
            .with_for_update()
        )
        if character is None:
            character = Character(
                tenant_id=tenant_id,
                source_system="gpoint",
                external_id=gpoint_character_id,
                name=payload.name,
            )
            session.add(character)
            await session.flush()
        else:
            character.name = payload.name
        now = datetime.now(UTC)
        personality = dict(payload.personality)
        identity = personality.get("identity_core")
        identity = dict(identity) if isinstance(identity, dict) else {}
        identity["name"] = payload.name
        personality["identity_core"] = identity
        previous = await session.scalar(
            select(CharacterProfileVersion)
            .where(
                CharacterProfileVersion.tenant_id == tenant_id,
                CharacterProfileVersion.character_id == character.id,
                CharacterProfileVersion.admin_draft.is_(False),
                CharacterProfileVersion.retired_at.is_(None),
            )
            .order_by(CharacterProfileVersion.version.desc())
            .with_for_update()
        )
        if previous is not None and (
            previous.personality == personality
            and previous.speech_style == payload.speech_style
            and previous.backstory == payload.backstory
            and previous.boundaries == payload.boundaries
        ):
            route = await session.scalar(
                select(ResponseRoutePolicy).where(
                    ResponseRoutePolicy.tenant_id == tenant_id,
                    ResponseRoutePolicy.character_id == character.id,
                    ResponseRoutePolicy.profile_version == previous.version,
                    ResponseRoutePolicy.enabled.is_(True),
                )
            )
            if route is None:
                session.add(
                    ResponseRoutePolicy(
                        tenant_id=tenant_id,
                        character_id=character.id,
                        profile_version=previous.version,
                        provider=settings.llm_provider,
                        model=settings.llm_model,
                        prompt_version="agata-core-v1",
                        schema_version="context-v1",
                        policy_version="gpoint-internal-v1",
                        enabled=True,
                        streaming_allowed=False,
                    )
                )
            return {"character_id": str(character.id), "profile_version": previous.version}
        version = (previous.version + 1) if previous else 1
        if previous:
            previous.retired_at = now
        profile = CharacterProfileVersion(
            tenant_id=tenant_id,
            character_id=character.id,
            version=version,
            personality=personality,
            speech_style=dict(payload.speech_style),
            backstory=dict(payload.backstory),
            boundaries=dict(payload.boundaries),
            effective_at=now,
            admin_draft=False,
        )
        session.add(profile)
        session.add(
            ResponseRoutePolicy(
                tenant_id=tenant_id,
                character_id=character.id,
                profile_version=version,
                provider=settings.llm_provider,
                model=settings.llm_model,
                prompt_version="agata-core-v1",
                schema_version="context-v1",
                policy_version="gpoint-internal-v1",
                enabled=True,
                streaming_allowed=False,
            )
        )
        await session.flush()
        return {"character_id": str(character.id), "profile_version": version}


@router.put("/users/{gpoint_user_id}/consent", dependencies=[Depends(require_internal_auth)])
async def sync_user_consent(
    gpoint_user_id: str,
    payload: UserConsentRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str | int]:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    if not 1 <= len(gpoint_user_id) <= 255 or "\x00" in gpoint_user_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_user_id"})
    if payload.long_term_memory and not payload.chat_history:
        raise HTTPException(status_code=422, detail={"code": "memory_requires_chat_history"})
    if payload.age_status == "verified" and (
        payload.age_band != "adult" or payload.verified_at is None
    ):
        raise HTTPException(status_code=422, detail={"code": "verified_age_evidence_required"})
    if payload.verified_at and payload.verified_at.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "timezone_required"})
    now = datetime.now(UTC)
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        user = await session.scalar(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.source_system == "gpoint",
                User.external_id == gpoint_user_id,
            )
            .with_for_update()
        )
        if user is None:
            user = User(tenant_id=tenant_id, source_system="gpoint", external_id=gpoint_user_id)
            session.add(user)
            await session.flush()
        revoked_purposes: set[str] = set()
        for purpose, granted in (
            ("chat_history", payload.chat_history),
            ("long_term_memory", payload.long_term_memory),
        ):
            existing = await session.scalar(
                select(Consent)
                .where(
                    Consent.tenant_id == tenant_id,
                    Consent.user_id == user.id,
                    Consent.purpose == purpose,
                    Consent.revoked_at.is_(None),
                )
                .order_by(Consent.granted_at.desc())
                .with_for_update()
            )
            if granted and existing is None:
                session.add(
                    Consent(
                        tenant_id=tenant_id,
                        user_id=user.id,
                        purpose=purpose,
                        granted_at=now,
                        policy_version="gpoint-consent-v1",
                    )
                )
            elif not granted and existing is not None:
                existing.revoked_at = now
                revoked_purposes.add(purpose)
        purge_types: set[str] = set()
        if "long_term_memory" in revoked_purposes:
            purge_types.update({"relationship_note", "relationship_summary", "user_profile_summary", "goal_summary", "project_summary"})
            # Consent revocation removes all derived graph material for this user's
            # relationships in the same transaction; old queued jobs cannot recreate
            # it because projection also requires the exact still-active consent id.
            await session.execute(text(
                "UPDATE agata.outbox_jobs SET state='dead', claimed_at=NULL, lease_expires_at=NULL, "
                "last_error='long_term_consent_revoked' WHERE tenant_id=:tenant "
                "AND job_type='memory_graph_sync' AND state IN ('pending','running','failed') "
                "AND payload->>'memory_id' IN (SELECT id::text FROM agata.memories "
                "WHERE tenant_id=:tenant AND user_id=:user)"
            ), {"tenant": tenant_id, "user": user.id})
            await session.execute(text(
                "DELETE FROM agata.memory_graph_evidence WHERE tenant_id=:tenant "
                "AND relationship_id IN (SELECT id FROM agata.relationships WHERE tenant_id=:tenant AND user_id=:user)"
            ), {"tenant": tenant_id, "user": user.id})
            await session.execute(text(
                "DELETE FROM agata.memory_relations WHERE tenant_id=:tenant "
                "AND relationship_id IN (SELECT id FROM agata.relationships WHERE tenant_id=:tenant AND user_id=:user)"
            ), {"tenant": tenant_id, "user": user.id})
            await session.execute(text(
                "DELETE FROM agata.memory_entities WHERE tenant_id=:tenant "
                "AND relationship_id IN (SELECT id FROM agata.relationships WHERE tenant_id=:tenant AND user_id=:user)"
            ), {"tenant": tenant_id, "user": user.id})
        if "chat_history" in revoked_purposes:
            purge_types.update({"conversation_summary", "current_context_summary", "topic_summary", "day_summary"})
        if purge_types:
            await session.execute(
                update(MemorySnapshot)
                .where(
                    MemorySnapshot.tenant_id == tenant_id,
                    MemorySnapshot.user_id == user.id,
                    MemorySnapshot.snapshot_type.in_(purge_types),
                )
                .values(
                    content={}, source_memory_ids=[], source_segment_ids=[], source_message_ids=[],
                    invalidated_at=now, generation=MemorySnapshot.generation + 1,
                )
            )
        prior_age = await session.scalar(
            select(AgeAssurance)
            .where(AgeAssurance.tenant_id == tenant_id, AgeAssurance.user_id == user.id)
            .order_by(AgeAssurance.version.desc())
            .with_for_update()
        )
        preserve_verified_age = prior_age is not None and prior_age.status == "verified"
        same_age = prior_age is not None and (
            prior_age.age_band == payload.age_band
            and prior_age.status == payload.age_status
            and prior_age.method == payload.age_method
            and prior_age.jurisdiction == payload.jurisdiction
            and prior_age.verified_at == payload.verified_at
            and prior_age.expires_at == payload.expires_at
        )
        if preserve_verified_age:
            age_version = prior_age.version
        elif not same_age:
            age_version = (prior_age.version + 1) if prior_age else 1
            session.add(
                AgeAssurance(
                    tenant_id=tenant_id,
                    user_id=user.id,
                    age_band=payload.age_band,
                    status=payload.age_status,
                    method=payload.age_method,
                    jurisdiction=payload.jurisdiction,
                    verified_at=payload.verified_at,
                    expires_at=payload.expires_at,
                    version=age_version,
                )
            )
        else:
            age_version = prior_age.version
        receipt_result = "NOT_ACCEPTED"
        if payload.disclosure_accepted:
            policy = await session.scalar(
                select(RegionalPolicyProfile)
                .where(
                    RegionalPolicyProfile.tenant_id == tenant_id,
                    RegionalPolicyProfile.status == "active",
                    RegionalPolicyProfile.effective_from <= now,
                    RegionalPolicyProfile.effective_to.is_(None)
                    | (RegionalPolicyProfile.effective_to > now),
                )
                .order_by(RegionalPolicyProfile.effective_from.desc())
                .limit(1)
                .with_for_update()
            )
            if policy is None:
                raise HTTPException(status_code=503, detail={"code": "policy_unavailable"})
            if not policy.rules.get("ai_disclosure_required", False):
                raise HTTPException(status_code=503, detail={"code": "disclosure_policy_invalid"})
            receipt_digest = hashlib.sha256(
                f"gpoint:{gpoint_user_id}:{payload.disclosure_receipt_id}".encode()
            ).hexdigest()
            receipt_key = f"gpoint:{receipt_digest}"
            existing_receipt = await session.scalar(
                select(AIDisclosureReceipt).where(
                    AIDisclosureReceipt.tenant_id == tenant_id,
                    AIDisclosureReceipt.receipt_key == receipt_key,
                )
            )
            if existing_receipt is None:
                session.add(
                    AIDisclosureReceipt(
                        tenant_id=tenant_id,
                        user_id=user.id,
                        policy_profile_id=policy.id,
                        policy_version=policy.version,
                        channel="gpoint_internal",
                        receipt_key=receipt_key,
                        displayed_at=now,
                    )
                )
            receipt_result = "RECORDED"
        await session.flush()
        return {"user_id": str(user.id), "age_version": age_version, "disclosure": receipt_result}


@router.post(
    "/respond", response_model=RespondResponse, dependencies=[Depends(require_internal_auth)]
)
async def respond(
    payload: RespondRequest,
    response_provider: ResponseProvider = Depends(get_response_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    session: AsyncSession = Depends(get_session),
) -> RespondResponse:
    settings = get_settings()
    tenant_id = UUID(settings.tenant_id)
    now = datetime.now(UTC)
    crypto = AeadCrypto(encoded_key=settings.message_aead_key.get_secret_value(), key_version="v1")
    fingerprinter = RequestFingerprinter(
        encoded_key=settings.request_fingerprint_hmac_key.get_secret_value()
    )
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        external_character = await session.scalar(
            select(Character).where(
                Character.tenant_id == tenant_id,
                Character.source_system == "gpoint",
                Character.external_id == payload.gpoint_character_id,
                Character.status == "active",
            )
        )
        if external_character is None:
            raise HTTPException(status_code=404, detail={"code": "character_unavailable"})
        user = await session.scalar(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.source_system == "gpoint",
                User.external_id == payload.gpoint_user_id,
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if user is None:
            user = User(
                tenant_id=tenant_id,
                source_system="gpoint",
                external_id=payload.gpoint_user_id,
                locale=payload.locale,
            )
            session.add(user)
            await session.flush()
        elif payload.locale:
            user.locale = payload.locale
        consent = await session.scalar(
            select(Consent.id)
            .where(
                Consent.tenant_id == tenant_id,
                Consent.user_id == user.id,
                Consent.purpose == "chat_history",
                Consent.revoked_at.is_(None),
            )
            .limit(1)
        )
        if consent is None:
            raise HTTPException(status_code=403, detail={"code": "consent_required"})
        principal = PrincipalContext(tenant_id, user.id, user.id, "internal")
        try:
            relationship = await resolve_relationship(
                session, principal, external_character.id, Scope.real_world, None
            )
        except RelationshipUnavailable as exc:
            raise HTTPException(
                status_code=409, detail={"code": str(exc)}
            ) from exc
        conversation = None
        external_conversation_id = payload.conversation_id or f"implicit:{payload.message_id}"
        conversation = await session.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user.id,
                Conversation.character_id == external_character.id,
                Conversation.source_system == "gpoint",
                Conversation.external_id == external_conversation_id,
                Conversation.relationship_id == relationship.id,
            )
            .with_for_update()
        )
        if conversation is not None and conversation.closed_at is not None:
            raise HTTPException(status_code=409, detail={"code": "conversation_closed"})
        if conversation is None:
            conversation = Conversation(
                tenant_id=tenant_id,
                user_id=user.id,
                character_id=external_character.id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                scenario_id=None,
                started_at=now,
                language_code=_language_code(payload.locale),
                source_system="gpoint",
                external_id=external_conversation_id,
            )
            session.add(conversation)
            await session.flush()
        existing_message = await session.scalar(
            select(Message).where(
                Message.tenant_id == tenant_id,
                Message.idempotency_key == payload.message_id,
            )
        )
        if existing_message is not None:
            if (
                existing_message.user_id != user.id
                or existing_message.character_id != external_character.id
                or existing_message.conversation_id != conversation.id
            ):
                raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
            now = existing_message.occurred_at
        user_id = user.id
        character_id = external_character.id
        conversation_id = conversation.id
    try:
        ingest = await MessageIngestionService(session, crypto, fingerprinter).ingest(
            principal,
            IngestMessage(
                conversation_id=conversation_id,
                character_id=character_id,
                idempotency_key=payload.message_id,
                role="user",
                plaintext=payload.text,
                occurred_at=now,
            ),
        )
    except ValueError as exc:
        code = getattr(exc, "code", "request_rejected")
        raise HTTPException(
            status_code=409 if code == "idempotency_conflict" else 403, detail={"code": code}
        ) from exc
    if not ingest.replayed:
        try:
            await record_accepted_interaction(
                session, principal, relationship.id, now
            )
        except RelationshipUnavailable as exc:
            raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    if response_provider is None:
        # The encrypted user message is already durable. Do not create a
        # response run or queue inference while production has no provider.
        raise HTTPException(status_code=503, detail={"code": "ai_unavailable"})
    orchestrator = ResponseOrchestrator(
        session,
        crypto,
        HybridRetrievalService(
            session,
            embedding_provider,
            hmac_key=base64.b64decode(
                settings.retrieval_query_hmac_key.get_secret_value(), validate=True
            ),
        ),
        response_provider,
        hmac_key=base64.b64decode(
            settings.response_fingerprint_hmac_key.get_secret_value(), validate=True
        ),
        provider_timeout_seconds=settings.llm_timeout_seconds,
    )
    result = await orchestrator.respond(
        principal,
        ResponseRequest(
            conversation_id=conversation_id,
            user_message_id=ingest.message_id,
            idempotency_key=payload.message_id,
            as_of=now,
        ),
    )
    if result.status == "pending":
        # Pending/running lifecycle is owned by the inference outbox. A
        # ResponseRun is intentionally created only when the assistant message
        # is committed, so retries recover from the deterministic outbox key.
        return RespondResponse(
            status="pending",
            request_id=payload.request_id,
            conversation_id=str(conversation_id),
            character_id=str(character_id),
        )
    if result.status != "completed" or result.assistant_message_id is None:
        code = (
            "ai_unavailable"
            if result.reason_code in {"provider_unavailable", "inference_retry_exhausted"}
            else result.reason_code
        )
        raise HTTPException(status_code=503, detail={"code": code})
    async with session.begin():
        await set_tenant_context(session, str(tenant_id))
        assistant = await session.scalar(
            select(Message).where(
                Message.tenant_id == tenant_id,
                Message.id == result.assistant_message_id,
                Message.user_id == user_id,
                Message.character_id == character_id,
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
    if (
        assistant is None
        or assistant.content_nonce is None
        or assistant.content_key_version is None
    ):
        raise HTTPException(status_code=503, detail={"code": "response_persistence_failed"})
    from app.messages.crypto import CiphertextEnvelope

    text = crypto.decrypt(
        CiphertextEnvelope(
            assistant.content_ciphertext, assistant.content_nonce, assistant.content_key_version
        ),
        MessageIngestionService._aad_for(tenant_id, user.id, conversation.id, assistant.id),
    )
    return RespondResponse(
        status="completed",
        request_id=payload.request_id,
        conversation_id=str(conversation.id),
        message_id=str(assistant.id),
        character_id=str(character_id),
        text=text,
    )
