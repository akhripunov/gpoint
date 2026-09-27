"""Deterministic temporal truth and memory precedence policy.

Relevance ranking must only run over rows admitted by this policy. Plaintext
values are compared only inside the trusted process; slot identities for
multi-value facts are keyed digests and never contain the fact value.
"""

from __future__ import annotations

import enum
import hmac
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvidenceState, LifecycleState, Memory, MemorySource, Message
from app.messages.service import DomainError


class TemporalMode(str, enum.Enum):
    CURRENT = "current"
    HISTORICAL = "historical"


class PrecedenceOutcome(str, enum.Enum):
    ACCEPT = "accept"
    IDENTICAL = "identical"
    DENY = "denied"
    CONFLICT = "conflict"


EVIDENCE_PRECEDENCE = {
    EvidenceState.explicit: 4,
    EvidenceState.confirmed: 3,
    EvidenceState.inferred: 2,
    EvidenceState.disputed: 1,
}


def current_truth_filter(at: datetime):
    """Canonical SQL eligibility for settled current truth, before ranking."""
    return and_(
        Memory.lifecycle_state == LifecycleState.active,
        Memory.deleted_at.is_(None),
        Memory.evidence_state != EvidenceState.disputed,
        or_(Memory.valid_from.is_(None), Memory.valid_from <= at),
        or_(Memory.valid_until.is_(None), Memory.valid_until > at),
    )


def historical_truth_filter():
    """Canonical history eligibility; historical labels are never rewritten."""
    return and_(
        Memory.lifecycle_state == LifecycleState.historical,
        Memory.deleted_at.is_(None),
        Memory.evidence_state != EvidenceState.disputed,
    )

# Only slots with established single-current-value meaning belong here.
# Unknown slots are conservatively treated as set-valued.
SINGLETON_SLOTS = frozenset({
    "user.preferred_name", "user.locale", "user.timezone",
    "user.current_activity", "user.works_at", "user.lives_in",
})


def slot_family(slot_identity: str) -> str:
    """Strip the opaque value suffix while preserving the public slot family."""
    return slot_identity.split("#v1:", 1)[0]


@dataclass(frozen=True, slots=True)
class PrecedenceDecision:
    outcome: PrecedenceOutcome
    reason_code: str


def build_slot_identity(slot_key: str, value: str, *, hmac_key: bytes) -> str:
    """Return singleton slot unchanged; key set-valued slot by opaque value digest."""
    if not isinstance(slot_key, str) or not 1 <= len(slot_key) <= 255:
        raise DomainError("memory_slot_invalid")
    if not isinstance(value, str) or not value or not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
        raise DomainError("memory_slot_identity_invalid")
    if slot_key in SINGLETON_SLOTS:
        return slot_key
    identity_key = hmac.new(hmac_key, b"agata/memory-slot/identity-key/v1", sha256).digest()
    digest = hmac.new(identity_key, b"agata/memory-slot/value/v1\x00" + value.strip().casefold().encode(), sha256).hexdigest()
    identity = f"{slot_key}#v1:{digest}"
    if len(identity) > 255:
        raise DomainError("memory_slot_identity_invalid")
    return identity


def effective_at(memory: Memory, at: datetime) -> bool:
    if at.tzinfo is None or at.utcoffset() is None:
        return False
    return (memory.valid_from is None or memory.valid_from <= at) and (
        memory.valid_until is None or memory.valid_until > at
    )


def resolve_as_of(memory: Memory, at: datetime) -> tuple[bool, str]:
    """Resolve only with explicit interval evidence; never invent boundaries."""
    if at.tzinfo is None or at.utcoffset() is None:
        return False, "invalid_target_time"
    if memory.valid_from is None or memory.valid_until is None:
        return False, "temporal_boundary_unknown"
    if effective_at(memory, at):
        return True, "interval_confirmed"
    return False, "outside_validity_interval"


def evidence_rank(value: EvidenceState | str) -> int:
    try:
        return EVIDENCE_PRECEDENCE[EvidenceState(value)]
    except (ValueError, KeyError):
        return 0


class MemoryPrecedenceService:
    """Canonical slot locking, expiry reconciliation, and revision-chain policy."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_slot(
        self, *, tenant_id: UUID, relationship_id: UUID, user_id: UUID,
        character_id: UUID, scope: str, scenario_id: UUID | None, slot_key: str,
    ) -> None:
        identity = ":".join((
            str(tenant_id), str(relationship_id), str(user_id), str(character_id),
            str(getattr(scope, "value", scope)), str(scenario_id or "real"), slot_key,
        ))
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"memory-slot:v1:{identity}"}
        )

    async def current_for_slot(
        self, *, tenant_id: UUID, relationship_id: UUID, user_id: UUID,
        character_id: UUID, scope: object, scenario_id: UUID | None,
        slot_key: str, at: datetime,
    ) -> Memory | None:
        """Lock and reconcile exactly one active slot row; expired rows become history."""
        await self.lock_slot(
            tenant_id=tenant_id, relationship_id=relationship_id, user_id=user_id,
            character_id=character_id, scope=scope, scenario_id=scenario_id,
            slot_key=slot_key,
        )
        rows = list((await self.session.scalars(
            select(Memory).where(
                Memory.tenant_id == tenant_id,
                Memory.relationship_id == relationship_id,
                Memory.user_id == user_id,
                Memory.character_id == character_id,
                Memory.scope == scope,
                Memory.scenario_id == scenario_id,
                Memory.slot_key == slot_key,
                Memory.kind.in_(["semantic_fact", "episodic"]),
                Memory.lifecycle_state == LifecycleState.active,
                Memory.deleted_at.is_(None),
            ).order_by(Memory.version.desc(), Memory.observed_at.desc(), Memory.id).with_for_update()
        )).all())
        current = rows[0] if rows else None
        if current is not None and not effective_at(current, at):
            if current.valid_from is not None and current.valid_from > at:
                current.lifecycle_state = LifecycleState.pending
                return None
            current.lifecycle_state = LifecycleState.historical
            current.updated_at = at
            return current
        return current

    async def resolve_current(self, **slot: object) -> Memory | None:
        """Canonical per-slot current resolver, including expired-row reconciliation."""
        return await self.current_for_slot(**slot)

    @staticmethod
    def compare_candidate(
        current: Memory | None, *, candidate_value: str, current_value: str | None,
        candidate_evidence: EvidenceState | str, candidate_claimed_at: datetime,
        current_claimed_at: datetime | None = None,
    ) -> PrecedenceDecision:
        try:
            candidate_state = EvidenceState(candidate_evidence)
        except ValueError:
            return PrecedenceDecision(PrecedenceOutcome.DENY, "invalid_evidence")
        if candidate_state == EvidenceState.disputed:
            return PrecedenceDecision(PrecedenceOutcome.CONFLICT, "disputed_candidate")
        if current is None:
            return PrecedenceDecision(PrecedenceOutcome.ACCEPT, "empty_or_expired_slot")
        if current.evidence_state == EvidenceState.disputed and candidate_state not in {
            EvidenceState.explicit, EvidenceState.confirmed,
        }:
            return PrecedenceDecision(PrecedenceOutcome.CONFLICT, "unresolved_disputed_slot")
        if current.evidence_state == EvidenceState.disputed:
            return PrecedenceDecision(PrecedenceOutcome.ACCEPT, "dispute_resolved")
        if current_value is not None and candidate_value.strip().casefold() == current_value.strip().casefold():
            return PrecedenceDecision(PrecedenceOutcome.IDENTICAL, "same_semantic_value")
        candidate_rank = evidence_rank(candidate_state)
        current_rank = evidence_rank(current.evidence_state)
        if candidate_rank < current_rank:
            return PrecedenceDecision(PrecedenceOutcome.CONFLICT, "weaker_evidence")
        current_time = current_claimed_at or current.observed_at
        if candidate_rank == current_rank and candidate_claimed_at < current_time:
            return PrecedenceDecision(PrecedenceOutcome.CONFLICT, "out_of_order_evidence")
        if candidate_rank == current_rank and candidate_claimed_at == current_time:
            return PrecedenceDecision(PrecedenceOutcome.CONFLICT, "equal_authority_conflict")
        return PrecedenceDecision(PrecedenceOutcome.ACCEPT, "authoritative_transition")

    async def latest_source_time(self, memory: Memory) -> datetime:
        """Return source event time for ordering claims, not evidence-arrival time."""
        value = await self.session.scalar(
            select(func.max(Message.occurred_at))
            .join(
                MemorySource,
                (MemorySource.tenant_id == Message.tenant_id)
                & (MemorySource.message_id == Message.id),
            )
            .where(
                MemorySource.tenant_id == memory.tenant_id,
                MemorySource.memory_id == memory.id,
                MemorySource.relation.in_(("supports", "corrects")),
                Message.deleted_at.is_(None),
            )
        )
        return value if isinstance(value, datetime) else memory.observed_at

    async def supersede(
        self, old: Memory, new: Memory, *, transition_at: datetime,
    ) -> None:
        """Append new → old, enforce same relationship/slot, monotonic version, no cycles."""
        if (
            old.id == new.id or old.tenant_id != new.tenant_id
            or old.relationship_id != new.relationship_id or old.user_id != new.user_id
            or old.character_id != new.character_id or old.scope != new.scope
            or old.scenario_id != new.scenario_id or old.slot_key != new.slot_key
        ):
            raise DomainError("memory_supersession_scope_mismatch")
        seen: set[UUID] = set()
        node: Memory | None = old
        while node is not None:
            if node.id in seen or node.id == new.id:
                raise DomainError("memory_supersession_cycle")
            seen.add(node.id)
            if node.supersedes_id is None:
                break
            node = await self.session.scalar(select(Memory).where(
                Memory.tenant_id == old.tenant_id,
                Memory.relationship_id == old.relationship_id,
                Memory.id == node.supersedes_id,
            ))
            if node is None:
                raise DomainError("memory_supersession_chain_invalid")
        latest_version = await self.session.scalar(select(Memory.version).where(
            Memory.tenant_id == old.tenant_id,
            Memory.relationship_id == old.relationship_id,
            Memory.user_id == old.user_id,
            Memory.character_id == old.character_id,
            Memory.scope == old.scope,
            Memory.scenario_id == old.scenario_id,
            Memory.slot_key == old.slot_key,
        ).order_by(Memory.version.desc()).limit(1))
        if latest_version is not None and old.version != latest_version:
            raise DomainError("memory_revision_not_current")
        old.lifecycle_state = LifecycleState.historical
        boundary = new.valid_from if new.valid_from is not None else new.observed_at
        if (
            boundary <= transition_at
            and (old.valid_from is None or boundary > old.valid_from)
            and (old.valid_until is None or boundary < old.valid_until)
        ):
            old.valid_until = boundary
        old.updated_at = transition_at
        new.supersedes_id = old.id
        new.version = old.version + 1
