"""PostgreSQL coverage for outbox dedupe, leases, retries, and bounded death."""

import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import ensure_test_relationship
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionFactory, set_tenant_context
from app.db.models import Character, JobState, MemoryControlState, OutboxJob, Scope, Tenant, User
from app.jobs.outbox import OutboxRepository
from app.messages.crypto import AeadCrypto
from app.responses.mac_worker import MacWorkerResponseProvider, WorkerInferenceUnavailable
from app.responses.pipeline import ResponseContext

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")


@pytest.mark.asyncio(loop_scope="module")
async def test_memory_job_deferral_locks_control_before_parent_job():
    tenant_id, user_id, character_id, control_id = (uuid4() for _ in range(4))
    suffix = uuid4().hex
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            session.add_all([
                User(id=user_id, tenant_id=tenant_id, external_id=f"defer-{suffix}"),
                Character(
                    id=character_id, tenant_id=tenant_id,
                    external_id=f"defer-{suffix}", name="Synthetic defer character",
                ),
            ])
            await session.flush()
            relationship = await ensure_test_relationship(
                session, tenant_id, user_id, character_id, Scope.real_world, None
            )
            control = MemoryControlState(
                id=control_id, tenant_id=tenant_id,
                relationship_id=relationship.id, user_id=user_id,
                character_id=character_id, scope=Scope.real_world,
                scenario_id=None,
            )
            session.add(control)
            await session.flush()
            parent = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="memory_extract",
                dedupe_key=f"defer-lock-order:{suffix}",
                aggregate_id=uuid4(),
                memory_control_state_id=control_id,
                creation_epoch=0,
                payload={"message_id": str(uuid4())},
            )
            parent.state = JobState.running
            parent.claimed_at = now
            parent.lease_expires_at = now + timedelta(seconds=60)
            parent_id = parent.id

    async def defer_parent():
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(tenant_id))
                parent = await session.get(OutboxJob, parent_id)
                assert parent is not None
                return await OutboxRepository(session).defer_pending_memory_job(parent)

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            await session.scalar(select(MemoryControlState).where(
                MemoryControlState.tenant_id == tenant_id,
                MemoryControlState.id == control_id,
            ).with_for_update())
            deferred = asyncio.create_task(defer_parent())
            await asyncio.sleep(0.1)
            parent = await session.scalar(select(OutboxJob).where(
                OutboxJob.tenant_id == tenant_id,
                OutboxJob.id == parent_id,
            ).with_for_update(nowait=True))
            assert parent is not None and parent.state == JobState.running
    assert await deferred is True
    async with SessionFactory() as session:
        await set_tenant_context(session, str(tenant_id))
        parent = await session.get(OutboxJob, parent_id)
        assert parent is not None and parent.state == JobState.pending
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
            await session.refresh(job)
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
async def test_expired_outbox_leases_stop_at_the_job_retry_budget():
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    suffix = uuid4().hex
    now = datetime.now(UTC)
    expired = now - timedelta(seconds=1)
    at_budget = (
        ("relationship_note_refresh", {"relationship_id": str(uuid4()), "creation_epoch": "0"}, 3),
        ("inference", {"purpose": "memory_extract", "parent_job_id": str(uuid4())}, 1),
        ("inference", {"purpose": "conversation_summary", "parent_job_id": str(uuid4())}, 1),
        ("inference", {"purpose": "response"}, 2),
        ("generic", {"code": "synthetic"}, 5),
    )
    job_ids = []
    async with SessionFactory() as session:
        async with session.begin():
            session.add_all([Tenant(id=tenant_id), Tenant(id=other_tenant_id)])
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            for index, (job_type, payload, attempts) in enumerate(at_budget):
                job = await OutboxRepository(session).enqueue(
                    tenant_id=tenant_id,
                    job_type=job_type,
                    dedupe_key=f"expired-budget:{suffix}:{index}",
                    payload=payload,
                )
                job.state = JobState.running
                job.attempts = attempts
                job.claimed_at = expired - timedelta(minutes=1)
                job.lease_expires_at = expired
                job.claim_token = uuid4().hex
                job_ids.append(job.id)
            below_budget = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="inference",
                dedupe_key=f"expired-budget:{suffix}:below",
                payload={"purpose": "response"},
            )
            below_budget.state = JobState.running
            below_budget.attempts = 1
            below_budget.claimed_at = expired - timedelta(minutes=1)
            below_budget.lease_expires_at = expired
            below_budget.claim_token = uuid4().hex
            below_budget_id = below_budget.id

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            repository = OutboxRepository(session)
            assert await repository.requeue_expired(tenant_id=tenant_id) == len(at_budget) + 1
            for job_id, (_, _, attempts) in zip(job_ids, at_budget, strict=True):
                job = await session.get(OutboxJob, job_id)
                assert job is not None
                assert job.state == JobState.dead
                assert job.attempts == attempts
                assert job.last_error == "lease_retry_budget_exhausted"
                assert job.claimed_at is None
                assert job.lease_expires_at is None
                assert job.claim_token is None
            below_budget = await session.get(OutboxJob, below_budget_id)
            assert below_budget is not None
            assert below_budget.state == JobState.pending
            assert below_budget.attempts == 1
            assert below_budget.last_error is None
            claimed = await repository.claim(
                tenant_id=tenant_id, job_types=frozenset({"inference"})
            )
            assert [job.id for job in claimed] == [below_budget_id]
            assert claimed[0].attempts == 2

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(other_tenant_id))
            other = await OutboxRepository(session).enqueue(
                tenant_id=other_tenant_id,
                job_type="generic",
                dedupe_key=f"expired-budget:{suffix}:other-tenant",
                payload={"code": "synthetic"},
            )
            other.state = JobState.running
            other.attempts = 5
            other.claimed_at = expired - timedelta(minutes=1)
            other.lease_expires_at = expired
            other.claim_token = uuid4().hex
            other_id = other.id

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(tenant_id))
            assert await OutboxRepository(session).requeue_expired(tenant_id=tenant_id) == 0
    async with SessionFactory() as session:
        await set_tenant_context(session, str(other_tenant_id))
        other = await session.get(OutboxJob, other_id)
        assert other is not None and other.state == JobState.running


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_expired_lease_recovery_updates_a_job_once():
    tenant_id = uuid4()
    suffix = uuid4().hex
    expired = datetime.now(UTC) - timedelta(seconds=1)
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            job = await OutboxRepository(session).enqueue(
                tenant_id=tenant_id,
                job_type="generic",
                dedupe_key=f"expired-concurrent:{suffix}",
                payload={"code": "synthetic"},
            )
            job.state = JobState.running
            job.attempts = 1
            job.claimed_at = expired - timedelta(minutes=1)
            job.lease_expires_at = expired
            job.claim_token = uuid4().hex

    async def recover() -> int:
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, str(tenant_id))
                return await OutboxRepository(session).requeue_expired(tenant_id=tenant_id)

    assert sorted(await asyncio.gather(recover(), recover())) == [0, 1]


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
async def test_mac_worker_claims_live_responses_before_older_background_jobs():
    tenant_id = uuid4()
    source_id = uuid4()
    parent_id = uuid4()
    async with SessionFactory() as session:
        async with session.begin():
            session.add(Tenant(id=tenant_id))
            await session.flush()
            await set_tenant_context(session, str(tenant_id))
            repository = OutboxRepository(session)
            background, _ = await repository.enqueue_idempotent(
                tenant_id=tenant_id,
                job_type="inference",
                dedupe_key=f"memory-task:memory_extract:{parent_id}",
                aggregate_id=source_id,
                payload={"purpose": "memory_extract", "parent_job_id": str(parent_id)},
            )
            live, _ = await repository.enqueue_inference_idempotent(
                tenant_id=tenant_id,
                dedupe_key=f"inference:{uuid4()}",
                aggregate_id=uuid4(),
                payload={},
            )
            background.available_at = datetime.now(UTC) - timedelta(seconds=10)
            live.available_at = datetime.now(UTC)

            claimed = await repository.claim(
                tenant_id=tenant_id,
                limit=1,
                job_types=frozenset({"inference"}),
                response_jobs_first=True,
            )

            assert len(claimed) == 1
            assert claimed[0].id == live.id
            assert background.state == JobState.pending


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
