from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobState, MemoryControlState, OutboxJob
from app.messages.protected_content import validate_outbox_payload

JOB_TYPES = frozenset(
    {
        "generic",
        "conversation_summary",
        "memory_extract",
        "memory_promote",
        "memory_embed",
        "response_retry",
    }
)


class StaleCreationEpoch(Exception):
    """Raised when a job was created before a scoped deletion epoch advanced."""


class OutboxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

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

    async def claim(
        self,
        *,
        tenant_id: UUID,
        limit: int = 1,
        lease_seconds: int = 60,
        job_types: frozenset[str] | None = None,
    ) -> list[OutboxJob]:
        now = datetime.now(UTC)
        stmt = (
            select(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.state == JobState.pending,
                OutboxJob.available_at <= now,
            )
            .order_by(OutboxJob.available_at, OutboxJob.id)
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
        await self.session.flush()

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

    async def fail(self, job: OutboxJob, error: str, *, max_attempts: int = 5) -> None:
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
        locked.last_error = error[:2000]
        locked.lease_expires_at = None
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
        stmt = (
            update(OutboxJob)
            .where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.state == JobState.running,
                OutboxJob.lease_expires_at < now,
            )
            .values(state=JobState.pending, claimed_at=None, lease_expires_at=None)
        )
        if job_types is not None:
            stmt = stmt.where(OutboxJob.job_type.in_(job_types))
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
