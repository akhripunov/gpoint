"""Authoritative, bounded shared-history continuity over source-linked memories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    Consent,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    SharedHistoryEpisode,
)
from app.messages.protected_content import decrypt_text_from_storage, memory_value_aad

MAX_SHARED_HISTORY = 10


class SharedHistoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SharedHistoryEvidence:
    memory_id: UUID
    memory_version: int
    value: str
    evidence_state: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SharedHistoryContext:
    tenant_id: UUID
    user_id: UUID
    character_id: UUID
    scope: object
    scenario_id: UUID | None
    relationship_id: UUID

    def __post_init__(self) -> None:
        from app.db.models import Scope

        if (
            not all(
                isinstance(value, UUID)
                for value in (
                    self.tenant_id, self.user_id, self.character_id, self.relationship_id
                )
            )
            or not isinstance(self.scope, Scope)
            or (self.scope == Scope.real_world and self.scenario_id is not None)
            or (self.scope == Scope.roleplay and not isinstance(self.scenario_id, UUID))
        ):
            raise SharedHistoryError("shared_history_invalid")


class SharedHistoryService:
    """Keeps a small, user-scoped relationship timeline without duplicating text."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def confirm(self, context: SharedHistoryContext, memory_id: UUID) -> SharedHistoryEpisode:
        async with self.session.begin():
            await set_tenant_context(self.session, str(context.tenant_id))
            return await self.confirm_in_transaction(context, memory_id)

    async def confirm_in_transaction(
        self, context: object, memory_id: object
    ) -> SharedHistoryEpisode:
        if not isinstance(context, SharedHistoryContext) or not isinstance(memory_id, UUID):
            raise SharedHistoryError("shared_history_invalid")
        memory = await self.session.scalar(
            select(Memory)
            .where(
                Memory.tenant_id == context.tenant_id,
                Memory.id == memory_id,
                Memory.relationship_id == context.relationship_id,
                Memory.user_id == context.user_id,
                Memory.character_id == context.character_id,
                Memory.scope == context.scope,
                Memory.scenario_id == context.scenario_id,
                Memory.lifecycle_state == LifecycleState.active,
                Memory.evidence_state.in_([EvidenceState.explicit, EvidenceState.confirmed]),
                Memory.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if memory is None:
            raise SharedHistoryError("shared_history_memory_ineligible")
        control = await self.session.scalar(
            select(MemoryControlState)
            .where(
                MemoryControlState.tenant_id == context.tenant_id,
                MemoryControlState.relationship_id == context.relationship_id,
                MemoryControlState.user_id == context.user_id,
                MemoryControlState.character_id == context.character_id,
                MemoryControlState.scope == context.scope,
                MemoryControlState.scenario_id == context.scenario_id,
            )
            .with_for_update()
        )
        if control is None or control.deletion_epoch != memory.created_epoch:
            raise SharedHistoryError("shared_history_stale")
        if not await self._active_memory_consent(context):
            raise SharedHistoryError("shared_history_consent_denied")
        existing = await self.session.scalar(
            select(SharedHistoryEpisode)
            .where(
                SharedHistoryEpisode.tenant_id == context.tenant_id,
                SharedHistoryEpisode.relationship_id == context.relationship_id,
                SharedHistoryEpisode.user_id == context.user_id,
                SharedHistoryEpisode.character_id == context.character_id,
                SharedHistoryEpisode.scope == context.scope,
                SharedHistoryEpisode.scenario_id == context.scenario_id,
                SharedHistoryEpisode.memory_id == memory.id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.state == "active":
                return existing
            existing.state = "active"
            existing.retired_at = None
            existing.confirmed_at = datetime.now(UTC)
            existing.creation_epoch = control.deletion_epoch
            await self.session.flush()
            from app.memory.snapshots import enqueue_relationship_summary_refresh

            await enqueue_relationship_summary_refresh(
                self.session, tenant_id=context.tenant_id,
                relationship_id=context.relationship_id,
                creation_epoch=control.deletion_epoch,
            )
            return existing
        active = list(
            (
                await self.session.scalars(
                    select(SharedHistoryEpisode)
                    .where(
                        SharedHistoryEpisode.tenant_id == context.tenant_id,
                        SharedHistoryEpisode.relationship_id == context.relationship_id,
                        SharedHistoryEpisode.user_id == context.user_id,
                        SharedHistoryEpisode.character_id == context.character_id,
                        SharedHistoryEpisode.scope == context.scope,
                        SharedHistoryEpisode.scenario_id == context.scenario_id,
                        SharedHistoryEpisode.state == "active",
                    )
                    .order_by(SharedHistoryEpisode.confirmed_at, SharedHistoryEpisode.id)
                    .with_for_update()
                )
            ).all()
        )
        # Keep at most ten active episodes.  The oldest remains provenance but
        # becomes historical before the new row is made visible.
        while len(active) >= MAX_SHARED_HISTORY:
            row = active.pop(0)
            row.state = "historical"
            row.retired_at = datetime.now(UTC)
        source_id = await self.session.scalar(
            select(MemorySource.message_id)
            .where(
                MemorySource.tenant_id == context.tenant_id,
                MemorySource.memory_id == memory.id,
            )
            .order_by(MemorySource.id)
            .limit(1)
        )
        episode = SharedHistoryEpisode(
            tenant_id=context.tenant_id,
            relationship_id=context.relationship_id,
            user_id=context.user_id,
            character_id=context.character_id,
            scope=context.scope,
            scenario_id=context.scenario_id,
            memory_id=memory.id,
            source_message_id=source_id if isinstance(source_id, UUID) else None,
            state="active",
            confirmed_at=datetime.now(UTC),
            creation_epoch=control.deletion_epoch,
        )
        self.session.add(episode)
        await self.session.flush()
        from app.memory.snapshots import enqueue_relationship_summary_refresh

        await enqueue_relationship_summary_refresh(
            self.session, tenant_id=context.tenant_id,
            relationship_id=context.relationship_id,
            creation_epoch=control.deletion_epoch,
        )
        return episode

    async def retire_memory_in_transaction(
        self, context: object, memory_id: object, *, deleted: bool = False
    ) -> int:
        if not isinstance(context, SharedHistoryContext) or not isinstance(memory_id, UUID):
            raise SharedHistoryError("shared_history_invalid")
        rows = list(
            (
                await self.session.scalars(
                    select(SharedHistoryEpisode)
                    .where(
                        SharedHistoryEpisode.tenant_id == context.tenant_id,
                        SharedHistoryEpisode.relationship_id == context.relationship_id,
                        SharedHistoryEpisode.user_id == context.user_id,
                        SharedHistoryEpisode.character_id == context.character_id,
                        SharedHistoryEpisode.scope == context.scope,
                        SharedHistoryEpisode.scenario_id == context.scenario_id,
                        SharedHistoryEpisode.memory_id == memory_id,
                        SharedHistoryEpisode.state == "active",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for row in rows:
            row.state = "deleted" if deleted else "historical"
            row.retired_at = datetime.now(UTC)
        await self.session.flush()
        return len(rows)

    async def load_in_transaction(self, context: object) -> tuple[SharedHistoryEvidence, ...]:
        if not isinstance(context, SharedHistoryContext):
            return ()
        if not await self._active_memory_consent(context):
            return ()
        rows = list(
            (
                await self.session.execute(
                    select(SharedHistoryEpisode, Memory)
                    .join(
                        Memory,
                        and_(
                            Memory.tenant_id == SharedHistoryEpisode.tenant_id,
                            Memory.id == SharedHistoryEpisode.memory_id,
                            Memory.relationship_id == SharedHistoryEpisode.relationship_id,
                            Memory.user_id == SharedHistoryEpisode.user_id,
                            Memory.character_id == SharedHistoryEpisode.character_id,
                            Memory.scope == SharedHistoryEpisode.scope,
                            Memory.scenario_key == SharedHistoryEpisode.scenario_key,
                        ),
                    )
                    .join(
                        MemoryControlState,
                        and_(
                            MemoryControlState.tenant_id == Memory.tenant_id,
                            MemoryControlState.relationship_id == Memory.relationship_id,
                            MemoryControlState.user_id == Memory.user_id,
                            MemoryControlState.character_id == Memory.character_id,
                            MemoryControlState.scope == Memory.scope,
                            MemoryControlState.scenario_key == Memory.scenario_key,
                            MemoryControlState.deletion_epoch
                            == SharedHistoryEpisode.creation_epoch,
                        ),
                    )
                    .where(
                        SharedHistoryEpisode.tenant_id == context.tenant_id,
                        SharedHistoryEpisode.relationship_id == context.relationship_id,
                        SharedHistoryEpisode.user_id == context.user_id,
                        SharedHistoryEpisode.character_id == context.character_id,
                        SharedHistoryEpisode.scope == context.scope,
                        SharedHistoryEpisode.scenario_id == context.scenario_id,
                        SharedHistoryEpisode.state == "active",
                        Memory.lifecycle_state == LifecycleState.active,
                        Memory.evidence_state.in_(
                            [EvidenceState.explicit, EvidenceState.confirmed]
                        ),
                        Memory.deleted_at.is_(None),
                        Memory.value_text.is_not(None),
                    )
                    .order_by(SharedHistoryEpisode.confirmed_at.desc(), SharedHistoryEpisode.id)
                    .limit(MAX_SHARED_HISTORY)
                )
            ).all()
        )
        evidence: list[SharedHistoryEvidence] = []
        for _, memory in rows:
            value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory))
            if value:
                evidence.append(
                    SharedHistoryEvidence(
                        memory.id,
                        memory.version,
                        value[:500],
                        memory.evidence_state.value,
                        memory.observed_at,
                    )
                )
        return tuple(evidence)

    async def verify_in_transaction(self, context: object, evidence: object) -> bool:
        if not isinstance(context, SharedHistoryContext) or not isinstance(evidence, tuple):
            return False
        expected = {item.memory_id for item in evidence if isinstance(item, SharedHistoryEvidence)}
        if len(expected) != len(evidence):
            return False
        current = await self.load_in_transaction(context)
        return expected.issubset({item.memory_id for item in current})

    async def _active_memory_consent(self, context: SharedHistoryContext) -> bool:
        return (
            await self.session.scalar(
                select(Consent.id)
                .where(
                    Consent.tenant_id == context.tenant_id,
                    Consent.user_id == context.user_id,
                    Consent.purpose == "long_term_memory",
                    Consent.revoked_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )


__all__ = [
    "MAX_SHARED_HISTORY",
    "SharedHistoryError",
    "SharedHistoryEvidence",
    "SharedHistoryContext",
    "SharedHistoryService",
]
