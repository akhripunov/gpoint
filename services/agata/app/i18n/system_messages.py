"""Controlled encrypted persistence for finite localized system messages."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    AuditLog,
    ChannelConversation,
    Consent,
    Message,
    RetentionPolicy,
)
from app.domain.principal import PrincipalContext
from app.messages.crypto import AeadCrypto
from app.messages.service import MessageIngestionService

SUPPORTED_LOCALE_SET: frozenset[str] = frozenset({"ru-RU", "en-US", "en-GB"})
_AUDIT_REASONS: frozenset[str] = frozenset({"language_command", "proactive_ritual"})


class LocalizedSystemMessageError(ValueError):
    pass


def _require_non_empty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LocalizedSystemMessageError("invalid_localized_system_message")
    return value


def _require_bounded_str(value: object, name: str, min_len: int, max_len: int) -> str:
    text = _require_non_empty_str(value, name)
    if not min_len <= len(text) <= max_len:
        raise LocalizedSystemMessageError("invalid_localized_system_message")
    return text


class LocalizedSystemMessageService:
    """Writes finite rendered text only after the accepted chat authority gates."""

    def __init__(self, session: AsyncSession, crypto: AeadCrypto) -> None:
        self.session, self.crypto = session, crypto

    async def persist(
        self,
        principal: PrincipalContext,
        mapping: ChannelConversation,
        *,
        locale_code: str,
        text: str,
        idempotency_key: str,
        audit_reason: str = "language_command",
    ) -> UUID:
        """Public transaction-owning persistence entrypoint."""
        self._validate(principal, mapping, locale_code, text, idempotency_key, audit_reason)
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            return await self.persist_in_transaction(
                principal,
                mapping,
                locale_code=locale_code,
                text=text,
                idempotency_key=idempotency_key,
                audit_reason=audit_reason,
            )

    async def persist_in_transaction(
        self,
        principal: PrincipalContext,
        mapping: ChannelConversation,
        *,
        locale_code: str,
        text: str,
        idempotency_key: str,
        audit_reason: str = "language_command",
    ) -> UUID:
        """Persist inside a caller-owned active transaction and tenant context."""
        self._validate(principal, mapping, locale_code, text, idempotency_key, audit_reason)
        existing = await self.session.scalar(
            select(Message)
            .where(
                Message.tenant_id == principal.tenant_id,
                Message.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.role != "assistant"
                or existing.user_id != principal.user_id
                or existing.character_id != mapping.character_id
                or existing.conversation_id != mapping.conversation_id
                or existing.scope != mapping.scope
                or existing.scenario_id != mapping.scenario_id
                or existing.language_code != ({"ru"} if locale_code == "ru-RU" else {"en"})
            ):
                raise LocalizedSystemMessageError("idempotency_conflict")
            return existing.id
        owned = await self.session.scalar(
            select(ChannelConversation)
            .where(
                ChannelConversation.tenant_id == principal.tenant_id,
                ChannelConversation.id == mapping.id,
                ChannelConversation.user_id == principal.user_id,
                ChannelConversation.character_id == mapping.character_id,
                ChannelConversation.scope == mapping.scope,
                ChannelConversation.scenario_key == mapping.scenario_key,
            )
            .with_for_update()
        )
        consent = await self.session.scalar(
            select(Consent)
            .where(
                Consent.tenant_id == principal.tenant_id,
                Consent.user_id == principal.user_id,
                Consent.purpose == "chat_history",
                Consent.revoked_at.is_(None),
            )
            .order_by(Consent.granted_at.desc(), Consent.id.desc())
            .limit(1)
            .with_for_update()
        )
        if owned is None or consent is None:
            raise LocalizedSystemMessageError("localized_message_denied")
        retention = await self.session.scalar(
            select(RetentionPolicy)
            .where(RetentionPolicy.tenant_id == principal.tenant_id)
            .order_by(RetentionPolicy.created_at.desc())
            .limit(1)
        )
        message_id = uuid4()
        envelope = self.crypto.encrypt(
            text,
            MessageIngestionService._aad_for(
                principal.tenant_id,
                principal.user_id,
                owned.conversation_id,
                message_id,
            ),
        )
        self.session.add(
            Message(
                id=message_id,
                tenant_id=principal.tenant_id,
                conversation_id=owned.conversation_id,
                user_id=principal.user_id,
                character_id=owned.character_id,
                scope=owned.scope,
                scenario_id=owned.scenario_id,
                idempotency_key=idempotency_key,
                role="assistant",
                content_ciphertext=envelope.ciphertext,
                content_nonce=envelope.nonce,
                content_key_version=envelope.key_version,
                consent_id=consent.id,
                retention_policy_id=None if retention is None else retention.id,
                occurred_at=datetime.now(UTC),
                language_code="ru" if locale_code == "ru-RU" else "en",
            )
        )
        self.session.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_type="internal",
                actor_id=principal.actor_id,
                action="localized_system_message_committed",
                object_id=message_id,
                metadata_json={"locale_code": locale_code, "reason": audit_reason},
            )
        )
        await self.session.flush()
        return message_id

    @staticmethod
    def _validate(
        principal: PrincipalContext,
        mapping: ChannelConversation,
        locale_code: str,
        text: str,
        idempotency_key: str,
        audit_reason: str,
    ) -> None:
        # Type-before-membership: validate str before set containment.
        if not isinstance(principal, PrincipalContext):
            raise LocalizedSystemMessageError("invalid_localized_system_message")
        if not isinstance(mapping, ChannelConversation):
            raise LocalizedSystemMessageError("invalid_localized_system_message")
        _require_non_empty_str(locale_code, "locale_code")
        if locale_code not in SUPPORTED_LOCALE_SET:
            raise LocalizedSystemMessageError("invalid_localized_system_message")
        _require_bounded_str(text, "text", 1, 4096)
        _require_bounded_str(idempotency_key, "idempotency_key", 1, 255)
        if not isinstance(audit_reason, str) or audit_reason not in _AUDIT_REASONS:
            raise LocalizedSystemMessageError("invalid_localized_system_message")
