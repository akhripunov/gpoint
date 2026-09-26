from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_session, set_tenant_context
from app.db.models import (
    AgeAssurance,
    AIDisclosureReceipt,
    Character,
    CharacterProfileVersion,
    Consent,
    Conversation,
    MemorySnapshot,
    Message,
    RegionalPolicyProfile,
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
from app.memory.embedding import HuggingFaceFreeEmbeddingProvider
from app.memory.retrieval import EmbeddingProvider, HybridRetrievalService
from app.messages.crypto import AeadCrypto, RequestFingerprinter
from app.messages.service import IngestMessage, MessageIngestionService
from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.pipeline import ResponseOrchestrator, ResponseProvider, ResponseRequest

router = APIRouter(prefix="/internal/v1", tags=["internal"])


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
    request_id: str
    conversation_id: str
    message_id: str
    character_id: str
    text: str


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


def _language_code(locale: str | None) -> str:
    """Map BCP-47 locale tags to the finite language codes stored by AGATA."""
    if not locale:
        return "unknown"
    language = locale.split("-", 1)[0].lower()
    return language if language in {"ru", "en"} else "mixed"


def require_internal_auth(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {get_settings().internal_api_token.get_secret_value()}"
    if not isinstance(authorization, str) or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "unauthorized"}
        )


def get_response_provider() -> ResponseProvider:
    settings = get_settings()
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail={"code": "provider_unconfigured"})
    return DeepSeekResponseProvider(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_api_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        provider=settings.llm_provider,
        model=settings.llm_model,
    )


def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if not settings.embedding_api_key:
        raise HTTPException(status_code=503, detail={"code": "embedding_provider_unconfigured"})
    return HuggingFaceFreeEmbeddingProvider(
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
    )


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
        same_age = prior_age is not None and (
            prior_age.age_band == payload.age_band
            and prior_age.status == payload.age_status
            and prior_age.method == payload.age_method
            and prior_age.jurisdiction == payload.jurisdiction
            and prior_age.verified_at == payload.verified_at
            and prior_age.expires_at == payload.expires_at
        )
        if not same_age:
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
    if result.status != "completed" or result.assistant_message_id is None:
        raise HTTPException(status_code=503, detail={"code": result.reason_code})
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
        request_id=payload.request_id,
        conversation_id=str(conversation.id),
        message_id=str(assistant.id),
        character_id=str(character_id),
        text=text,
    )
