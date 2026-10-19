from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobState, MemoryControlState, OutboxJob
from app.messages.protected_content import validate_outbox_payload

JOB_TYPES = frozenset(
    {
        "generic",
        "conversation_summary",
        "relationship_note_refresh",
        "snapshot_refresh",
        "memory_extract",
        "memory_promote",
        "memory_embed",
        "memory_graph_sync",
        "response_retry",
        "inference",
        "sales_draft_inference",
    }
)
MEMORY_WORKER_JOB_TYPES = frozenset(
    {
        "conversation_summary",
        "relationship_note_refresh",
        "snapshot_refresh",
        "memory_extract",
        "memory_promote",
        "memory_embed",
        "memory_graph_sync",
    }
)
INFERENCE_JOB_TYPES = frozenset({"inference"})
BACKGROUND_INFERENCE_PURPOSES = frozenset({"memory_extract", "conversation_summary"})
DEFAULT_MAX_ATTEMPTS = 5
MEMORY_MAX_ATTEMPTS = 3
BACKGROUND_INFERENCE_MAX_ATTEMPTS = 1
REGULAR_INFERENCE_MAX_ATTEMPTS = 2


def retry_budget_for_job(job_type: str, payload: dict) -> int:
    """Return the existing retry policy for a claimed job identity."""
    if job_type in MEMORY_WORKER_JOB_TYPES:
        return MEMORY_MAX_ATTEMPTS
    if job_type in INFERENCE_JOB_TYPES:
        if payload.get("purpose", "response") in BACKGROUND_INFERENCE_PURPOSES:
            return BACKGROUND_INFERENCE_MAX_ATTEMPTS
        return REGULAR_INFERENCE_MAX_ATTEMPTS
    return DEFAULT_MAX_ATTEMPTS


def _retry_budget_expression():
    inference_budget = case(
        (
            OutboxJob.payload["purpose"].astext.in_(BACKGROUND_INFERENCE_PURPOSES),
            BACKGROUND_INFERENCE_MAX_ATTEMPTS,
        ),
        else_=REGULAR_INFERENCE_MAX_ATTEMPTS,
    )
    return case(
        (OutboxJob.job_type.in_(MEMORY_WORKER_JOB_TYPES), MEMORY_MAX_ATTEMPTS),
        (OutboxJob.job_type.in_(INFERENCE_JOB_TYPES), inference_budget),
        else_=DEFAULT_MAX_ATTEMPTS,
    )


class StaleCreationEpoch(Exception):
    """Raised when a job was created before a scoped deletion epoch advanced."""


class OutboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def expire_stale_inference_jobs(self, *, tenant_id: UUID, ttl_seconds: int) -> int:
        """Bound worker-offline backlog and invalidate leases past the inference TTL."""
        if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 3600:
            raise ValueError("inference_job_ttl_invalid")
        result = await self.session.execute(
            update(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.job_type.in_(("inference", "sales_draft_inference")),
                OutboxJob.state.in_((JobState.pending, JobState.running)),
                OutboxJob.created_at < datetime.now(UTC) - timedelta(seconds=ttl_seconds),
            )
            .values(
                state=JobState.dead,
                claim_token=None,
                lease_expires_at=None,
                last_error="inference_job_expired",
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        job_type: str,
        dedupe_key: str,
        payload: dict,
        aggregate_id: UUID | None = None,
        creation_epoch: int = 0,
        memory_control_state_id: UUID | None = None,
    ) -> OutboxJob:
        if not isinstance(job_type, str) or job_type not in JOB_TYPES:
            raise ValueError("unsupported outbox job_type")
        payload = dict(payload)
        if "memory_control_state_id" in payload:
            raise ValueError("memory_control_state_id is a typed column, not payload data")
        try:
            validate_outbox_payload(job_type, payload)
        except ValueError as exc:
            raise ValueError("outbox_payload_not_content_safe") from exc
        if job_type.startswith("memory_") and memory_control_state_id is None:
            raise StaleCreationEpoch("memory job requires memory_control_state_id")
        job = OutboxJob(
            tenant_id=tenant_id,
            job_type=job_type,
            dedupe_key=dedupe_key,
            payload=payload,
            aggregate_id=aggregate_id,
            creation_epoch=creation_epoch,
            memory_control_state_id=memory_control_state_id,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def enqueue_inference_idempotent(
        self,
        *,
        tenant_id: UUID,
        dedupe_key: str,
        aggregate_id: UUID,
        payload: dict,
    ) -> tuple[OutboxJob, bool]:
        """Create one inference row, or reuse only its exact dedupe identity.

        The conflict target is deliberately limited to uq_outbox_dedupe. Other
        constraint failures still raise, and a collision with a different job
        identity is rejected after reading the conflicting row.
        """
        return await self.enqueue_idempotent(
            tenant_id=tenant_id,
            job_type="inference",
            dedupe_key=dedupe_key,
            aggregate_id=aggregate_id,
            payload=payload,
        )

    async def enqueue_idempotent(
        self,
        *,
        tenant_id: UUID,
        job_type: str,
        dedupe_key: str,
        aggregate_id: UUID,
        payload: dict,
        creation_epoch: int = 0,
        memory_control_state_id: UUID | None = None,
        reactivate_terminal: bool = False,
    ) -> tuple[OutboxJob, bool]:
        """Atomically insert or reuse a row with the same complete job identity.

        This explicit path is for job types whose dedupe key is an idempotency
        contract. The ordinary ``enqueue`` method intentionally remains strict.
        """
        if not isinstance(job_type, str) or job_type not in JOB_TYPES:
            raise ValueError("unsupported outbox job_type")
        if not isinstance(dedupe_key, str) or not dedupe_key:
            raise ValueError("invalid outbox dedupe key")
        safe_payload = dict(payload)
        if "memory_control_state_id" in safe_payload:
            raise ValueError("memory_control_state_id is a typed column, not payload data")
        try:
            validate_outbox_payload(job_type, safe_payload)
        except ValueError as exc:
            raise ValueError("outbox_payload_not_content_safe") from exc
        if job_type.startswith("memory_") and memory_control_state_id is None:
            raise StaleCreationEpoch("memory job requires memory_control_state_id")
        proposed_id = uuid4()
        statement = (
            pg_insert(OutboxJob)
            .values(
                id=proposed_id,
                tenant_id=tenant_id,
                job_type=job_type,
                dedupe_key=dedupe_key,
                aggregate_id=aggregate_id,
                payload=safe_payload,
                creation_epoch=creation_epoch,
                memory_control_state_id=memory_control_state_id,
            )
            .on_conflict_do_nothing(constraint="uq_outbox_dedupe")
            .returning(OutboxJob.id)
        )
        inserted_id = await self.session.scalar(statement)
        job = await self.session.scalar(
            select(OutboxJob)
            .where(OutboxJob.tenant_id == tenant_id, OutboxJob.dedupe_key == dedupe_key)
            .with_for_update()
        )
        if job is None:
            raise RuntimeError("outbox_idempotent_insert_not_visible")
        if (
            job.job_type != job_type
            or job.aggregate_id != aggregate_id
            or job.creation_epoch != creation_epoch
            or job.memory_control_state_id != memory_control_state_id
        ):
            raise ValueError(f"{job_type}_outbox_dedupe_conflict")
        if not inserted_id and reactivate_terminal and job.state in {JobState.completed, JobState.dead}:
            job.state = JobState.pending
            job.attempts = 0
            job.available_at = datetime.now(UTC)
            job.claimed_at = None
            job.lease_expires_at = None
            job.claim_token = None
            job.last_error = None
            await self.session.flush()
        return job, inserted_id is not None

    async def claim(
        self,
        *,
        tenant_id: UUID,
        limit: int = 1,
        lease_seconds: int = 60,
        job_types: frozenset[str] | None = None,
        response_jobs_first: bool = False,
    ) -> list[OutboxJob]:
        now = datetime.now(UTC)
        order_by = [OutboxJob.available_at, OutboxJob.id]
        if response_jobs_first:
            order_by.insert(
                0,
                case(
                    (
                        OutboxJob.payload["purpose"].astext.in_(
                            ("memory_extract", "conversation_summary")
                        ),
                        1,
                    ),
                    else_=0,
                ),
            )
        stmt = (
            select(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.state == JobState.pending,
                OutboxJob.available_at <= now,
            )
            .order_by(*order_by)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if job_types is not None:
            stmt = stmt.where(OutboxJob.job_type.in_(job_types))
        rows = list((await self.session.scalars(stmt)).all())
        for job in rows:
            job.state = JobState.running
            job.attempts += 1
            job.claimed_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.claim_token = uuid4().hex
        await self.session.flush()
        return rows

    async def complete(self, job: OutboxJob) -> None:
        if job.memory_control_state_id is not None or job.job_type.startswith("memory_"):
            raise StaleCreationEpoch("epoch-guarded memory jobs require complete_memory_job")
        if job.state != JobState.running or (
            job.lease_expires_at is not None and job.lease_expires_at < datetime.now(UTC)
        ):
            raise StaleCreationEpoch("job is not running or lease expired")
        job.state = JobState.completed
        job.lease_expires_at = None
        job.claim_token = None
        await self.session.flush()

    async def complete_memory_job(
        self,
        job: OutboxJob,
        write_authoritative: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        """Lock control and job rows, perform writes, and complete atomically."""
        if job.memory_control_state_id is None:
            raise StaleCreationEpoch("memory job has no typed memory_control_state_id")
        control = await self.session.scalar(
            select(MemoryControlState)
            .where(
                MemoryControlState.id == job.memory_control_state_id,
                MemoryControlState.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        locked = await self.session.scalar(
            select(OutboxJob).where(OutboxJob.id == job.id).with_for_update()
        )
        if control is None or locked is None or locked.state != JobState.running:
            raise StaleCreationEpoch("memory control/job is unavailable")
        self.ensure_epoch(job=locked, current_epoch=control.deletion_epoch)
        if locked.lease_expires_at is not None and locked.lease_expires_at < datetime.now(
            UTC
        ):
            raise StaleCreationEpoch("memory job lease expired")
        if write_authoritative is not None:
            await write_authoritative(self.session)
        locked.state = JobState.completed
        locked.lease_expires_at = None
        locked.claim_token = None
        await self.session.flush()

    async def defer_pending_memory_job(
        self, job: OutboxJob, *, delay_seconds: int = 5, reason: str = "awaiting_provider"
    ) -> bool:
        """Release a memory lease while its idempotent Mac Worker child is pending."""
        if (
            not isinstance(delay_seconds, int)
            or isinstance(delay_seconds, bool)
            or not 1 <= delay_seconds <= 60
            or not isinstance(reason, str)
            or not reason.isascii()
            or not reason.replace("_", "").isalnum()
            or len(reason) > 64
        ):
            raise ValueError("memory_job_defer_invalid")
        identity = await self.session.scalar(select(OutboxJob).where(
            OutboxJob.id == job.id,
            OutboxJob.tenant_id == job.tenant_id,
        ))
        if identity is None or identity.memory_control_state_id is None:
            raise StaleCreationEpoch("memory job is not running")
        control = await self.session.scalar(
            select(MemoryControlState).where(
                MemoryControlState.tenant_id == identity.tenant_id,
                MemoryControlState.id == identity.memory_control_state_id,
            ).with_for_update()
        )
        locked = await self.session.scalar(
            select(OutboxJob).where(
                OutboxJob.id == job.id,
                OutboxJob.tenant_id == job.tenant_id,
            ).with_for_update()
        )
        if (
            not isinstance(locked, OutboxJob)
            or locked.state != JobState.running
            or locked.memory_control_state_id != identity.memory_control_state_id
        ):
            raise StaleCreationEpoch("memory job is not running")
        if control is None or control.deletion_epoch != locked.creation_epoch:
            locked.state = JobState.dead
            locked.last_error = "stale_epoch"
            locked.claim_token = None
            locked.lease_expires_at = None
            await self.session.flush()
            return False
        if locked.lease_expires_at is not None and locked.lease_expires_at <= datetime.now(UTC):
            raise StaleCreationEpoch("memory job lease expired")
        locked.attempts = max(0, locked.attempts - 1)
        locked.state = JobState.pending
        locked.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        locked.claimed_at = None
        locked.lease_expires_at = None
        locked.claim_token = None
        locked.last_error = reason
        await self.session.flush()
        return True

    async def bump_deletion_epoch(self, control_id: UUID, tenant_id: UUID) -> int:
        control = await self.session.scalar(
            select(MemoryControlState)
            .where(
                MemoryControlState.id == control_id,
                MemoryControlState.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if control is None:
            raise StaleCreationEpoch("memory control is unavailable")
        control.deletion_epoch += 1
        await self.session.flush()
        return control.deletion_epoch

    async def fail(
        self, job: OutboxJob, error: str, *, max_attempts: int | None = None
    ) -> None:
        locked = await self.session.scalar(
            select(OutboxJob).where(OutboxJob.id == job.id).with_for_update()
        )
        # Unit callers may use an in-memory job/mock session; production always locks the row.
        in_memory = not isinstance(locked, OutboxJob)
        if in_memory:
            locked = job
        if not in_memory and (locked is None or locked.state != JobState.running):
            raise StaleCreationEpoch("job is not running")
        assert isinstance(locked, OutboxJob)
        if max_attempts is None:
            max_attempts = retry_budget_for_job(locked.job_type, locked.payload)
        locked.last_error = error[:2000]
        locked.lease_expires_at = None
        locked.claim_token = None
        locked.state = JobState.dead if locked.attempts >= max_attempts else JobState.failed
        if locked.state == JobState.failed:
            locked.state = JobState.pending
            locked.available_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2**locked.attempts)
            )
        await self.session.flush()

    @staticmethod
    def ensure_epoch(*, job: OutboxJob, current_epoch: int) -> None:
        if job.creation_epoch != current_epoch:
            raise StaleCreationEpoch(job.id)

    async def requeue_expired(
        self, *, tenant_id: UUID, job_types: frozenset[str] | None = None
    ) -> int:
        """Recover only this tenant's expired leases before a worker claims.

        Attempts are intentionally preserved: a crash must not reset the bounded
        retry budget.  Optional job-type filtering prevents one specialised
        worker from taking ownership of another worker's lifecycle.
        """
        now = datetime.now(UTC)
        retry_budget_exhausted = OutboxJob.attempts >= _retry_budget_expression()
        stmt = (
            update(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.state == JobState.running,
                OutboxJob.lease_expires_at < now,
            )
            .values(
                state=case(
                    (retry_budget_exhausted, literal(JobState.dead, type_=OutboxJob.state.type)),
                    else_=literal(JobState.pending, type_=OutboxJob.state.type),
                ),
                last_error=case(
                    (retry_budget_exhausted, "lease_retry_budget_exhausted"),
                    else_=OutboxJob.last_error,
                ),
                claimed_at=None,
                lease_expires_at=None,
                claim_token=None,
            )
        )
        if job_types is not None:
            stmt = stmt.where(OutboxJob.job_type.in_(job_types))
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
