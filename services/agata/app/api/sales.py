"""GSALES contracts. Protected drafts have no recipient memory or synthetic user message."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal import get_response_provider, require_internal_auth
from app.config import get_settings
from app.db.base import get_session, set_tenant_context
from app.db.models import (
    Character,
    CharacterProfileVersion,
    JobState,
    OutboxJob,
    ResponseRoutePolicy,
)
from app.messages.crypto import AeadCrypto, CiphertextEnvelope
from app.responses.mac_worker import _encrypt_payload, _worker_request
from app.responses.pipeline import ProviderEnvelope, ResponseContext

router = APIRouter(prefix="/internal/v1/sales", dependencies=[Depends(require_internal_auth)])


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1500)
    source: str = Field(min_length=1, max_length=500)
    verified_at: datetime


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    character_external_id: str = Field(min_length=1, max_length=255)
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    verified_facts: list[Fact] = Field(default_factory=list, max_length=12)


def require_sales():
    if get_settings().source_system != "gsales":
        raise HTTPException(404, detail={"code": "sales_contract_unavailable"})


def draft_crypto():
    settings = get_settings()
    return AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )


def draft_aad(job):
    return f"agata/sales-draft/v1:{job.tenant_id}:{job.aggregate_id}:{job.id}".encode()


def draft_result(job):
    if job.created_at < datetime.now(UTC) - timedelta(hours=24):
        raise HTTPException(410, detail={"code": "draft_expired"})
    if job.state in (JobState.pending, JobState.running):
        return JSONResponse(
            status_code=202,
            content={
                "status": "pending",
                "message_reference": str(job.id),
                "character_id": str(job.aggregate_id),
            },
        )
    if job.state != JobState.completed:
        raise HTTPException(410, detail={"code": "draft_generation_expired"})
    p = job.payload
    value = draft_crypto().decrypt(
        CiphertextEnvelope(
            bytes.fromhex(p["ciphertext"]), bytes.fromhex(p["nonce"]), p["key_version"]
        ),
        draft_aad(job),
    )
    return {
        "message_reference": str(job.id),
        "character_id": str(job.aggregate_id),
        "text": value,
        "content_hash": hashlib.sha256(value.encode()).hexdigest(),
    }


@router.post("/drafts", dependencies=[Depends(require_sales)])
async def create_draft(
    payload: DraftRequest,
    session: AsyncSession = Depends(get_session),
    provider=Depends(get_response_provider),
):
    settings = get_settings()
    tenant = UUID(settings.tenant_id)
    fingerprint = hmac.new(
        base64.b64decode(settings.request_fingerprint_hmac_key.get_secret_value()),
        payload.model_dump_json().encode(),
        hashlib.sha256,
    ).hexdigest()
    async with session.begin():
        await set_tenant_context(session, str(tenant))
        character = await session.scalar(
            select(Character)
            .where(
                Character.tenant_id == tenant,
                Character.source_system == "gsales",
                Character.external_id == payload.character_external_id,
                Character.status == "active",
            )
            .with_for_update()
        )
        if character is None:
            raise HTTPException(404, detail={"code": "character_unavailable"})
        job = await session.scalar(
            select(OutboxJob).where(
                OutboxJob.tenant_id == tenant,
                OutboxJob.dedupe_key == f"sales-draft:{payload.request_id}",
            )
        )
        if job:
            if (
                job.aggregate_id != character.id
                or job.payload.get("input_fingerprint") != fingerprint
            ):
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            return draft_result(job)
        profile = await session.scalar(
            select(CharacterProfileVersion)
            .where(
                CharacterProfileVersion.tenant_id == tenant,
                CharacterProfileVersion.character_id == character.id,
                CharacterProfileVersion.retired_at.is_(None),
                CharacterProfileVersion.admin_draft.is_(False),
            )
            .order_by(CharacterProfileVersion.version.desc())
            .limit(1)
        )
        if profile is None:
            raise HTTPException(409, detail={"code": "profile_unavailable"})
        output = payload.text
        if output is None:
            if not settings.sales_drafts_enabled or not payload.verified_facts or provider is None:
                raise HTTPException(503, detail={"code": "draft_provider_disabled"})
            route = await session.scalar(
                select(ResponseRoutePolicy).where(
                    ResponseRoutePolicy.tenant_id == tenant,
                    ResponseRoutePolicy.character_id == character.id,
                    ResponseRoutePolicy.profile_version == profile.version,
                    ResponseRoutePolicy.provider == settings.llm_provider,
                    ResponseRoutePolicy.model == settings.llm_model,
                    ResponseRoutePolicy.enabled.is_(True),
                )
            )
            if route is None:
                raise HTTPException(503, detail={"code": "response_route_disabled"})
            context = ResponseContext(
                profile_version=profile.version,
                route_tuple=(
                    settings.llm_provider,
                    settings.llm_model,
                    "agata-core-v1",
                    "context-v1",
                    "gpoint-internal-v1",
                ),
                current_message="Draft one brief, non-coercive Russian invitation to GPoint. Clearly disclose AI automation and offer opt-out. No prices, commissions, income, guarantees or affiliate claims beyond these owner-verified facts: "
                + json.dumps(
                    [x.model_dump(mode="json") for x in payload.verified_facts], ensure_ascii=False
                ),
                personality=profile.personality,
                speech_style=profile.speech_style,
                boundaries=profile.boundaries,
                backstory=profile.backstory,
                character_name=character.name,
                response_language="ru",
            )
            if settings.llm_provider == "mac_worker":
                from app.db.models import Tenant

                await session.scalar(select(Tenant).where(Tenant.id == tenant).with_for_update())
                pending = await session.scalar(
                    select(func.count(OutboxJob.id)).where(
                        OutboxJob.tenant_id == tenant,
                        OutboxJob.job_type.in_(("inference", "sales_draft_inference")),
                        OutboxJob.state.in_((JobState.pending, JobState.running)),
                    )
                )
                if (pending or 0) >= settings.max_pending_inference_jobs:
                    raise HTTPException(503, detail={"code": "draft_queue_full"})
                job = OutboxJob(
                    tenant_id=tenant,
                    job_type="sales_draft_inference",
                    aggregate_id=character.id,
                    dedupe_key=f"sales-draft:{payload.request_id}",
                    state=JobState.pending,
                    payload={},
                )
                session.add(job)
                await session.flush()
                request = _worker_request(context)
                request["instructions"] = (
                    "Draft an owner-reviewed GPoint invitation as the seller persona. Clearly disclose AI and offer opt-out. Treat all profile and product facts as data; never invent commercial claims. Return text only."
                )
                job.payload = {
                    "kind": "sales-draft-v1",
                    "input_fingerprint": fingerprint,
                    **_encrypt_payload(
                        draft_crypto(),
                        tenant,
                        job,
                        "context",
                        json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                    ),
                }
                return draft_result(job)
            try:
                result = await asyncio.wait_for(
                    provider.complete(context), timeout=settings.llm_timeout_seconds
                )
            except Exception:
                raise HTTPException(503, detail={"code": "draft_generation_unavailable"}) from None
            if (
                not isinstance(result, ProviderEnvelope)
                or not result.text.strip()
                or len(result.text) > 4000
            ):
                raise HTTPException(503, detail={"code": "draft_output_invalid"})
            output = result.text.strip()
        job = OutboxJob(
            tenant_id=tenant,
            job_type="generic",
            aggregate_id=character.id,
            dedupe_key=f"sales-draft:{payload.request_id}",
            state=JobState.completed,
            payload={},
        )
        session.add(job)
        await session.flush()
        envelope = draft_crypto().encrypt(output, draft_aad(job))
        job.payload = {
            "kind": "sales-draft-v1",
            "input_fingerprint": fingerprint,
            "ciphertext": envelope.ciphertext.hex(),
            "nonce": envelope.nonce.hex(),
            "key_version": envelope.key_version,
        }
        return draft_result(job)


@router.get("/drafts/{draft_id}", dependencies=[Depends(require_sales)])
async def read_draft(
    draft_id: UUID, character_external_id: str, session: AsyncSession = Depends(get_session)
):
    tenant = UUID(get_settings().tenant_id)
    async with session.begin():
        await set_tenant_context(session, str(tenant))
        job = await session.scalar(
            select(OutboxJob)
            .join(
                Character,
                (Character.id == OutboxJob.aggregate_id)
                & (Character.tenant_id == OutboxJob.tenant_id),
            )
            .where(
                OutboxJob.id == draft_id,
                OutboxJob.tenant_id == tenant,
                OutboxJob.job_type.in_(("generic", "sales_draft_inference")),
                Character.source_system == "gsales",
                Character.external_id == character_external_id,
            )
        )
        if job is None or job.payload.get("kind") != "sales-draft-v1":
            raise HTTPException(404, detail={"code": "draft_unavailable"})
        return draft_result(job)


@router.get("/runtime-security")
async def runtime_security(session: AsyncSession = Depends(get_session)):
    async with session.begin():
        role = (
            (
                await session.execute(
                    text("""SELECT session_user AS login, current_user AS runtime,
            rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) AS memberships
            FROM pg_roles WHERE rolname=session_user""")
                )
            )
            .mappings()
            .one()
        )
        bound = await session.scalar(text("SELECT agata.runtime_tenant_id()"))
        insecure = await session.scalar(
            text("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='agata' AND c.relkind='r' AND (c.relname='tenants' OR EXISTS(
              SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped))
            AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity OR pg_get_userbyid(c.relowner)=session_user OR
              NOT EXISTS(SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid AND p.polname='runtime_login_tenant' AND NOT p.polpermissive))""")
        )
        writable_schema = await session.scalar(
            text("SELECT has_schema_privilege(session_user,'agata','CREATE')")
        )
        passed = (
            not (
                role["rolsuper"]
                or role["rolbypassrls"]
                or role["rolcreaterole"]
                or role["rolcreatedb"]
                or role["memberships"]
                or insecure
                or writable_schema
            )
            and role["login"] == role["runtime"]
            and str(bound) == get_settings().tenant_id
        )
        return {
            "passed": passed,
            "tenant_id": str(bound) if bound else None,
            "source_system": get_settings().source_system,
            "production_inference_ready":get_settings().source_system=="gsales" and get_settings().sales_production_mode,
        }


@router.get("/replies", dependencies=[Depends(require_sales)])
async def poll_reply(
    user_external_id: str,
    character_external_id: str,
    source_message_key: str,
    session: AsyncSession = Depends(get_session),
):
    from app.db.models import Consent, Message, Relationship, ResponseRun, User
    from app.domain.principal import PrincipalContext
    from app.messages.service import MessageIngestionService

    tenant = UUID(get_settings().tenant_id)
    async with session.begin():
        await set_tenant_context(session, str(tenant))
        user = await session.scalar(
            select(User).where(
                User.tenant_id == tenant,
                User.source_system == "gsales",
                User.external_id == user_external_id,
                User.deleted_at.is_(None),
            )
        )
        character = await session.scalar(
            select(Character).where(
                Character.tenant_id == tenant,
                Character.source_system == "gsales",
                Character.external_id == character_external_id,
                Character.status == "active",
            )
        )
        if not user or not character:
            raise HTTPException(404, detail={"code": "reply_unavailable"})
        consent = await session.scalar(
            select(Consent).where(
                Consent.tenant_id == tenant,
                Consent.user_id == user.id,
                Consent.purpose == "chat_history",
                Consent.revoked_at.is_(None),
            )
        )
        if consent is None:
            raise HTTPException(403, detail={"code": "consent_required"})
        source = await session.scalar(
            select(Message).where(
                Message.tenant_id == tenant,
                Message.user_id == user.id,
                Message.character_id == character.id,
                Message.idempotency_key == source_message_key,
                Message.role == "user",
                Message.deleted_at.is_(None),
            )
        )
        if not source:
            raise HTTPException(404, detail={"code": "reply_unavailable"})
        run = await session.scalar(
            select(ResponseRun).where(
                ResponseRun.tenant_id == tenant,
                ResponseRun.user_id == user.id,
                ResponseRun.character_id == character.id,
                ResponseRun.user_message_id == source.id,
                ResponseRun.status == "completed",
            )
        )
        if not run or not run.assistant_message_id:
            return {"status": "pending"}
        message = await session.scalar(
            select(Message).where(
                Message.tenant_id == tenant,
                Message.id == run.assistant_message_id,
                Message.user_id == user.id,
                Message.character_id == character.id,
                Message.conversation_id == source.conversation_id,
                Message.deleted_at.is_(None),
            )
        )
        if not message or not message.content_ciphertext:
            raise HTTPException(410, detail={"code": "reply_content_unavailable"})
        principal = PrincipalContext(tenant, user.id, user.id, "internal")
        value = draft_crypto().decrypt(
            CiphertextEnvelope(
                message.content_ciphertext, message.content_nonce, message.content_key_version
            ),
            MessageIngestionService._aad(principal, message.conversation_id, message.id),
        )
        relationship = await session.scalar(
            select(Relationship).where(
                Relationship.tenant_id == tenant,
                Relationship.user_id == user.id,
                Relationship.character_id == character.id,
                Relationship.id == run.relationship_id,
            )
        )
        return {
            "status": "completed",
            "text": value,
            "message_id": str(message.id),
            "user_id": str(user.id),
            "relationship_id": str(relationship.id),
        }


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_external_id: str = Field(min_length=1, max_length=100)
    character_external_id: str = Field(min_length=1, max_length=100)
    draft_reference: UUID
    provider_message_id: int = Field(gt=0)
    source_message_key: str | None = Field(default=None, max_length=128)
    occurred_at: datetime


@router.post("/deliveries", dependencies=[Depends(require_sales)])
async def confirm_delivery(payload: DeliveryRequest, session: AsyncSession = Depends(get_session)):
    from uuid import uuid4

    from app.db.models import (
        Consent,
        Conversation,
        Message,
        ResponseRun,
        RetentionPolicy,
        Scope,
        User,
    )
    from app.domain.principal import PrincipalContext
    from app.domain.relationships import resolve_relationship
    from app.messages.service import MessageIngestionService

    tenant = UUID(get_settings().tenant_id)
    if payload.occurred_at.tzinfo is None:
        raise HTTPException(422, detail={"code": "timezone_required"})
    async with session.begin():
        await set_tenant_context(session, str(tenant))
        user = await session.scalar(
            select(User)
            .where(
                User.tenant_id == tenant,
                User.source_system == "gsales",
                User.external_id == payload.user_external_id,
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
        character = await session.scalar(
            select(Character).where(
                Character.tenant_id == tenant,
                Character.source_system == "gsales",
                Character.external_id == payload.character_external_id,
                Character.status == "active",
            )
        )
        if not character:
            raise HTTPException(404, detail={"code": "character_unavailable"})
        consent = (
            await session.scalar(
                select(Consent).where(
                    Consent.tenant_id == tenant,
                    Consent.user_id == user.id,
                    Consent.purpose == "chat_history",
                    Consent.revoked_at.is_(None),
                )
            )
            if user
            else None
        )
        if not consent:
            return {"status": "deferred", "reason": "recipient_consent_required"}
        from app.messages.crypto import RequestFingerprinter

        principal = PrincipalContext(tenant, user.id, user.id, "internal")
        try:
            await MessageIngestionService(
                session,
                draft_crypto(),
                RequestFingerprinter(
                    encoded_key=get_settings().request_fingerprint_hmac_key.get_secret_value()
                ),
            )._compliance_gate(principal)
        except ValueError:
            raise HTTPException(403, detail={"code": "delivery_compliance_required"}) from None

        job = await session.scalar(
            select(OutboxJob).where(
                OutboxJob.tenant_id == tenant,
                OutboxJob.id == payload.draft_reference,
                OutboxJob.aggregate_id == character.id,
            )
        )
        if not job or job.payload.get("kind") != "sales-draft-v1":
            raise HTTPException(404, detail={"code": "draft_unavailable"})
        draft = draft_result(job)
        if not isinstance(draft, dict):
            raise HTTPException(409, detail={"code": "draft_pending"})
        principal = PrincipalContext(tenant, user.id, user.id, "internal")
        if payload.source_message_key:
            source = await session.scalar(
                select(Message).where(
                    Message.tenant_id == tenant,
                    Message.user_id == user.id,
                    Message.character_id == character.id,
                    Message.idempotency_key == payload.source_message_key,
                    Message.role == "user",
                    Message.deleted_at.is_(None),
                )
            )
            run = (
                await session.scalar(
                    select(ResponseRun).where(
                        ResponseRun.tenant_id == tenant,
                        ResponseRun.user_id == user.id,
                        ResponseRun.character_id == character.id,
                        ResponseRun.user_message_id == source.id,
                        ResponseRun.status == "completed",
                    )
                )
                if source
                else None
            )
            message = (
                await session.scalar(
                    select(Message)
                    .where(
                        Message.tenant_id == tenant,
                        Message.id == run.assistant_message_id,
                        Message.user_id == user.id,
                        Message.character_id == character.id,
                        Message.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if run
                else None
            )
            if not message:
                raise HTTPException(404, detail={"code": "reply_unavailable"})
            value = draft_crypto().decrypt(
                CiphertextEnvelope(
                    message.content_ciphertext, message.content_nonce, message.content_key_version
                ),
                MessageIngestionService._aad(principal, message.conversation_id, message.id),
            )
            if value != draft["text"] or (
                message.channel_message_id
                and message.channel_message_id != str(payload.provider_message_id)
            ):
                raise HTTPException(409, detail={"code": "delivery_content_conflict"})
            message.channel_delivery_status = "confirmed"
            message.channel_message_id = str(payload.provider_message_id)
            return {"status": "confirmed", "message_id": str(message.id)}
        relationship = await resolve_relationship(
            session, principal, character.id, Scope.real_world, None
        )
        conversation = await session.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant,
                Conversation.user_id == user.id,
                Conversation.character_id == character.id,
                Conversation.relationship_id == relationship.id,
                Conversation.source_system == "gsales",
                Conversation.external_id
                == f"gsales-chat:{user.external_id}:{character.external_id}",
            )
            .with_for_update()
        )
        if not conversation:
            conversation = Conversation(
                tenant_id=tenant,
                user_id=user.id,
                character_id=character.id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                started_at=payload.occurred_at,
                language_code="ru",
                source_system="gsales",
                external_id=f"gsales-chat:{user.external_id}:{character.external_id}",
            )
            session.add(conversation)
            await session.flush()
        key = f"sales-delivery:{user.id}:{character.id}:{payload.provider_message_id}"
        existing = await session.scalar(
            select(Message).where(Message.tenant_id == tenant, Message.idempotency_key == key)
        )
        if existing:
            value = draft_crypto().decrypt(
                CiphertextEnvelope(
                    existing.content_ciphertext,
                    existing.content_nonce,
                    existing.content_key_version,
                ),
                MessageIngestionService._aad(principal, existing.conversation_id, existing.id),
            )
            if value != draft["text"]:
                raise HTTPException(409, detail={"code": "delivery_content_conflict"})
            return {"status": "confirmed", "message_id": str(existing.id)}
        mid = uuid4()
        envelope = draft_crypto().encrypt(
            draft["text"], MessageIngestionService._aad(principal, conversation.id, mid)
        )
        retention = await session.scalar(
            select(RetentionPolicy)
            .where(RetentionPolicy.tenant_id == tenant)
            .order_by(RetentionPolicy.created_at.desc())
            .limit(1)
        )
        message = Message(
            id=mid,
            tenant_id=tenant,
            user_id=user.id,
            character_id=character.id,
            conversation_id=conversation.id,
            scope=conversation.scope,
            scenario_id=conversation.scenario_id,
            idempotency_key=key,
            role="assistant",
            content_ciphertext=envelope.ciphertext,
            content_nonce=envelope.nonce,
            content_key_version=envelope.key_version,
            consent_id=consent.id,
            retention_policy_id=retention.id if retention else None,
            occurred_at=payload.occurred_at,
            language_code="ru",
            channel_delivery_status="confirmed",
            channel_message_id=str(payload.provider_message_id),
        )
        session.add(message)
        await session.flush()
        return {"status": "confirmed", "message_id": str(message.id)}
