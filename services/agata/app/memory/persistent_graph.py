"""Evidence-backed persistent projection of canonical Memory revisions."""
from __future__ import annotations

import unicodedata
from hashlib import sha256
from hmac import new
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Consent,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemoryEntity,
    MemoryGraphEvidence,
    MemoryRelation,
    MemorySource,
    Message,
    RetentionPolicy,
    SuppressionRule,
)
from app.jobs.outbox import OutboxRepository
from app.memory.precedence import current_truth_filter, historical_truth_filter, slot_family
from app.messages.crypto import AeadCrypto, derive_hmac_subkey
from app.messages.protected_content import decrypt_text_from_storage, memory_value_aad

GRAPH_ELIGIBLE_PREDICATES = frozenset({
    "preferred_name", "likes", "dislikes", "hobby", "locale", "timezone",
    "current_activity", "goal", "event", "relationship", "emotion", "instruction",
    "works_at", "lives_in",
})
ENTITY_TYPE_MAPPING = {
    "preferred_name": "person", "likes": "preference", "dislikes": "preference",
    "hobby": "activity", "locale": "locale", "timezone": "timezone",
    "current_activity": "activity", "goal": "goal", "event": "event",
    "relationship": "relationship", "emotion": "emotion", "instruction": "instruction",
    "works_at": "organization", "lives_in": "place",
}
RELATION_TYPE_MAPPING = {value: value for value in GRAPH_ELIGIBLE_PREDICATES}
ENTITY_HMAC_DOMAIN = b"gptgirls/memory-graph/entity-key/v1"
SUPPRESSION_HMAC_DOMAIN = b"gptgirls/memory-suppression/v1"
GRAPH_CONTEXT_MAX_ITEMS = 8
GRAPH_CONTEXT_MAX_CHARS = 160


def normalize_entity_value(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not value or len(value) > 512 or "\x00" in value:
        return None
    return value


async def enqueue_memory_graph_sync(
    session: AsyncSession, *, memory: Memory, control: MemoryControlState
) -> None:
    """Queue IDs/revision only; plaintext never enters outbox payloads."""
    await OutboxRepository(session).enqueue(
        tenant_id=memory.tenant_id,
        job_type="memory_graph_sync",
        dedupe_key=f"graph:{memory.id}:{memory.version}:{memory.created_epoch}",
        aggregate_id=memory.id,
        creation_epoch=memory.created_epoch,
        memory_control_state_id=control.id,
        payload={"memory_id": str(memory.id), "memory_version": str(memory.version)},
    )


class PersistentMemoryGraphService:
    def __init__(self, session: AsyncSession, *, crypto: AeadCrypto,
                 hmac_root: bytes | None = None,
                 suppression_hmac_roots: tuple[bytes, ...] | None = None):
        if hmac_root is not None and (not isinstance(hmac_root, bytes) or len(hmac_root) < 32):
            raise ValueError("memory_graph_hmac_key_invalid")
        self.session = session
        self.crypto = crypto
        self._hmac_root = hmac_root
        self._key = derive_hmac_subkey(hmac_root, ENTITY_HMAC_DOMAIN) if hmac_root else None
        roots = suppression_hmac_roots if suppression_hmac_roots is not None else ((hmac_root,) if hmac_root else ())
        if any(not isinstance(root, bytes) or len(root) < 32 for root in roots):
            raise ValueError("memory_graph_suppression_key_invalid")
        self._suppression_keys = tuple(
            derive_hmac_subkey(root, SUPPRESSION_HMAC_DOMAIN) for root in roots
        )

    def _fingerprint(self, *, relationship_id: UUID, entity_type: str, value: str) -> str:
        if self._key is None:
            raise ValueError("memory_graph_projection_key_unavailable")
        digest = new(
            self._key,
            b"gptgirls/memory-graph/entity/v1\x00"
            + relationship_id.bytes + b"\x00" + entity_type.encode("ascii")
            + b"\x00" + value.encode("utf-8"),
            sha256,
        ).hexdigest()
        return f"hmac-sha256:v1:{digest}"

    async def project_memory(self, *, memory_id: UUID, memory_version: int, tenant_id: UUID,
                             control_id: UUID, creation_epoch: int) -> bool:
        """Project one exact canonical version iff every existing authority gate passes."""
        control = await self.session.scalar(select(MemoryControlState).where(
            MemoryControlState.tenant_id == tenant_id, MemoryControlState.id == control_id
        ).with_for_update())
        if control is None or control.deletion_epoch != creation_epoch:
            return False
        memory = await self.session.scalar(select(Memory).where(
            Memory.tenant_id == tenant_id, Memory.id == memory_id,
            Memory.version == memory_version, Memory.relationship_id == control.relationship_id,
            Memory.user_id == control.user_id, Memory.character_id == control.character_id,
            Memory.scope == control.scope, Memory.scenario_key == control.scenario_key,
            Memory.created_epoch == creation_epoch, Memory.deleted_at.is_(None),
            Memory.lifecycle_state.in_((LifecycleState.active, LifecycleState.historical)),
        ).with_for_update())
        if (memory is None or memory.evidence_state.value == "disputed"
                or memory.subject_type != "user" or memory.predicate not in GRAPH_ELIGIBLE_PREDICATES
                or not memory.consent_id):
            return False
        source_exists = await self.session.scalar(select(
            exists().where(
                MemorySource.tenant_id == memory.tenant_id,
                MemorySource.memory_id == memory.id,
                MemorySource.message_id == Message.id,
                Message.tenant_id == MemorySource.tenant_id,
                Message.deleted_at.is_(None),
            )
        ))
        if not source_exists and not (
            memory.kind == "user_fact" and memory.evidence_state == EvidenceState.explicit
        ):
            return False
        consent = await self.session.scalar(select(Consent.id).where(
            Consent.tenant_id == tenant_id, Consent.user_id == memory.user_id,
            Consent.id == memory.consent_id, Consent.purpose == "long_term_memory",
            Consent.revoked_at.is_(None),
        ).limit(1).with_for_update())
        if consent is None:
            return False
        retention = await self.session.scalar(select(RetentionPolicy).where(
            RetentionPolicy.tenant_id == tenant_id,
            RetentionPolicy.id == memory.retention_policy_id,
        )) if memory.retention_policy_id else None
        if retention and not retention.legal_hold and retention.memory_days is not None:
            from sqlalchemy import func
            expired = await self.session.scalar(select(
                memory.created_at + func.make_interval(0, 0, 0, retention.memory_days) <= func.now()
            ))
            if expired:
                return False
        value = decrypt_text_from_storage(memory.value_text, memory_value_aad(memory), crypto=self.crypto)
        normalized = normalize_entity_value(value) if value is not None else None
        if normalized is None:
            return False
        # Defense in depth: never rebuild a value explicitly suppressed in this scope.
        suppression_pairs = [and_(
            SuppressionRule.slot_hmac == new(key, slot_family(memory.slot_key or "").encode(), sha256).hexdigest(),
            SuppressionRule.value_hmac == new(key, value.encode(), sha256).hexdigest(),
        ) for key in self._suppression_keys]
        suppressed_query = select(SuppressionRule.id).where(
            SuppressionRule.tenant_id == tenant_id,
            SuppressionRule.relationship_id == memory.relationship_id,
            SuppressionRule.user_id == memory.user_id,
            SuppressionRule.character_id == memory.character_id,
            SuppressionRule.scope == memory.scope,
            SuppressionRule.scenario_key == memory.scenario_key,
            SuppressionRule.revoked_at.is_(None),
            or_(*suppression_pairs) if suppression_pairs else False,
        ).limit(1)
        suppressed = await self.session.scalar(suppressed_query)
        if suppressed is not None:
            return False
        predicate = memory.predicate
        object_type = ENTITY_TYPE_MAPPING[predicate]
        subject_fp = self._fingerprint(relationship_id=memory.relationship_id, entity_type="user", value=str(memory.user_id))
        object_fp = self._fingerprint(relationship_id=memory.relationship_id, entity_type=object_type, value=normalized)
        scope_fields = dict(
            tenant_id=memory.tenant_id, relationship_id=memory.relationship_id,
            user_id=memory.user_id, character_id=memory.character_id, scope=memory.scope,
            scenario_id=memory.scenario_id, entity_type="user", entity_key_hmac=subject_fp,
            created_epoch=creation_epoch,
        )
        subject_id = await self._upsert_entity(scope_fields)
        target_id = await self._upsert_entity({**scope_fields, "entity_type": object_type, "entity_key_hmac": object_fp})
        relation_stmt = insert(MemoryRelation).values(
            tenant_id=memory.tenant_id, relationship_id=memory.relationship_id,
            user_id=memory.user_id, character_id=memory.character_id, scope=memory.scope,
            scenario_id=memory.scenario_id, source_entity_id=subject_id,
            target_entity_id=target_id, relation_type=RELATION_TYPE_MAPPING[predicate],
            created_epoch=creation_epoch,
        ).on_conflict_do_nothing(constraint="uq_memory_relation_topology").returning(MemoryRelation.id)
        relation_id = await self.session.scalar(relation_stmt)
        if relation_id is None:
            relation_id = await self.session.scalar(select(MemoryRelation.id).where(
                MemoryRelation.tenant_id == tenant_id,
                MemoryRelation.relationship_id == memory.relationship_id,
                MemoryRelation.source_entity_id == subject_id,
                MemoryRelation.target_entity_id == target_id,
                MemoryRelation.relation_type == predicate,
            ))
        evidence_rows = [
            dict(
                tenant_id=tenant_id, relationship_id=memory.relationship_id,
                user_id=memory.user_id, character_id=memory.character_id, scope=memory.scope,
                scenario_id=memory.scenario_id, memory_id=memory.id, memory_version=memory.version,
                entity_id=target_id, evidence_role="defines_entity", created_epoch=creation_epoch,
            ),
            dict(
                tenant_id=tenant_id, relationship_id=memory.relationship_id,
                user_id=memory.user_id, character_id=memory.character_id, scope=memory.scope,
                scenario_id=memory.scenario_id, memory_id=memory.id, memory_version=memory.version,
                relation_id=relation_id, evidence_role="supports_relation", created_epoch=creation_epoch,
            ),
        ]
        for evidence in evidence_rows:
            await self.session.execute(
                insert(MemoryGraphEvidence).values(**evidence).on_conflict_do_nothing()
            )
        await self.session.flush()
        return True

    async def _upsert_entity(self, values: dict) -> UUID:
        stmt = insert(MemoryEntity).values(**values).on_conflict_do_nothing(
            constraint="uq_memory_entity_identity"
        ).returning(MemoryEntity.id)
        entity_id = await self.session.scalar(stmt)
        if entity_id is not None:
            return entity_id
        return await self.session.scalar(select(MemoryEntity.id).where(
            MemoryEntity.tenant_id == values["tenant_id"],
            MemoryEntity.relationship_id == values["relationship_id"],
            MemoryEntity.entity_type == values["entity_type"],
            MemoryEntity.entity_key_hmac == values["entity_key_hmac"],
        ))

    async def load_context(self, *, tenant_id: UUID, relationship_id: UUID,
                           user_id: UUID, character_id: UUID, scope, scenario_key: UUID,
                           as_of, temporal_mode: str, seeds: tuple) -> tuple[str, ...]:
        if not seeds:
            return ()
        ids = [item.memory_id for item in seeds if getattr(item, "memory_id", None)]
        if not ids:
            return ()
        temporal_filter = historical_truth_filter() if temporal_mode == "historical" else current_truth_filter(as_of)
        rows = list((await self.session.execute(
            select(MemoryGraphEvidence.memory_id, MemoryGraphEvidence.memory_version)
            .join(MemoryRelation, and_(
                MemoryRelation.tenant_id == MemoryGraphEvidence.tenant_id,
                MemoryRelation.id == MemoryGraphEvidence.relation_id,
                MemoryRelation.relationship_id == MemoryGraphEvidence.relationship_id,
            ))
            .join(Memory, and_(
                Memory.tenant_id == MemoryGraphEvidence.tenant_id,
                Memory.id == MemoryGraphEvidence.memory_id,
                Memory.version == MemoryGraphEvidence.memory_version,
                Memory.relationship_id == MemoryGraphEvidence.relationship_id,
            ))
            .join(MemoryControlState, and_(
                MemoryControlState.tenant_id == Memory.tenant_id,
                MemoryControlState.relationship_id == Memory.relationship_id,
                MemoryControlState.deletion_epoch == Memory.created_epoch,
                MemoryControlState.deletion_epoch == MemoryGraphEvidence.created_epoch,
            ))
            .join(Consent, and_(Consent.tenant_id == Memory.tenant_id,
                Consent.user_id == Memory.user_id, Consent.purpose == "long_term_memory",
                Consent.id == Memory.consent_id,
                Consent.revoked_at.is_(None)))
            .outerjoin(RetentionPolicy, and_(
                RetentionPolicy.tenant_id == Memory.tenant_id,
                RetentionPolicy.id == Memory.retention_policy_id,
            ))
            .where(
                MemoryGraphEvidence.tenant_id == tenant_id,
                MemoryGraphEvidence.relationship_id == relationship_id,
                MemoryGraphEvidence.user_id == user_id,
                MemoryGraphEvidence.character_id == character_id,
                MemoryGraphEvidence.scope == scope,
                MemoryGraphEvidence.scenario_key == scenario_key,
                MemoryGraphEvidence.memory_id.in_(ids),
                MemoryGraphEvidence.evidence_role == "supports_relation",
                MemoryGraphEvidence.invalidated_at.is_(None),
                MemoryRelation.created_epoch == MemoryGraphEvidence.created_epoch,
                temporal_filter,
                or_(RetentionPolicy.legal_hold.is_(True), RetentionPolicy.memory_days.is_(None),
                    Memory.created_at + func.make_interval(0, 0, 0, RetentionPolicy.memory_days) > func.now()),
            ).order_by(MemoryGraphEvidence.memory_id, MemoryRelation.id)
            .limit(GRAPH_CONTEXT_MAX_ITEMS)
        )).all())
        allowed = {(row.memory_id, row.memory_version) for row in rows}
        output: list[str] = []
        for seed in seeds:
            if (seed.memory_id, seed.version) not in allowed or not isinstance(seed.value, str):
                continue
            label = " ".join(seed.value.replace("\x00", "").replace("<|", "＜|").split())
            if label:
                output.append(f"User {seed.predicate}: {label}"[:GRAPH_CONTEXT_MAX_CHARS])
            if len(output) >= GRAPH_CONTEXT_MAX_ITEMS:
                break
        return tuple(output)

    async def invalidate_memory_evidence(self, *, tenant_id: UUID, memory_id: UUID, now) -> None:
        await self.session.execute(
            MemoryGraphEvidence.__table__.update().where(
                MemoryGraphEvidence.tenant_id == tenant_id,
                MemoryGraphEvidence.memory_id == memory_id,
                MemoryGraphEvidence.invalidated_at.is_(None),
            ).values(invalidated_at=now)
        )

    async def cleanup_orphans(self, *, tenant_id: UUID, relationship_id: UUID) -> None:
        await self.session.execute(
            MemoryRelation.__table__.delete().where(
                MemoryRelation.tenant_id == tenant_id,
                MemoryRelation.relationship_id == relationship_id,
                ~MemoryRelation.id.in_(select(MemoryGraphEvidence.relation_id).where(
                    MemoryGraphEvidence.tenant_id == tenant_id,
                    MemoryGraphEvidence.relation_id.is_not(None),
                    MemoryGraphEvidence.invalidated_at.is_(None),
                )),
            )
        )
        await self.session.execute(
            MemoryEntity.__table__.delete().where(
                MemoryEntity.tenant_id == tenant_id,
                MemoryEntity.relationship_id == relationship_id,
                ~MemoryEntity.id.in_(select(MemoryRelation.source_entity_id).where(
                    MemoryRelation.tenant_id == tenant_id,
                    MemoryRelation.relationship_id == relationship_id,
                ).union(select(MemoryRelation.target_entity_id).where(
                    MemoryRelation.tenant_id == tenant_id,
                    MemoryRelation.relationship_id == relationship_id,
                ))),
            )
        )

    async def rebuild_relationship(self, *, tenant_id: UUID, relationship_id: UUID,
                                   control_id: UUID, limit: int = 500) -> int:
        """Deterministically rebuild derived graph rows from authorized current-epoch Memory only."""
        if self._key is None or not 1 <= limit <= 500:
            raise ValueError("memory_graph_rebuild_invalid")
        control = await self.session.scalar(select(MemoryControlState).where(
            MemoryControlState.tenant_id == tenant_id,
            MemoryControlState.id == control_id,
            MemoryControlState.relationship_id == relationship_id,
        ).with_for_update())
        if control is None:
            return 0
        await self.session.execute(MemoryGraphEvidence.__table__.delete().where(
            MemoryGraphEvidence.tenant_id == tenant_id,
            MemoryGraphEvidence.relationship_id == relationship_id,
        ))
        await self.session.execute(MemoryRelation.__table__.delete().where(
            MemoryRelation.tenant_id == tenant_id,
            MemoryRelation.relationship_id == relationship_id,
        ))
        await self.session.execute(MemoryEntity.__table__.delete().where(
            MemoryEntity.tenant_id == tenant_id,
            MemoryEntity.relationship_id == relationship_id,
        ))
        rows = list((await self.session.scalars(select(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.relationship_id == relationship_id,
            Memory.user_id == control.user_id,
            Memory.character_id == control.character_id,
            Memory.scope == control.scope,
            Memory.scenario_key == control.scenario_key,
            Memory.created_epoch == control.deletion_epoch,
            Memory.deleted_at.is_(None),
            Memory.lifecycle_state.in_((LifecycleState.active, LifecycleState.historical)),
        ).order_by(Memory.id).limit(limit))).all())
        projected = 0
        for row in rows:
            projected += int(await self.project_memory(
                memory_id=row.id, memory_version=row.version, tenant_id=tenant_id,
                control_id=control_id, creation_epoch=control.deletion_epoch,
            ))
        return projected
