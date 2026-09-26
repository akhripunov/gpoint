from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.policy import PolicyService
from app.compliance.receipt import current_disclosure
from app.db.base import set_tenant_context
from app.db.models import (
    AuditLog,
    Character,
    Consent,
    Conversation,
    MemoryControlState,
    Message,
    RetentionPolicy,
    User,
)
from app.domain.principal import PrincipalContext
from app.jobs.outbox import OutboxRepository
from app.messages.crypto import AeadCrypto, RequestFingerprinter


class DomainError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IngestMessage:
    conversation_id: UUID
    character_id: UUID
    idempotency_key: str
    role: str
    plaintext: str
    occurred_at: datetime
    reply_context: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.conversation_id, UUID)
            or not isinstance(self.character_id, UUID)
            or not isinstance(self.idempotency_key, str)
            or not isinstance(self.role, str)
            or not isinstance(self.plaintext, str)
            or not isinstance(self.occurred_at, datetime)
            or self.role != "user"
            or not 1 <= len(self.idempotency_key) <= 255
            or not 1 <= len(self.plaintext) <= 16_384
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
            or (
                self.reply_context is not None
                and (
                    not isinstance(self.reply_context, str)
                    or not 1 <= len(self.reply_context) <= 4096
                    or "\x00" in self.reply_context
                )
            )
        ):
            raise DomainError("invalid_message_request")


@dataclass(frozen=True, slots=True)
class IngestResult:
    message_id: UUID
    replayed: bool


class MessageIngestionService:
    def __init__(
        self, session: AsyncSession, crypto: AeadCrypto, fingerprinter: RequestFingerprinter
    ) -> None:
        self.session = session
        self.crypto = crypto
        self.fingerprinter = fingerprinter

    async def ingest(self, principal: PrincipalContext, request: IngestMessage) -> IngestResult:
        if not isinstance(principal, PrincipalContext) or not isinstance(request, IngestMessage):
            raise DomainError("invalid_message_request")
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            # Serializes same tenant/key races before the read-then-insert idempotency path.
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{principal.tenant_id}:{request.idempotency_key}"},
            )
            fingerprint = self.fingerprinter.fingerprint(
                tenant_id=principal.tenant_id,
                principal_user_id=principal.user_id,
                conversation_id=request.conversation_id,
                character_id=request.character_id,
                role=request.role,
                plaintext=request.plaintext,
                occurred_at=request.occurred_at,
            )
            existing = await self.session.scalar(
                select(Message).where(
                    Message.tenant_id == principal.tenant_id,
                    Message.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise DomainError("idempotency_conflict")
                return IngestResult(existing.id, True)
            conversation = await self.session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == request.conversation_id,
                    Conversation.tenant_id == principal.tenant_id,
                    Conversation.user_id == principal.user_id,
                    Conversation.character_id == request.character_id,
                    Conversation.closed_at.is_(None),
                )
                .with_for_update()
            )
            if conversation is None:
                raise DomainError("message_not_permitted")
            if not await self._active(User, principal.tenant_id, principal.user_id):
                raise DomainError("message_not_permitted")
            if not await self._active(Character, principal.tenant_id, request.character_id):
                raise DomainError("message_not_permitted")
            chat_consent = await self._current_consent(principal, "chat_history")
            if chat_consent is None:
                raise DomainError("chat_history_consent_required")
            await self._compliance_gate(principal)
            message_id = uuid4()
            aad = self._aad(principal, conversation.id, message_id)
            envelope = self.crypto.encrypt(request.plaintext, aad)
            retention = await self.session.scalar(
                select(RetentionPolicy)
                .where(RetentionPolicy.tenant_id == principal.tenant_id)
                .order_by(RetentionPolicy.created_at.desc())
                .limit(1)
            )
            message = Message(
                id=message_id,
                tenant_id=principal.tenant_id,
                conversation_id=conversation.id,
                user_id=principal.user_id,
                character_id=conversation.character_id,
                scope=conversation.scope,
                scenario_id=conversation.scenario_id,
                idempotency_key=request.idempotency_key,
                role="user",
                content_ciphertext=envelope.ciphertext,
                content_nonce=envelope.nonce,
                content_key_version=envelope.key_version,
                request_fingerprint=fingerprint,
                consent_id=chat_consent.id,
                retention_policy_id=retention.id if retention else None,
                occurred_at=request.occurred_at,
                language_code=self._language_code(request.plaintext),
            )
            self.session.add(message)
            self.session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor_type=principal.channel,
                    actor_id=principal.actor_id,
                    action="message_ingested",
                    object_id=message_id,
                    metadata_json={"role": "user"},
                )
            )
            memory_consent = await self._current_consent(principal, "long_term_memory")
            if memory_consent is not None:
                control = await self._control(principal, conversation)
                context_payload: dict[str, str] = {}
                if request.reply_context is not None:
                    context_envelope = self.crypto.encrypt(
                        request.reply_context,
                        self._reply_context_aad(
                            principal.tenant_id, principal.user_id, conversation.id, message_id
                        ),
                    )
                    context_payload = {
                        "reply_context_ciphertext": context_envelope.ciphertext.hex(),
                        "reply_context_nonce": context_envelope.nonce.hex(),
                        "reply_context_key_version": str(context_envelope.key_version),
                    }
                await OutboxRepository(self.session).enqueue(
                    tenant_id=principal.tenant_id,
                    job_type="memory_extract",
                    dedupe_key=f"extract:{message_id}",
                    aggregate_id=message_id,
                    creation_epoch=control.deletion_epoch,
                    memory_control_state_id=control.id,
                    payload={
                        "message_id": str(message_id),
                        "conversation_id": str(conversation.id),
                        "code": "extract",
                        **context_payload,
                    },
                )
            await self.session.flush()
            return IngestResult(message_id, False)

    async def _active(
        self, model: type[User] | type[Character], tenant_id: UUID, entity_id: UUID
    ) -> bool:
        entity = await self.session.scalar(
            select(model).where(model.id == entity_id, model.tenant_id == tenant_id)
        )
        return (
            entity is not None
            and getattr(entity, "deleted_at", None) is None
            and getattr(entity, "status", "active") == "active"
        )

    async def _current_consent(self, principal: PrincipalContext, purpose: str) -> Consent | None:
        return await self.session.scalar(
            select(Consent)
            .where(
                Consent.tenant_id == principal.tenant_id,
                Consent.user_id == principal.user_id,
                Consent.purpose == purpose,
                Consent.revoked_at.is_(None),
            )
            .order_by(Consent.granted_at.desc(), Consent.id.desc())
            .limit(1)
            .with_for_update()
        )

    async def _compliance_gate(self, principal: PrincipalContext) -> None:
        now = datetime.now(UTC)
        if not await PolicyService().age_assurance_eligible(
            self.session, tenant_id=principal.tenant_id, user_id=principal.user_id, now=now
        ):
            raise DomainError("compliance_not_permitted")
        disclosure = await current_disclosure(
            self.session, tenant_id=principal.tenant_id, user_id=principal.user_id
        )
        if disclosure is None or disclosure.receipt is None:
            raise DomainError("compliance_not_permitted")

    async def _control(
        self, principal: PrincipalContext, conversation: Conversation
    ) -> MemoryControlState:
        control = await self.session.scalar(
            select(MemoryControlState)
            .where(
                MemoryControlState.tenant_id == principal.tenant_id,
                MemoryControlState.relationship_id == conversation.relationship_id,
                MemoryControlState.user_id == principal.user_id,
                MemoryControlState.character_id == conversation.character_id,
                MemoryControlState.scope == conversation.scope,
                MemoryControlState.scenario_id == conversation.scenario_id,
            )
            .with_for_update()
        )
        if control is None:
            control = MemoryControlState(
                tenant_id=principal.tenant_id,
                relationship_id=conversation.relationship_id,
                user_id=principal.user_id,
                character_id=conversation.character_id,
                scope=conversation.scope,
                scenario_id=conversation.scenario_id,
            )
            self.session.add(control)
            await self.session.flush()
        return control

    @staticmethod
    def _aad(principal: PrincipalContext, conversation_id: UUID, message_id: UUID) -> bytes:
        return MessageIngestionService._aad_for(
            principal.tenant_id, principal.user_id, conversation_id, message_id
        )

    @staticmethod
    def _aad_for(tenant_id: UUID, user_id: UUID, conversation_id: UUID, message_id: UUID) -> bytes:
        return f"{tenant_id}:{user_id}:{conversation_id}:{message_id}".encode()

    @staticmethod
    def _reply_context_aad(
        tenant_id: UUID, user_id: UUID, conversation_id: UUID, message_id: UUID
    ) -> bytes:
        return f"reply-context:{tenant_id}:{user_id}:{conversation_id}:{message_id}".encode()

    @staticmethod
    def _language_code(value: str) -> str:
        """Minimal finite metadata only; no provider/model supplied language is trusted."""
        cyrillic = sum("\u0400" <= char <= "\u052f" for char in value)
        latin = sum("a" <= char.casefold() <= "z" for char in value)
        if cyrillic and latin:
            return "mixed"
        if cyrillic:
            return "ru"
        if latin:
            return "en"
        return "unknown"
