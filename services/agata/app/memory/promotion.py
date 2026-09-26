"""Default-deny authoritative promotion from locked shadow candidates."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import (
    AuditLog,
    AutoWriteReleasePolicy,
    CandidatePromotionReceipt,
    Consent,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    Message,
    OutboxJob,
    RetentionPolicy,
    ShadowMemoryCandidate,
    SuppressionRule,
)
from app.jobs.outbox import OutboxRepository, StaleCreationEpoch
from app.messages.crypto import (
    AeadCrypto,
    CiphertextEnvelope,
    canonical_tagged_fields,
    derive_hmac_subkey,
)
from app.messages.protected_content import (
    content_aad,
    decrypt_text_from_storage,
    encrypt_text_for_storage,
    memory_value_aad,
)
from app.messages.service import DomainError

PROMOTION_DOMAIN = b"gptgirls/autowrite-promotion/v1"
SUPPRESSION_DOMAIN = b"gptgirls/memory-suppression/v1"
PROMOTABLE_CATEGORIES = frozenset(
    {
        "preferred_name",
        "current_activity",
        "preference",
        "goal",
        "dated_event",
        "relationship",
        "emotional_episode",
        "explicit_instruction",
    }
)
SLOT_BY_CATEGORY = {
    "preferred_name": "user.preferred_name",
    "current_activity": "user.current_activity",
    "preference": "user.preference",
    "goal": "user.goal",
    "dated_event": "user.dated_event",
    "relationship": "user.relationship",
    "emotional_episode": "user.emotional_episode",
    "explicit_instruction": "user.explicit_instruction",
}
ALLOWED_CATEGORY_PREDICATES = frozenset(
    {
        ("preferred_name", "preferred_name"),
        ("current_activity", "current_activity"),
        ("preference", "likes"),
        ("goal", "goal"),
        ("dated_event", "event"),
        ("relationship", "relationship"),
        ("emotional_episode", "emotion"),
        ("explicit_instruction", "instruction"),
    }
)


@dataclass(frozen=True, slots=True)
class PromotionResult:
    status: str
    memory_id: UUID | None


class AutoWritePromotionService:
    """Promote approved facts and short-lived current activity through all gates."""

    def __init__(
        self,
        session: AsyncSession,
        crypto: AeadCrypto,
        *,
        hmac_key: bytes,
        policy_version: str = "promotion-v1",
    ) -> None:
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise DomainError("promotion_key_invalid")
        if not isinstance(policy_version, str) or not policy_version:
            raise DomainError("promotion_policy_invalid")
        self.session, self.crypto, self.policy_version = session, crypto, policy_version
        self._promotion_key = derive_hmac_subkey(hmac_key, PROMOTION_DOMAIN)
        self._suppression_key = derive_hmac_subkey(hmac_key, SUPPRESSION_DOMAIN)

    async def promote(self, *, tenant_id: UUID, candidate_id: UUID) -> PromotionResult:
        if not isinstance(tenant_id, UUID) or not isinstance(candidate_id, UUID):
            raise DomainError("promotion_request_invalid")
        now = datetime.now(UTC)
        transaction = nullcontext() if self.session.in_transaction() else self.session.begin()
        async with transaction:
            await set_tenant_context(self.session, str(tenant_id))
            candidate = await self.session.scalar(
                select(ShadowMemoryCandidate)
                .where(
                    ShadowMemoryCandidate.tenant_id == tenant_id,
                    ShadowMemoryCandidate.id == candidate_id,
                )
                .with_for_update()
            )
            if candidate is None:
                return PromotionResult("denied", None)
            receipt = await self.session.scalar(
                select(CandidatePromotionReceipt).where(
                    CandidatePromotionReceipt.tenant_id == tenant_id,
                    CandidatePromotionReceipt.candidate_id == candidate.id,
                )
            )
            if receipt is not None:
                return PromotionResult(receipt.status, receipt.memory_id)
            if (
                candidate.status != "shadow"
                or candidate.subject != "user"
                or candidate.category not in PROMOTABLE_CATEGORIES
                or candidate.predicate
                not in {
                    "preferred_name",
                    "current_activity",
                    "likes",
                    "goal",
                    "event",
                    "relationship",
                    "emotion",
                    "instruction",
                }
                or (candidate.category, candidate.predicate) not in ALLOWED_CATEGORY_PREDICATES
                or (
                    candidate.category == "preferred_name"
                    and candidate.temporal_hint not in {"none", "stable"}
                )
                or (
                    candidate.category == "current_activity"
                    and candidate.temporal_hint != "temporary"
                )
                or candidate.invalidated_at is not None
                or (candidate.expires_at is not None and candidate.expires_at <= now)
            ):
                return PromotionResult("denied", None)
            release = await self.session.scalar(
                select(AutoWriteReleasePolicy)
                .where(
                    AutoWriteReleasePolicy.tenant_id == tenant_id,
                    AutoWriteReleasePolicy.category == candidate.category,
                    AutoWriteReleasePolicy.extractor_provider == candidate.extractor_provider,
                    AutoWriteReleasePolicy.extractor_model == candidate.extractor_model,
                    AutoWriteReleasePolicy.extractor_prompt_version
                    == candidate.extractor_prompt_version,
                    AutoWriteReleasePolicy.extractor_schema_version
                    == candidate.extractor_schema_version,
                    AutoWriteReleasePolicy.policy_version == self.policy_version,
                    AutoWriteReleasePolicy.enabled.is_(True),
                )
                .order_by(AutoWriteReleasePolicy.created_at.desc())
                .limit(1)
            )
            if release is None or not self._release_allows(release, float(candidate.confidence)):
                return PromotionResult("denied", None)
            control = await self.session.scalar(
                select(MemoryControlState)
                .where(
                    MemoryControlState.tenant_id == tenant_id,
                    MemoryControlState.relationship_id == candidate.relationship_id,
                    MemoryControlState.id == candidate.memory_control_state_id,
                    MemoryControlState.user_id == candidate.user_id,
                    MemoryControlState.character_id == candidate.character_id,
                    MemoryControlState.scope == candidate.scope,
                    MemoryControlState.scenario_id == candidate.scenario_id,
                )
                .with_for_update()
            )
            source = await self.session.scalar(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.id == candidate.source_message_id,
                    Message.user_id == candidate.user_id,
                    Message.character_id == candidate.character_id,
                    Message.scope == candidate.scope,
                    Message.scenario_id == candidate.scenario_id,
                    Message.deleted_at.is_(None),
                )
                .with_for_update()
            )
            consent = await self.session.scalar(
                select(Consent)
                .where(
                    Consent.tenant_id == tenant_id,
                    Consent.user_id == candidate.user_id,
                    Consent.purpose == "long_term_memory",
                    Consent.revoked_at.is_(None),
                )
                .limit(1)
                .with_for_update()
            )
            if (
                control is None
                or source is None
                or consent is None
                or control.deletion_epoch != candidate.source_deletion_epoch
            ):
                return PromotionResult("denied", None)
            retention = None
            if candidate.retention_policy_id is not None:
                retention = await self.session.scalar(
                    select(RetentionPolicy).where(
                        RetentionPolicy.tenant_id == tenant_id,
                        RetentionPolicy.id == candidate.retention_policy_id,
                    )
                )
                if retention is None or (
                    retention.memory_days is not None
                    and candidate.created_at + timedelta(days=retention.memory_days) <= now
                ):
                    return PromotionResult("denied", None)
            try:
                value = self.crypto.decrypt(
                    CiphertextEnvelope(
                        candidate.value_ciphertext,
                        candidate.value_nonce,
                        candidate.value_key_version,
                    ),
                    f"shadow:{tenant_id}:{candidate.user_id}:{candidate.source_message_id}:{candidate.id}".encode(),
                )
            except Exception:
                return PromotionResult("denied", None)
            if not self._valid_value(candidate.category, value):
                return PromotionResult("denied", None)
            slot_key = SLOT_BY_CATEGORY[candidate.category]
            slot_hmac = self._digest(slot_key)
            value_hmac = self._digest(value)
            suppression = await self.session.scalar(
                select(SuppressionRule)
                .where(
                    SuppressionRule.tenant_id == tenant_id,
                    SuppressionRule.relationship_id == candidate.relationship_id,
                    SuppressionRule.user_id == candidate.user_id,
                    SuppressionRule.character_id == candidate.character_id,
                    SuppressionRule.scope == candidate.scope,
                    SuppressionRule.scenario_id == candidate.scenario_id,
                    SuppressionRule.slot_hmac == slot_hmac,
                    SuppressionRule.value_hmac == value_hmac,
                    SuppressionRule.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if suppression is not None:
                return PromotionResult("denied", None)
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {
                    "key": (
                        f"autowrite:{tenant_id}:{candidate.relationship_id}:{candidate.user_id}:{candidate.character_id}:"
                        f"{candidate.scope.value}:{candidate.scenario_id}:{slot_key}"
                    )
                },
            )
            existing = await self.session.scalar(
                select(Memory)
                .where(
                    Memory.tenant_id == tenant_id,
                    Memory.relationship_id == candidate.relationship_id,
                    Memory.user_id == candidate.user_id,
                    Memory.character_id == candidate.character_id,
                    Memory.scope == candidate.scope,
                    Memory.scenario_id == candidate.scenario_id,
                    Memory.slot_key == slot_key,
                    Memory.lifecycle_state == LifecycleState.active,
                )
                .with_for_update()
            )
            status = "promoted"
            existing_value = (
                decrypt_text_from_storage(
                    existing.value_text, memory_value_aad(existing), crypto=self.crypto
                )
                if existing is not None
                else None
            )
            if existing is not None and existing_value != value:
                if candidate.category in {"preferred_name", "current_activity"}:
                    candidate.status = "conflict"
                    await self._receipt(candidate, release, "conflict", None)
                    return PromotionResult("conflict", None)
                existing.lifecycle_state = LifecycleState.historical
                existing.updated_at = now
                status = "conflict"
                existing_id = existing.id
                existing = None
            else:
                existing_id = None
            if existing is None:
                memory_id = uuid4()
                existing = Memory(
                    id=memory_id,
                    tenant_id=tenant_id,
                    relationship_id=candidate.relationship_id,
                    user_id=candidate.user_id,
                    character_id=candidate.character_id,
                    # Current activity is a medium-term episode: it remains
                    # durable in PostgreSQL, but expires into history instead
                    # of becoming a permanent semantic fact.
                    kind="episodic"
                    if candidate.category in {"current_activity", "dated_event"}
                    else "semantic_fact",
                    scope=candidate.scope,
                    scenario_id=candidate.scenario_id,
                    slot_key=slot_key,
                    slot_schema_version=1,
                    subject_type="user",
                    predicate=slot_key.removeprefix("user."),
                    value_text=encrypt_text_for_storage(
                        value,
                        content_aad(
                            entity="memories",
                            tenant_id=tenant_id,
                            user_id=candidate.user_id,
                            character_id=candidate.character_id,
                            scope=candidate.scope,
                            scenario_id=candidate.scenario_id,
                            record_id=memory_id,
                            field="value_text",
                        ),
                        crypto=self.crypto,
                    ),
                    observed_at=source.occurred_at,
                    valid_until=(now + timedelta(days=3))
                    if candidate.category == "current_activity"
                    else None,
                    evidence_state=EvidenceState.inferred,
                    lifecycle_state=LifecycleState.active,
                    confidence=candidate.confidence,
                    importance=0.3,
                    sensitivity="normal",
                    consent_id=consent.id,
                    retention_policy_id=candidate.retention_policy_id,
                    created_epoch=control.deletion_epoch,
                )
                if existing_id is not None:
                    existing.supersedes_id = existing_id
                self.session.add(existing)
                await self.session.flush()
            else:
                status = "identical"
            existing_source = await self.session.scalar(
                select(MemorySource).where(
                    MemorySource.tenant_id == tenant_id,
                    MemorySource.memory_id == existing.id,
                    MemorySource.message_id == candidate.source_message_id,
                    MemorySource.relation == "supports",
                )
            )
            if existing_source is None:
                self.session.add(
                    MemorySource(
                        tenant_id=tenant_id,
                        memory_id=existing.id,
                        message_id=candidate.source_message_id,
                        user_id=candidate.user_id,
                        character_id=candidate.character_id,
                        scope=candidate.scope,
                        scenario_key=candidate.scenario_key,
                        relation="supports",
                    )
                )
            candidate.status = "promoted"
            candidate.promoted_memory_id = existing.id
            self.session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor_type="worker",
                    action="memory_autowrite_promoted",
                    object_id=existing.id,
                    metadata_json={
                        "candidate_id": str(candidate.id),
                        "release_policy_id": str(release.id),
                        "policy_version": release.policy_version,
                    },
                )
            )
            await self._receipt(candidate, release, status, existing.id)
            embed_key = f"embed:{existing.id}:1"
            embed_exists = await self.session.scalar(
                select(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id, OutboxJob.dedupe_key == embed_key
                )
            )
            if embed_exists is None:
                await OutboxRepository(self.session).enqueue(
                    tenant_id=tenant_id,
                    job_type="memory_embed",
                    dedupe_key=embed_key,
                    aggregate_id=existing.id,
                    creation_epoch=control.deletion_epoch,
                    memory_control_state_id=control.id,
                    payload={"memory_id": str(existing.id), "code": "embed"},
                )
            await self.session.flush()
            return PromotionResult(status, existing.id)

    async def process_claimed(self, job: OutboxJob) -> PromotionResult:
        """Promote from a typed claimed job under the outbox epoch transaction."""
        if not isinstance(job, OutboxJob) or job.job_type != "memory_promote":
            raise DomainError("promotion_job_invalid")
        raw_candidate_id = job.payload.get("candidate_id")
        try:
            candidate_id = UUID(raw_candidate_id) if isinstance(raw_candidate_id, str) else None
        except ValueError as exc:
            raise DomainError("promotion_job_invalid") from exc
        if candidate_id is None or job.aggregate_id != candidate_id:
            raise DomainError("promotion_job_invalid")
        async with self.session.begin():
            await set_tenant_context(self.session, str(job.tenant_id))
            try:
                async with self.session.begin_nested():
                    outcomes: list[PromotionResult] = []

                    async def callback(_: AsyncSession) -> None:
                        outcomes.append(
                            await self.promote(
                                tenant_id=job.tenant_id,
                                candidate_id=candidate_id,
                            )
                        )

                    await OutboxRepository(self.session).complete_memory_job(job, callback)
                    return outcomes[0]
            except StaleCreationEpoch:
                await OutboxRepository(self.session).fail(job, "stale_epoch")
                return PromotionResult("denied", None)
            except Exception:
                await OutboxRepository(self.session).fail(job, "promotion_rejected")
                return PromotionResult("denied", None)

    async def _receipt(
        self,
        candidate: ShadowMemoryCandidate,
        release: AutoWriteReleasePolicy,
        status: str,
        memory_id: UUID | None,
    ) -> None:
        fingerprint = new(
            self._promotion_key,
            canonical_tagged_fields(
                (
                    (b"version", PROMOTION_DOMAIN),
                    (b"candidate_id", candidate.id.bytes),
                    (b"release_id", release.id.bytes),
                    (b"candidate_fingerprint", candidate.value_fingerprint.encode("ascii")),
                )
            ),
            sha256,
        ).hexdigest()
        self.session.add(
            CandidatePromotionReceipt(
                tenant_id=candidate.tenant_id,
                candidate_id=candidate.id,
                release_policy_id=release.id,
                promotion_fingerprint=fingerprint,
                status=status,
                memory_id=memory_id,
            )
        )

    def _digest(self, value: str) -> str:
        return new(self._suppression_key, value.encode("utf-8"), sha256).hexdigest()

    @staticmethod
    def _release_allows(policy: AutoWriteReleasePolicy, confidence: float) -> bool:
        return bool(
            policy.enabled
            and policy.category in PROMOTABLE_CATEGORIES
            and confidence >= float(policy.min_confidence)
            and float(policy.measured_precision) >= float(policy.min_precision)
            and policy.measured_support >= policy.min_labelled_support
        )

    @staticmethod
    def _valid_preferred_name(value: object) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 64
            and "\x00" not in value
            and "\n" not in value
            and not any(token in value.casefold() for token in ("password", "address", "secret"))
        )

    @classmethod
    def _valid_value(cls, category: str, value: object) -> bool:
        if category == "preferred_name":
            return cls._valid_preferred_name(value)
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 2000
            and "\x00" not in value
            and not any(
                token in value.casefold() for token in ("password", "secret", "api key", "токен")
            )
        )
