from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    AuditLog,
    Consent,
    Conversation,
    ExplicitCommandReceipt,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    Message,
    Scope,
    ShadowMemoryCandidate,
    SuppressionRule,
)
from app.domain.principal import PrincipalContext
from app.domain.relationships import resolve_relationship
from app.memory.shared_history import SharedHistoryContext, SharedHistoryService
from app.messages.crypto import canonical_tagged_fields, derive_hmac_subkey
from app.messages.protected_content import (
    content_aad,
    decrypt_text_from_storage,
    encrypt_text_for_storage,
    memory_value_aad,
)
from app.messages.service import DomainError

EXPLICIT_CONFIDENCE = 1.0
COMMAND_FINGERPRINT_VERSION = b"gptgirls/explicit-command-fingerprint/v1"
SUPPRESSION_HMAC_DOMAIN = b"gptgirls/memory-suppression/v1"
COMMAND_HMAC_DOMAIN = b"gptgirls/explicit-command-fingerprint/v1"


def _valid_command_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= maximum and "\x00" not in value


@dataclass(frozen=True, slots=True)
class Remember:
    command_id: str
    character_id: UUID
    scope: Scope
    scenario_id: UUID | None
    source_message_id: UUID
    slot_key: str
    predicate: str
    value_text: str
    confirm_remember_again: bool = False

    def __post_init__(self) -> None:
        if not all(
            _valid_command_text(v, 128) for v in (self.command_id, self.slot_key, self.predicate)
        ) or not _valid_command_text(self.value_text, 1024):
            raise DomainError("invalid_memory_command")


@dataclass(frozen=True, slots=True)
class Correct:
    command_id: str
    memory_id: UUID
    source_message_id: UUID
    predicate: str
    value_text: str

    def __post_init__(self) -> None:
        if (
            not _valid_command_text(self.command_id, 128)
            or not isinstance(self.memory_id, UUID)
            or not isinstance(self.source_message_id, UUID)
            or not _valid_command_text(self.predicate, 128)
            or not _valid_command_text(self.value_text, 1024)
        ):
            raise DomainError("invalid_memory_command")


@dataclass(frozen=True, slots=True)
class Forget:
    command_id: str
    memory_id: UUID

    def __post_init__(self) -> None:
        if not _valid_command_text(self.command_id, 128) or not isinstance(self.memory_id, UUID):
            raise DomainError("invalid_memory_command")


class ExplicitMemoryService:
    def __init__(self, session: AsyncSession, *, hmac_key: bytes, hmac_key_version: str) -> None:
        if (
            not isinstance(hmac_key, bytes)
            or len(hmac_key) < 32
            or not isinstance(hmac_key_version, str)
            or not hmac_key_version
        ):
            raise DomainError("invalid_memory_hmac_key")
        self.session, self.hmac_key_version = session, hmac_key_version
        self._suppression_hmac_key = derive_hmac_subkey(hmac_key, SUPPRESSION_HMAC_DOMAIN)
        self._command_hmac_key = derive_hmac_subkey(hmac_key, COMMAND_HMAC_DOMAIN)

    async def remember(self, principal: PrincipalContext, command: Remember) -> UUID:
        if not isinstance(principal, PrincipalContext) or not isinstance(command, Remember):
            raise DomainError("invalid_memory_command")
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            receipt = await self._command_receipt(principal, command, "remember")
            if receipt is not None:
                return self._receipt_result(receipt)
            consent = await self._current_memory_consent(principal)
            if consent is None:
                raise DomainError("long_term_memory_consent_required")
            relationship = await resolve_relationship(
                self.session, principal, command.character_id, command.scope, command.scenario_id
            )
            source = await self.session.scalar(
                select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
                    Message.tenant_id == principal.tenant_id,
                    Message.id == command.source_message_id,
                    Message.user_id == principal.user_id,
                    Message.character_id == command.character_id,
                    Message.scope == command.scope,
                    Message.scenario_id == command.scenario_id,
                    Message.role == "user",
                    Conversation.relationship_id == relationship.id,
                )
            )
            if source is None:
                raise DomainError("memory_source_not_permitted")
            control = await self._control(principal, command)
            slot_hmac, value_hmac = self._digest(command.slot_key), self._digest(command.value_text)
            suppression = await self.session.scalar(
                select(SuppressionRule)
                .where(
                    SuppressionRule.tenant_id == principal.tenant_id,
                    SuppressionRule.relationship_id == control.relationship_id,
                    SuppressionRule.user_id == principal.user_id,
                    SuppressionRule.character_id == command.character_id,
                    SuppressionRule.scope == command.scope,
                    SuppressionRule.scenario_id == command.scenario_id,
                    SuppressionRule.slot_hmac == slot_hmac,
                    SuppressionRule.value_hmac == value_hmac,
                    SuppressionRule.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if suppression is not None:
                if not command.confirm_remember_again:
                    raise DomainError("memory_suppressed")
                suppression.revoked_at = datetime.now(UTC)
                self.session.add(
                    AuditLog(
                        tenant_id=principal.tenant_id,
                        actor_type=principal.channel,
                        actor_id=principal.actor_id,
                        action="suppression_revoked",
                        object_id=suppression.id,
                        metadata_json={"code": "remember_again"},
                    )
                )
            existing = await self.session.scalar(
                select(Memory)
                .where(
                    Memory.tenant_id == principal.tenant_id,
                    Memory.relationship_id == control.relationship_id,
                    Memory.user_id == principal.user_id,
                    Memory.character_id == command.character_id,
                    Memory.scope == command.scope,
                    Memory.scenario_id == command.scenario_id,
                    Memory.slot_key == command.slot_key,
                    Memory.lifecycle_state == LifecycleState.active,
                )
                .with_for_update()
            )
            if existing is not None:
                await self._store_receipt(principal, command.command_id, existing.id, "completed")
                return existing.id
            memory_id = uuid4()
            memory = Memory(
                id=memory_id,
                tenant_id=principal.tenant_id,
                relationship_id=control.relationship_id,
                user_id=principal.user_id,
                character_id=command.character_id,
                kind="semantic_fact",
                scope=command.scope,
                scenario_id=command.scenario_id,
                slot_key=command.slot_key,
                predicate=command.predicate,
                value_text=encrypt_text_for_storage(
                    command.value_text,
                    content_aad(
                        entity="memories",
                        tenant_id=principal.tenant_id,
                        user_id=principal.user_id,
                        character_id=command.character_id,
                        scope=command.scope,
                        scenario_id=command.scenario_id,
                        record_id=memory_id,
                        field="value_text",
                    ),
                ),
                observed_at=datetime.now(UTC),
                evidence_state="explicit",
                lifecycle_state="active",
                confidence=EXPLICIT_CONFIDENCE,
                importance=0.5,
                consent_id=consent.id,
                created_epoch=control.deletion_epoch,
            )
            self.session.add(memory)
            self.session.add(
                MemorySource(
                    tenant_id=principal.tenant_id,
                    memory_id=memory.id,
                    message_id=source.id,
                    user_id=principal.user_id,
                    character_id=command.character_id,
                    scope=command.scope,
                    scenario_key=source.scenario_key,
                    relation="supports",
                )
            )
            self.session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor_type=principal.channel,
                    actor_id=principal.actor_id,
                    action="memory_remembered",
                    object_id=memory.id,
                    metadata_json={"code": "explicit"},
                )
            )
            await self.session.flush()
            await self._store_receipt(principal, command.command_id, memory.id, "completed")
            return memory.id

    async def correct(self, principal: PrincipalContext, command: Correct) -> UUID:
        if not isinstance(principal, PrincipalContext) or not isinstance(command, Correct):
            raise DomainError("invalid_memory_command")
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            receipt = await self._command_receipt(principal, command, "correct")
            if receipt is not None:
                return self._receipt_result(receipt)
            # This lock is the consent linearization point for a correction write.
            # A concurrent revocation either commits first (deny) or waits until this
            # committed correction finishes; no replacement is written post-revocation.
            consent = await self._current_memory_consent(principal)
            if consent is None:
                raise DomainError("long_term_memory_consent_required")
            old = await self.session.scalar(
                select(Memory)
                .where(
                    Memory.id == command.memory_id,
                    Memory.tenant_id == principal.tenant_id,
                    Memory.user_id == principal.user_id,
                    Memory.lifecycle_state == LifecycleState.active,
                )
                .with_for_update()
            )
            if old is None:
                raise DomainError("memory_target_unavailable")
            source = await self.session.scalar(
                select(Message).where(
                    Message.id == command.source_message_id,
                    Message.tenant_id == principal.tenant_id,
                    Message.user_id == principal.user_id,
                    Message.character_id == old.character_id,
                    Message.scope == old.scope,
                    Message.scenario_id == old.scenario_id,
                    Message.role == "user",
                )
            )
            if (
                source is None
                or not isinstance(command.predicate, str)
                or not isinstance(command.value_text, str)
            ):
                raise DomainError("memory_source_not_permitted")
            control = await self._control(principal, old)
            now = datetime.now(UTC)
            old.lifecycle_state, old.valid_until = LifecycleState.historical, now
            await SharedHistoryService(self.session).retire_memory_in_transaction(
                SharedHistoryContext(
                    principal.tenant_id,
                    principal.user_id,
                    old.character_id,
                    old.scope,
                    old.scenario_id,
                    old.relationship_id,
                ),
                old.id,
            )
            replacement_id = uuid4()
            replacement = Memory(
                id=replacement_id,
                tenant_id=old.tenant_id,
                relationship_id=old.relationship_id,
                user_id=old.user_id,
                character_id=old.character_id,
                kind="semantic_fact",
                scope=old.scope,
                scenario_id=old.scenario_id,
                slot_key=old.slot_key,
                predicate=command.predicate,
                value_text=encrypt_text_for_storage(
                    command.value_text,
                    content_aad(
                        entity="memories",
                        tenant_id=old.tenant_id,
                        user_id=old.user_id,
                        character_id=old.character_id,
                        scope=old.scope,
                        scenario_id=old.scenario_id,
                        record_id=replacement_id,
                        field="value_text",
                    ),
                ),
                observed_at=now,
                evidence_state="explicit",
                lifecycle_state="active",
                confidence=EXPLICIT_CONFIDENCE,
                importance=float(old.importance),
                consent_id=consent.id,
                retention_policy_id=old.retention_policy_id,
                supersedes_id=old.id,
                version=old.version + 1,
                created_epoch=control.deletion_epoch,
            )
            self.session.add(replacement)
            self.session.add(
                MemorySource(
                    tenant_id=old.tenant_id,
                    memory_id=replacement.id,
                    message_id=source.id,
                    user_id=old.user_id,
                    character_id=old.character_id,
                    scope=old.scope,
                    scenario_key=source.scenario_key,
                    relation="corrects",
                )
            )
            self.session.add(
                AuditLog(
                    tenant_id=old.tenant_id,
                    actor_type=principal.channel,
                    actor_id=principal.actor_id,
                    action="memory_corrected",
                    object_id=replacement.id,
                    metadata_json={"code": "explicit"},
                )
            )
            await self.session.flush()
            await self._store_receipt(principal, command.command_id, replacement.id, "completed")
            return replacement.id

    async def forget(self, principal: PrincipalContext, command: Forget) -> bool:
        if not isinstance(principal, PrincipalContext) or not isinstance(command, Forget):
            raise DomainError("invalid_memory_command")
        async with self.session.begin():
            await set_tenant_context(self.session, str(principal.tenant_id))
            receipt = await self._command_receipt(principal, command, "forget")
            if receipt is not None:
                return receipt.result_status == "already_forgotten"
            memory = await self.session.scalar(
                select(Memory)
                .where(
                    Memory.id == command.memory_id,
                    Memory.tenant_id == principal.tenant_id,
                    Memory.user_id == principal.user_id,
                )
                .with_for_update()
            )
            if memory is None:
                raise DomainError("memory_target_unavailable")
            if memory.lifecycle_state == LifecycleState.deleted:
                await self._store_receipt(
                    principal, command.command_id, memory.id, "already_forgotten"
                )
                return True
            control = await self._control(principal, memory)
            now = datetime.now(UTC)
            old_value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory)) or ""
            memory.lifecycle_state, memory.deleted_at, memory.valid_until = (
                LifecycleState.deleted,
                now,
                now,
            )
            memory.value_text = None
            memory.value_json = None
            memory.search_text = None
            memory.embedding = None
            memory.embedding_model = None
            memory.embedded_at = None
            source_message_ids = select(MemorySource.message_id).where(
                MemorySource.tenant_id == memory.tenant_id,
                MemorySource.memory_id == memory.id,
            )
            await self.session.execute(
                update(ShadowMemoryCandidate)
                .where(
                    ShadowMemoryCandidate.tenant_id == memory.tenant_id,
                    ShadowMemoryCandidate.relationship_id == memory.relationship_id,
                    ShadowMemoryCandidate.source_message_id.in_(source_message_ids),
                    ShadowMemoryCandidate.user_id == memory.user_id,
                    ShadowMemoryCandidate.character_id == memory.character_id,
                    ShadowMemoryCandidate.status.in_(("shadow", "promoting")),
                )
                .values(
                    status="invalidated",
                    invalidated_at=now,
                    value_ciphertext=None,
                    value_nonce=None,
                    value_key_version=None,
                )
            )
            await SharedHistoryService(self.session).retire_memory_in_transaction(
                SharedHistoryContext(
                    principal.tenant_id,
                    principal.user_id,
                    memory.character_id,
                    memory.scope,
                    memory.scenario_id,
                    memory.relationship_id,
                ),
                memory.id,
                deleted=True,
            )
            control.deletion_epoch += 1
            self.session.add(
                SuppressionRule(
                    tenant_id=memory.tenant_id,
                    relationship_id=memory.relationship_id,
                    user_id=memory.user_id,
                    character_id=memory.character_id,
                    scope=memory.scope,
                    scenario_id=memory.scenario_id,
                    rule_type="slot_value",
                    slot_hmac=self._digest(memory.slot_key or ""),
                    value_hmac=self._digest(old_value),
                    key_version=self.hmac_key_version,
                    reason="explicit_forget",
                )
            )
            self.session.add(
                AuditLog(
                    tenant_id=memory.tenant_id,
                    actor_type=principal.channel,
                    actor_id=principal.actor_id,
                    action="memory_forgotten",
                    object_id=memory.id,
                    metadata_json={"code": "explicit"},
                )
            )
            await self.session.flush()
            await self._store_receipt(principal, command.command_id, memory.id, "completed")
            return False

    async def _control(self, principal: PrincipalContext, command: object) -> MemoryControlState:
        character_id = command.character_id
        scope = command.scope
        scenario_id = command.scenario_id
        relationship_id = getattr(command, "relationship_id", None)
        if not isinstance(relationship_id, UUID):
            relationship = await resolve_relationship(
                self.session, principal, character_id, scope, scenario_id
            )
            relationship_id = relationship.id
        control = await self.session.scalar(
            select(MemoryControlState)
            .where(
                MemoryControlState.tenant_id == principal.tenant_id,
                MemoryControlState.relationship_id == relationship_id,
                MemoryControlState.user_id == principal.user_id,
                MemoryControlState.character_id == character_id,
                MemoryControlState.scope == scope,
                MemoryControlState.scenario_id == scenario_id,
            )
            .with_for_update()
        )
        if control is None:
            control = MemoryControlState(
                tenant_id=principal.tenant_id,
                relationship_id=relationship_id,
                user_id=principal.user_id,
                character_id=character_id,
                scope=scope,
                scenario_id=scenario_id,
            )
            self.session.add(control)
            await self.session.flush()
        return control

    def _digest(self, value: str) -> str:
        return new(self._suppression_hmac_key, value.encode("utf-8"), sha256).hexdigest()

    async def _current_memory_consent(self, principal: PrincipalContext) -> Consent | None:
        return await self.session.scalar(
            select(Consent)
            .where(
                Consent.tenant_id == principal.tenant_id,
                Consent.user_id == principal.user_id,
                Consent.purpose == "long_term_memory",
                Consent.revoked_at.is_(None),
            )
            .order_by(Consent.granted_at.desc(), Consent.id.desc())
            .limit(1)
            .with_for_update()
        )

    async def _command_receipt(
        self, principal: PrincipalContext, command: Remember | Correct | Forget, operation: str
    ) -> ExplicitCommandReceipt | None:
        """Lock one durable tenant/user command key before any write-side transition."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{principal.tenant_id}:{principal.user_id}:{command.command_id}"},
        )
        fingerprint = self._command_fingerprint(operation, command)
        existing = await self.session.scalar(
            select(ExplicitCommandReceipt)
            .where(
                ExplicitCommandReceipt.tenant_id == principal.tenant_id,
                ExplicitCommandReceipt.user_id == principal.user_id,
                ExplicitCommandReceipt.command_id == command.command_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.operation != operation or existing.command_fingerprint != fingerprint:
                raise DomainError("idempotency_conflict")
            return existing
        receipt = ExplicitCommandReceipt(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            command_id=command.command_id,
            operation=operation,
            command_fingerprint=fingerprint,
            result_status="completed",
        )
        self.session.add(receipt)
        await self.session.flush()
        return None

    @staticmethod
    def _receipt_result(receipt: ExplicitCommandReceipt) -> UUID:
        if receipt.result_memory_id is None:
            raise DomainError("idempotency_receipt_invalid")
        return receipt.result_memory_id

    async def _store_receipt(
        self, principal: PrincipalContext, command_id: str, memory_id: UUID, status: str
    ) -> None:
        row = await self.session.scalar(
            select(ExplicitCommandReceipt)
            .where(
                ExplicitCommandReceipt.tenant_id == principal.tenant_id,
                ExplicitCommandReceipt.user_id == principal.user_id,
                ExplicitCommandReceipt.command_id == command_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DomainError("idempotency_receipt_invalid")
        row.result_memory_id, row.result_status = memory_id, status

    def _command_fingerprint(self, operation: str, command: Remember | Correct | Forget) -> str:
        if not isinstance(operation, str) or operation not in {"remember", "correct", "forget"}:
            raise DomainError("invalid_memory_command")
        fields: list[tuple[bytes, bytes]] = [
            (b"version", COMMAND_FINGERPRINT_VERSION),
            (b"operation", operation.encode("ascii")),
            (b"command_id_utf8", command.command_id.encode("utf-8")),
        ]
        if isinstance(command, Remember):
            fields.extend(
                (
                    (b"command_type", b"remember"),
                    (b"character_id_uuid", command.character_id.bytes),
                    (b"scope_enum", command.scope.value.encode("ascii")),
                    (b"scenario_id", self._optional_uuid_bytes(command.scenario_id)),
                    (b"source_message_id_uuid", command.source_message_id.bytes),
                    (b"slot_key_utf8", command.slot_key.encode("utf-8")),
                    (b"predicate_utf8", command.predicate.encode("utf-8")),
                    (b"value_text_utf8", command.value_text.encode("utf-8")),
                    (
                        b"confirm_remember_again_bool",
                        b"\x01" if command.confirm_remember_again else b"\x00",
                    ),
                )
            )
        elif isinstance(command, Correct):
            fields.extend(
                (
                    (b"command_type", b"correct"),
                    (b"memory_id_uuid", command.memory_id.bytes),
                    (b"source_message_id_uuid", command.source_message_id.bytes),
                    (b"predicate_utf8", command.predicate.encode("utf-8")),
                    (b"value_text_utf8", command.value_text.encode("utf-8")),
                )
            )
        elif isinstance(command, Forget):
            fields.extend(
                ((b"command_type", b"forget"), (b"memory_id_uuid", command.memory_id.bytes))
            )
        else:
            raise DomainError("invalid_memory_command")
        return new(
            self._command_hmac_key, canonical_tagged_fields(tuple(fields)), sha256
        ).hexdigest()

    @staticmethod
    def _optional_uuid_bytes(value: UUID | None) -> bytes:
        if value is None:
            return b"\x00"
        if not isinstance(value, UUID):
            raise DomainError("invalid_memory_command")
        return b"\x01" + value.bytes
