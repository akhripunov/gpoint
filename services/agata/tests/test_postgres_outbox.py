"""PostgreSQL coverage for outbox dedupe, leases, retries, and bounded death."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionFactory, set_tenant_context
from app.db.models import JobState, OutboxJob, Tenant
from app.jobs.outbox import OutboxRepository

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
@pytest.mark.asyncio(loop_scope="module")
async def test_outbox_postgres_dedupe_skip_locked_lease_recovery_and_bounded_retry():
    tenant_id = uuid4()
    suffix = uuid4().hex
    dedupe_key = f"outbox-e2e-{suffix}"
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            job = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="generic",
                dedupe_key=dedupe_key,
                payload={"code": "test"},
            )
            job_id = job.id

    async with SessionFactory() as first, SessionFactory() as second:
        async with first.begin():
            await set_tenant_context(first, str(tenant_id))
            claimed = await OutboxRepository(first).claim(tenant_id=tenant_id, limit=1)
            assert [job.id for job in claimed] == [job_id]
            async with second.begin():
                await set_tenant_context(second, str(tenant_id))
                assert await OutboxRepository(second).claim(tenant_id=tenant_id, limit=1) == []

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            job = await session.get(OutboxJob, job_id)
            assert job is not None and job.state == JobState.running and job.attempts == 1
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            assert await OutboxRepository(session).requeue_expired(tenant_id=tenant_id) == 1
            await session.flush()
            assert job.state == JobState.pending and job.attempts == 1

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            claimed = await OutboxRepository(session).claim(tenant_id=tenant_id, limit=1)
            assert len(claimed) == 1 and claimed[0].attempts == 2
            await OutboxRepository(session).fail(claimed[0], "bounded_test", max_attempts=3)
            assert claimed[0].state == JobState.pending
            claimed[0].available_at = datetime.now(UTC) - timedelta(seconds=1)

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            claimed = await OutboxRepository(session).claim(tenant_id=tenant_id, limit=1)
            assert len(claimed) == 1 and claimed[0].attempts == 3
            await OutboxRepository(session).fail(claimed[0], "bounded_test", max_attempts=3)
            assert claimed[0].state == JobState.dead

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await OutboxRepository(session).enqueue(
                        tenant_id=tenant_id,
                        job_type="generic",
                        dedupe_key=dedupe_key,
                        payload={"code": "test"},
                    )
            count = await session.scalar(
                select(func.count()).select_from(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.dedupe_key == dedupe_key,
                )
            )
            assert count == 1
