"""PostgreSQL coverage for outbox dedupe, leases, retries, and bounded death."""

import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionFactory, set_tenant_context
from app.db.models import JobState, OutboxJob, Tenant
from app.jobs.outbox import OutboxRepository
from app.messages.crypto import AeadCrypto
from app.responses.mac_worker import MacWorkerResponseProvider, WorkerInferenceUnavailable
from app.responses.pipeline import ResponseContext

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


@pytest.mark.asyncio(loop_scope="module")
async def test_inference_worker_offline_ttl_expires_only_inference_jobs():
    tenant_id = uuid4()
    suffix = uuid4().hex
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            repository = OutboxRepository(session)
            inference = await repository.enqueue(
                tenant_id=tenant_id,
                job_type="inference",
                dedupe_key=f"inference-ttl-{suffix}",
                payload={},
            )
            other = await repository.enqueue(
                tenant_id=tenant_id,
                job_type="response_retry",
                dedupe_key=f"response-retry-ttl-{suffix}",
                payload={},
            )
            stale = datetime.now(UTC) - timedelta(minutes=10)
            inference.created_at = stale
            other.created_at = stale

            expired = await repository.expire_stale_inference_jobs(
                tenant_id=tenant_id, ttl_seconds=300
            )

            assert expired == 1
            await session.refresh(inference)
            assert inference.state == JobState.dead
            assert inference.last_error == "inference_job_expired"
            assert other.state == JobState.pending


@pytest.mark.asyncio(loop_scope="module")
async def test_full_mac_worker_queue_returns_controlled_unavailable_without_new_job(monkeypatch):
    from app.responses import mac_worker

    tenant_id = uuid4()
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="inference",
                dedupe_key=f"queue-full-{uuid4().hex}",
                payload={},
            )

    monkeypatch.setattr(
        mac_worker,
        "get_settings",
        lambda: SimpleNamespace(inference_job_ttl_seconds=420, max_pending_inference_jobs=1),
    )
    crypto = AeadCrypto(
        encoded_key=base64.b64encode(b"x" * 32).decode(),
        key_version="v1",
    )
    provider = MacWorkerResponseProvider(
        SessionFactory(), crypto=crypto, tenant_id=tenant_id, model="codex-default"
    )
    context = ResponseContext(
        profile_version=1,
        route_tuple=("user", "character", "real_world", "", ""),
        current_message="hello",
        source_message_id=uuid4(),
        source_idempotency_key="queue-full-test",
    )

    async with provider.session as session:
        with pytest.raises(WorkerInferenceUnavailable):
            await provider.complete(context)
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            count = await session.scalar(
                select(func.count()).select_from(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.job_type == "inference",
                    OutboxJob.state.in_((JobState.pending, JobState.running)),
                )
            )
            assert count == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_inference_enqueue_reuses_only_the_matching_dedupe_identity():
    tenant_id = uuid4()
    message_id = uuid4()
    dedupe_key = f"inference:{message_id}"
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))

    async def enqueue_once():
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(tenant_id))
                return await OutboxRepository(session).enqueue_inference_idempotent(
                    tenant_id=tenant_id,
                    dedupe_key=dedupe_key,
                    aggregate_id=message_id,
                    payload={},
                )

    (first, first_created), (second, second_created) = await asyncio.gather(
        enqueue_once(), enqueue_once()
    )
    assert first.id == second.id
    assert sorted((first_created, second_created)) == [False, True]

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            with pytest.raises(ValueError, match="inference_outbox_dedupe_conflict"):
                await OutboxRepository(session).enqueue_inference_idempotent(
                    tenant_id=tenant_id,
                    dedupe_key=dedupe_key,
                    aggregate_id=uuid4(),
                    payload={},
                )
            count = await session.scalar(
                select(func.count()).select_from(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.dedupe_key == dedupe_key,
                )
            )
            assert count == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_relationship_note_refresh_enqueue_reuses_exact_identity_and_keeps_transaction_usable():
    tenant_id = uuid4()
    relationship_id = uuid4()
    dedupe_key = f"relationship-note:{relationship_id}:0"
    payload = {"relationship_id": str(relationship_id), "creation_epoch": "0"}
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            repository = OutboxRepository(session)
            first, first_created = await repository.enqueue_idempotent(
                tenant_id=tenant_id,
                job_type="relationship_note_refresh",
                dedupe_key=dedupe_key,
                aggregate_id=relationship_id,
                creation_epoch=0,
                payload=payload,
            )
            duplicate, duplicate_created = await repository.enqueue_idempotent(
                tenant_id=tenant_id,
                job_type="relationship_note_refresh",
                dedupe_key=dedupe_key,
                aggregate_id=relationship_id,
                creation_epoch=0,
                payload=payload,
            )
            assert first_created is True
            assert duplicate_created is False
            assert duplicate.id == first.id

            with pytest.raises(ValueError, match="relationship_note_refresh_outbox_dedupe_conflict"):
                await repository.enqueue_idempotent(
                    tenant_id=tenant_id,
                    job_type="relationship_note_refresh",
                    dedupe_key=dedupe_key,
                    aggregate_id=uuid4(),
                    creation_epoch=0,
                    payload=payload,
                )

            count = await session.scalar(
                select(func.count()).select_from(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.dedupe_key == dedupe_key,
                )
            )
            assert count == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_existing_pending_and_running_inference_jobs_return_pending_without_reenqueue(monkeypatch):
    from app.responses import mac_worker

    tenant_id = uuid4()
    message_ids = [uuid4(), uuid4()]
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            repository = OutboxRepository(session)
            await repository.enqueue_inference_idempotent(
                tenant_id=tenant_id,
                dedupe_key=f"inference:{message_ids[0]}",
                aggregate_id=message_ids[0],
                payload={},
            )
            running = await repository.enqueue_inference_idempotent(
                tenant_id=tenant_id,
                dedupe_key=f"inference:{message_ids[1]}",
                aggregate_id=message_ids[1],
                payload={},
            )
            running[0].state = JobState.running
            running[0].attempts = 1

    monkeypatch.setattr(
        mac_worker,
        "get_settings",
        lambda: SimpleNamespace(inference_job_ttl_seconds=420, max_pending_inference_jobs=10),
    )
    crypto = AeadCrypto(encoded_key=base64.b64encode(b"x" * 32).decode(), key_version="v1")
    for message_id in message_ids:
        provider = MacWorkerResponseProvider(
            SessionFactory(), crypto=crypto, tenant_id=tenant_id, model="codex-default", wait_seconds=0
        )
        context = ResponseContext(
            profile_version=1,
            route_tuple=("user", "character", "real_world", "", ""),
            current_message="hello",
            source_message_id=message_id,
            source_idempotency_key=str(message_id),
        )
        with pytest.raises(mac_worker.WorkerInferencePending):
            await provider.complete(context)
        await provider.session.close()

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            count = await session.scalar(
                select(func.count()).select_from(OutboxJob).where(
                    OutboxJob.tenant_id == tenant_id,
                    OutboxJob.job_type == "inference",
                    OutboxJob.aggregate_id.in_(message_ids),
                )
            )
            assert count == 2
