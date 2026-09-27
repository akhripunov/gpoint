"""Canonical encrypted, bounded snapshots derived from relationship-owned sources."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Consent,
    EvidenceState,
    JobState,
    Memory,
    MemoryControlState,
    MemorySnapshot,
    OutboxJob,
    UserCharacterSettings,
)
from app.memory.precedence import current_truth_filter
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    encrypt_json_for_storage,
    memory_snapshot_content_aad,
)

SNAPSHOT_TYPES = frozenset({
    "relationship_note", "relationship_summary", "user_profile_summary",
    "conversation_summary", "topic_summary", "day_summary", "goal_summary",
    "project_summary", "current_context_summary",
})
MAX_CONTENT_CHARS = {
    "relationship_note": 1800,
    "relationship_summary": 2600,
    "current_context_summary": 1800,
    "conversation_summary": 2200,
    "user_profile_summary": 2200,
    "topic_summary": 1800,
    "day_summary": 1800,
    "goal_summary": 1800,
    "project_summary": 2200,
}


def snapshot_fingerprint(*parts: object) -> str:
    """Hash only stable source identifiers/versions, never source plaintext."""
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


async def enqueue_relationship_summary_refresh(
    session: AsyncSession, *, tenant_id: UUID, relationship_id: UUID, creation_epoch: int,
) -> None:
    """Invalidate immediately and enqueue an ID-only refresh; never put derived text in outbox."""
    await MemorySnapshotService(session, _configured_crypto()).invalidate_relationship(
        tenant_id, relationship_id, snapshot_types=("relationship_summary",)
    )
    await _enqueue_refresh(
        session, tenant_id=tenant_id, job_type="snapshot_refresh",
        relationship_id=relationship_id, creation_epoch=creation_epoch,
        payload={"relationship_id": str(relationship_id), "snapshot_type": "relationship_summary", "creation_epoch": str(creation_epoch)},
        dedupe_key=f"snapshot-refresh:{relationship_id}:relationship_summary:{creation_epoch}",
    )


async def enqueue_relationship_note_refresh(
    session: AsyncSession, *, tenant_id: UUID, relationship_id: UUID, creation_epoch: int,
) -> None:
    await MemorySnapshotService(session, _configured_crypto()).invalidate_relationship(
        tenant_id, relationship_id, snapshot_types=("relationship_note",)
    )
    await _enqueue_refresh(
        session, tenant_id=tenant_id, job_type="relationship_note_refresh",
        relationship_id=relationship_id, creation_epoch=creation_epoch,
        payload={"relationship_id": str(relationship_id), "creation_epoch": str(creation_epoch)},
        dedupe_key=f"relationship-note:{relationship_id}:{creation_epoch}",
    )


async def _enqueue_refresh(
    session: AsyncSession, *, tenant_id: UUID, job_type: str,
    relationship_id: UUID, creation_epoch: int, payload: dict[str, str], dedupe_key: str,
) -> None:
    from app.jobs.outbox import OutboxRepository

    existing = await session.scalar(select(OutboxJob).where(
        OutboxJob.tenant_id == tenant_id,
        OutboxJob.dedupe_key == dedupe_key,
    ).with_for_update())
    if existing is not None:
        if existing.state in {JobState.completed, JobState.dead}:
            existing.state = JobState.pending
            existing.attempts = 0
            existing.available_at = datetime.now(UTC)
            existing.claimed_at = None
            existing.lease_expires_at = None
            existing.last_error = None
        return
    await OutboxRepository(session).enqueue(
        tenant_id=tenant_id, job_type=job_type, dedupe_key=dedupe_key,
        aggregate_id=relationship_id, creation_epoch=creation_epoch, payload=payload,
    )


def _configured_crypto() -> AeadCrypto:
    from app.messages.protected_content import content_crypto

    return content_crypto()


class MemorySnapshotService:
    def __init__(self, session: AsyncSession, crypto: AeadCrypto) -> None:
        self.session = session
        self.crypto = crypto

    async def upsert(
        self, *, tenant_id: UUID, relationship: Any, snapshot_type: str,
        snapshot_key: str, content: dict[str, Any], source_fingerprint: str,
        creation_epoch: int, source_memory_ids: list[UUID] | None = None,
        source_segment_ids: list[UUID] | None = None,
        source_message_ids: list[UUID] | None = None,
        conversation_id: UUID | None = None, expires_at: datetime | None = None,
        expected_generation: int | None = None,
    ) -> MemorySnapshot | None:
        owner_relationship_id = getattr(relationship, "relationship_id", None)
        if not isinstance(owner_relationship_id, UUID):
            owner_relationship_id = getattr(relationship, "id", None)
        if not isinstance(owner_relationship_id, UUID):
            raise ValueError("snapshot_relationship_owner_invalid")
        if snapshot_type not in SNAPSHOT_TYPES or not isinstance(snapshot_key, str) or not 1 <= len(snapshot_key) <= 255:
            raise ValueError("snapshot_identity_invalid")
        conversation_types = {"conversation_summary", "current_context_summary"}
        if snapshot_type in conversation_types:
            if conversation_id is None or snapshot_key != f"conversation:{conversation_id}":
                raise ValueError("snapshot_conversation_identity_invalid")
        elif conversation_id is not None:
            raise ValueError("snapshot_conversation_identity_invalid")
        if not isinstance(content, dict):
            raise ValueError("snapshot_content_invalid")
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(serialized) > MAX_CONTENT_CHARS[snapshot_type]:
            raise ValueError("snapshot_content_over_budget")
        if not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64:
            raise ValueError("snapshot_fingerprint_invalid")
        if creation_epoch < 0:
            raise ValueError("snapshot_epoch_invalid")
        lock_key = f"snapshot:{tenant_id}:{owner_relationship_id}:{snapshot_type}:{snapshot_key}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock_key}
        )
        row = await self.session.scalar(
            select(MemorySnapshot).where(
                MemorySnapshot.tenant_id == tenant_id,
                MemorySnapshot.relationship_id == owner_relationship_id,
                MemorySnapshot.snapshot_type == snapshot_type,
                MemorySnapshot.snapshot_key == snapshot_key,
            ).with_for_update()
        )
        if expected_generation is not None and (
            (row.generation if row is not None else 0) != expected_generation
        ):
            return row
        if row is not None and row.source_fingerprint == source_fingerprint and row.invalidated_at is None and row.creation_epoch == creation_epoch:
            return row
        generation = row.generation + 1 if row is not None else 1
        snapshot = row or MemorySnapshot(
            id=uuid4(), tenant_id=tenant_id, relationship_id=owner_relationship_id,
            user_id=relationship.user_id, character_id=relationship.character_id,
            scope=relationship.scope, scenario_id=relationship.scenario_id,
            snapshot_type=snapshot_type, snapshot_key=snapshot_key,
        )
        snapshot.generation = generation
        snapshot.conversation_id = conversation_id
        snapshot.source_fingerprint = source_fingerprint
        snapshot.source_memory_ids = [str(value) for value in (source_memory_ids or [])][:64]
        snapshot.source_segment_ids = [str(value) for value in (source_segment_ids or [])][:64]
        snapshot.source_message_ids = [str(value) for value in (source_message_ids or [])][:64]
        snapshot.creation_epoch = creation_epoch
        snapshot.generated_at = datetime.now(UTC)
        snapshot.expires_at = expires_at
        snapshot.invalidated_at = None
        snapshot.content = encrypt_json_for_storage(content, memory_snapshot_content_aad(snapshot), crypto=self.crypto)
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def invalidate_relationship(
        self, tenant_id: UUID, relationship_id: UUID, *, snapshot_types: tuple[str, ...] | None = None
    ) -> int:
        now = datetime.now(UTC)
        stmt = select(MemorySnapshot).where(
            MemorySnapshot.tenant_id == tenant_id,
            MemorySnapshot.relationship_id == relationship_id,
            MemorySnapshot.invalidated_at.is_(None),
        )
        if snapshot_types:
            stmt = stmt.where(MemorySnapshot.snapshot_type.in_(snapshot_types))
        rows = list((await self.session.scalars(stmt.with_for_update())).all())
        for row in rows:
            row.invalidated_at = now
        await self.session.flush()
        return len(rows)

    async def current_epoch(self, tenant_id: UUID, relationship_id: UUID) -> int | None:
        return await self.session.scalar(select(MemoryControlState.deletion_epoch).where(
            MemoryControlState.tenant_id == tenant_id,
            MemoryControlState.relationship_id == relationship_id,
        ))

    async def consent_allowed(self, tenant_id: UUID, user_id: UUID, purpose: str) -> bool:
        return await self.session.scalar(select(Consent.id).where(
            Consent.tenant_id == tenant_id, Consent.user_id == user_id,
            Consent.purpose == purpose, Consent.revoked_at.is_(None),
        ).limit(1)) is not None

    async def refresh_relationship_note(self, *, tenant_id: UUID, relationship: Any) -> MemorySnapshot | None:
        if not await self.consent_allowed(tenant_id, relationship.user_id, "long_term_memory"):
            await self.invalidate_relationship(tenant_id, relationship.id, snapshot_types=("relationship_note", "relationship_summary"))
            return None
        epoch = await self.current_epoch(tenant_id, relationship.id)
        if epoch is None:
            return None
        settings = await self.session.scalar(select(UserCharacterSettings).where(
            UserCharacterSettings.tenant_id == tenant_id,
            UserCharacterSettings.user_id == relationship.user_id,
            UserCharacterSettings.character_id == relationship.character_id,
        ).limit(1))
        preferences = settings.preferences if settings and isinstance(settings.preferences, dict) else {}
        preferred_name = preferences.get("preferred_name")
        if not isinstance(preferred_name, str) or not 1 <= len(preferred_name.strip()) <= 80 or any(c in preferred_name for c in ("\x00", "\n", "<|")):
            preferred_name = None
        now = datetime.now(UTC)
        memories = list((await self.session.scalars(select(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.relationship_id == relationship.id,
            Memory.user_id == relationship.user_id,
            Memory.character_id == relationship.character_id,
            Memory.scope == relationship.scope,
            Memory.scenario_id == relationship.scenario_id,
            current_truth_filter(now),
            Memory.evidence_state.in_([EvidenceState.explicit, EvidenceState.confirmed]),
            Memory.created_epoch == epoch,
        ).order_by(Memory.importance.desc(), Memory.updated_at.desc(), Memory.id).limit(12))).all())
        from app.messages.protected_content import decrypt_text_from_storage, memory_value_aad
        facts: list[dict[str, str]] = []
        valid_memories: list[Memory] = []
        for memory in memories:
            value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory), crypto=self.crypto)
            if isinstance(value, str) and 1 <= len(value.strip()) <= 180:
                facts.append({"predicate": (memory.predicate or memory.slot_key or "fact")[:64], "value": value.strip()})
                valid_memories.append(memory)
        content = {
            "preferred_name": preferred_name, "stable_facts": facts[:10],
            "communication_preferences": [],
        }
        content = {key: value for key, value in content.items() if value not in (None, [], {})}
        fingerprint = snapshot_fingerprint(
            "relationship_note", relationship.id, epoch,
            settings.version if settings else 0,
            [(str(m.id), m.version, m.updated_at.isoformat()) for m in valid_memories],
        )
        expiries = [m.valid_until for m in valid_memories if m.valid_until is not None and m.valid_until > now]
        return await self.upsert(
            tenant_id=tenant_id, relationship=relationship,
            snapshot_type="relationship_note", snapshot_key="relationship",
            content=content or {"stable_facts": []}, source_fingerprint=fingerprint,
            creation_epoch=epoch, source_memory_ids=[m.id for m in valid_memories],
            expires_at=min(expiries) if expiries else None,
        )

    async def resolve_current(
        self, *, tenant_id: UUID, relationship_id: UUID, snapshot_type: str,
        snapshot_key: str, user_id: UUID, required_consent: str,
    ) -> MemorySnapshot | None:
        if not await self.consent_allowed(tenant_id, user_id, required_consent):
            return None
        epoch = await self.current_epoch(tenant_id, relationship_id)
        if epoch is None:
            return None
        now = datetime.now(UTC)
        return await self.session.scalar(select(MemorySnapshot).where(
            MemorySnapshot.tenant_id == tenant_id,
            MemorySnapshot.relationship_id == relationship_id,
            MemorySnapshot.snapshot_type == snapshot_type,
            MemorySnapshot.snapshot_key == snapshot_key,
            MemorySnapshot.creation_epoch == epoch,
            MemorySnapshot.invalidated_at.is_(None),
            (MemorySnapshot.expires_at.is_(None) | (MemorySnapshot.expires_at > now)),
        ).limit(1))
