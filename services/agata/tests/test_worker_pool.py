"""Worker-pool contracts that run without provider calls or LaunchAgent changes."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
sys.path.insert(0, str(WORKER_DIR))

from agata_worker_pool import (  # noqa: E402
    LaneScheduler,
    PoolConfig,
    PoolPaths,
    PoolSupervisor,
    ProcessSlotRunner,
    acquire_lock,
    write_pool_status,
)


def test_production_defaults_to_one_slot_and_requires_explicit_provider_gate_for_more():
    assert PoolConfig.from_env({}).concurrency == 1
    with pytest.raises(ValueError, match="parallel_provider_capability_not_verified"):
        PoolConfig.from_env({"AGATA_WORKER_CONCURRENCY": "2"})
    assert PoolConfig.from_env({
        "AGATA_WORKER_CONCURRENCY": "3",
        "AGATA_CODEX_PARALLEL_CAPABILITY": "VERIFIED",
    }).concurrency == 3
    with pytest.raises(ValueError, match="parallel_provider_capability_not_verified"):
        PoolConfig(concurrency=2)


def test_weighted_scheduler_preserves_gpoint_gsales_priority_without_starvation():
    scheduler = LaneScheduler()
    assert [scheduler.next_production_lane({"GPOINT", "GSALES"}) for _ in range(12)] == [
        "GPOINT", "GPOINT", "GSALES", "GPOINT", "GPOINT", "GSALES",
        "GPOINT", "GPOINT", "GSALES", "GPOINT", "GPOINT", "GSALES",
    ]


def test_scheduler_skips_unconfigured_target_and_only_offers_lab_after_production_empty():
    scheduler = LaneScheduler()
    assert scheduler.next_production_lane({"GPOINT"}) == "GPOINT"
    assert scheduler.next_production_lane({"GPOINT"}) == "GPOINT"
    assert scheduler.next_production_lane({"GPOINT"}) == "GPOINT"
    assert scheduler.next_production_lane(set()) is None
    assert scheduler.lab_may_run(production_claims_empty=True) is True
    assert scheduler.lab_may_run(production_claims_empty=False) is False


def test_pool_slot_and_job_ownership_are_separate_and_exclusive(tmp_path):
    paths = PoolPaths(tmp_path)
    paths.ensure()
    pool = acquire_lock(paths.pool_lock)
    slot = acquire_lock(paths.slot_lock("slot-01"))
    job = acquire_lock(paths.job_lock("job-1"))
    try:
        with pytest.raises(RuntimeError, match="lock_already_held"):
            acquire_lock(paths.pool_lock)
        with pytest.raises(RuntimeError, match="lock_already_held"):
            acquire_lock(paths.slot_lock("slot-01"))
        with pytest.raises(RuntimeError, match="lock_already_held"):
            acquire_lock(paths.job_lock("job-1"))
        other_slot = acquire_lock(paths.slot_lock("slot-02"))
        other_job = acquire_lock(paths.job_lock("job-2"))
        other_slot.close()
        other_job.close()
    finally:
        job.close()
        slot.close()
        pool.close()


def test_default_runtime_tree_is_canonical_and_contains_operational_roots():
    paths = PoolPaths.from_env({})
    assert paths.root == Path.home() / "Library/Application Support/GPoint/agata-codex-worker"
    assert paths.runtime.name == "runtime"
    assert paths.state.name == "state"
    assert paths.locks.name == "locks"
    assert paths.slots.name == "slots"
    assert paths.lab_queue == paths.queues / "lab"


def test_status_is_split_by_pool_lane_slot_and_never_serializes_credentials(tmp_path):
    paths = PoolPaths(tmp_path)
    paths.ensure()
    token = "status-secret-token"
    write_pool_status(paths, {
        "pool": {"state": "HEALTHY", "total": 1, "busy": 0, "idle": 1, "failed": 0},
        "provider": {"state": "HEALTHY", "parallel_capability": "NOT_VERIFIED"},
        "configured_concurrency": 1,
        "lanes": {"GPOINT": {"state": "HEALTHY"}, "GSALES": {"state": "HEALTHY"}, "LAB": {"state": "AVAILABLE"}},
        "slots": {"slot-01": {"state": "IDLE"}},
        "worker_token": token,
    })
    output = paths.status_file.read_text()
    status = json.loads(output)
    assert status["pool"]["state"] == "HEALTHY"
    assert status["configured_concurrency"] == 1
    assert set(status["lanes"]) == {"GPOINT", "GSALES", "LAB"}
    assert token not in output
    assert "worker_token" not in status
    assert paths.status_file.stat().st_mode & 0o077 == 0


def test_maintenance_marker_stops_new_claims_without_stopping_supervisor(tmp_path):
    runner = _FakeRunner()
    calls = []
    pool = PoolSupervisor(PoolPaths(tmp_path), PoolConfig(), {
        "GPOINT": lambda: calls.append("GPOINT"),
        "GSALES": lambda: calls.append("GSALES"),
    }, runner=runner)
    try:
        pool.start()
        pool.paths.maintenance_stop.touch(mode=0o600)
        assert pool.poll_once() == 0
        assert calls == []
        assert not runner.started
        status = json.loads(pool.paths.status_file.read_text())
        assert status["pool"]["state"] == "MAINTENANCE"
    finally:
        pool.close()


def test_successful_pool_claim_polls_clear_stale_legacy_lane_errors(tmp_path):
    paths = PoolPaths(tmp_path)
    paths.ensure()
    paths.worker_status_file.write_text(json.dumps({
        "targets": {
            "GPOINT": {"last_error_code": "worker_target_unavailable", "last_poll_at": "old"},
            "GSALES": {"last_error_code": "worker_target_unavailable", "last_poll_at": "old"},
        },
    }))

    def successful_empty_claim(lane):
        def claim():
            status = json.loads(paths.worker_status_file.read_text())
            status["targets"][lane]["last_poll_at"] = "new"
            paths.worker_status_file.write_text(json.dumps(status))
            return None
        return claim

    pool = PoolSupervisor(paths, PoolConfig(), {
        "GPOINT": successful_empty_claim("GPOINT"),
        "GSALES": successful_empty_claim("GSALES"),
    }, runner=_FakeRunner(), lab_enabled=False)
    try:
        assert pool.poll_once() == 0
        status = json.loads(paths.worker_status_file.read_text())
        for lane in ("GPOINT", "GSALES"):
            assert status["targets"][lane]["last_poll_at"] == "new"
            assert status["targets"][lane]["last_error_code"] is None
    finally:
        pool.close()


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0 if self.code is None else self.code
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9


class _FakeRunner:
    def __init__(self):
        self.started = []

    def start(self, slot_id, lane, offer, slot_lock, job_lock):
        process = _FakeProcess(100 + len(self.started))
        self.started.append((slot_id, lane, offer, process))
        return process


def test_supervisor_limits_assignments_to_configured_isolated_slots(tmp_path):
    runner = _FakeRunner()
    point_jobs = iter([{"id": "p1"}, {"id": "p2"}])
    pool = PoolSupervisor(
        PoolPaths(tmp_path),
        PoolConfig(concurrency=2, provider_parallel_capability="VERIFIED"),
        {"GPOINT": lambda: next(point_jobs, None), "GSALES": lambda: None},
        runner=runner,
    )
    try:
        assert pool.poll_once() == 2
        assert [item[0] for item in runner.started] == ["slot-01", "slot-02"]
        assert [item[2]["id"] for item in runner.started] == ["p1", "p2"]
        assert pool.poll_once() == 0
    finally:
        pool.close()


def test_failed_slot_does_not_stop_other_active_slot(tmp_path):
    runner = _FakeRunner()
    point_jobs = iter([{"id": "p1"}, {"id": "p2"}])
    pool = PoolSupervisor(
        PoolPaths(tmp_path),
        PoolConfig(concurrency=2, provider_parallel_capability="VERIFIED"),
        {"GPOINT": lambda: next(point_jobs, None), "GSALES": lambda: None},
        runner=runner,
    )
    try:
        assert pool.poll_once() == 2
        runner.started[0][3].code = 1
        assert pool.poll_once() == 0
        assert set(pool.active) == {"slot-02"}
        status = json.loads(pool.paths.status_file.read_text())
        assert status["pool"]["busy"] == 1
        assert status["slots"]["slot-01"]["state"] == "FAILED"
    finally:
        pool.close()


def test_restart_marks_unknown_remote_execution_ambiguous_and_never_reexecutes_it(tmp_path):
    paths = PoolPaths(tmp_path)
    paths.ensure()
    slot_dir = paths.slot_dir("slot-01")
    slot_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir / "state.json").write_text(json.dumps({
        "state": "BUSY", "lane": "GPOINT", "job_id": "uncertain-1",
        "phase": "INFERENCE_STARTED", "pid": 12345,
    }))
    runner = _FakeRunner()
    rejected = []
    pool = PoolSupervisor(
        paths, PoolConfig(), {"GPOINT": lambda: {"id": "uncertain-1"}, "GSALES": lambda: None},
        runner=runner, reject_offer=lambda lane, offer, reason: rejected.append((lane, offer, reason)),
    )
    try:
        pool.start()
        assert (paths.state / "ambiguous-jobs/uncertain-1.json").is_file()
        assert pool.poll_once() == 0
        assert runner.started == []
        assert rejected == [("GPOINT", {"id": "uncertain-1"}, "worker_execution_ambiguous")]
    finally:
        pool.close()


def test_supervisor_holds_shared_lifecycle_lock_and_exclusive_pool_lock(tmp_path):
    paths = PoolPaths(tmp_path)
    paths.ensure()
    supervisor = PoolSupervisor(paths, PoolConfig(), {})
    supervisor.start()
    try:
        lifecycle_reader = acquire_lock(paths.lifecycle_lock, shared=True)
        lifecycle_reader.close()
        with pytest.raises(RuntimeError, match="lock_already_held"):
            acquire_lock(paths.lifecycle_lock, blocking=False)
        with pytest.raises(RuntimeError, match="lock_already_held"):
            acquire_lock(paths.pool_lock)
    finally:
        supervisor.close()


def test_lab_is_dispatched_only_after_all_production_lanes_report_empty(tmp_path):
    runner = _FakeRunner()
    calls = []
    pool = PoolSupervisor(
        PoolPaths(tmp_path), PoolConfig(),
        {
            "GPOINT": lambda: calls.append("GPOINT") or None,
            "GSALES": lambda: calls.append("GSALES") or None,
            "LAB": lambda: calls.append("LAB") or {"job_id": "lab-1"},
        }, runner=runner,
    )
    try:
        assert pool.poll_once() == 1
        assert calls == ["GPOINT", "GSALES", "LAB"]
        assert runner.started[0][1] == "LAB"
    finally:
        pool.close()


def test_n2_mock_jobs_execute_in_real_isolated_processes_without_context_mixing(tmp_path):
    script = tmp_path / "mock_slot.py"
    script.write_text(textwrap.dedent("""
        import fcntl, json, os, sys, time
        counter = os.environ["TEST_COUNTER"]
        outcomes = os.environ["TEST_OUTCOMES"]
        with open(counter, "a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            state = json.loads(stream.read() or '{"active": 0, "maximum": 0}')
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            stream.seek(0); stream.truncate(); stream.write(json.dumps(state)); stream.flush()
            fcntl.flock(stream, fcntl.LOCK_UN)
        job = json.load(sys.stdin)
        time.sleep(0.3)
        record = {"id": job["id"], "tenant": job["tenant_id"], "persona": job["request"]["persona"]["name"]}
        with open(outcomes, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX); stream.write(json.dumps(record) + "\\n"); stream.flush(); fcntl.flock(stream, fcntl.LOCK_UN)
        with open(counter, "a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX); stream.seek(0)
            state = json.loads(stream.read()); state["active"] -= 1
            stream.seek(0); stream.truncate(); stream.write(json.dumps(state)); stream.flush(); fcntl.flock(stream, fcntl.LOCK_UN)
    """))
    counter = tmp_path / "counter.json"
    counter.write_text('{"active":0,"maximum":0}')
    outcomes = tmp_path / "outcomes.jsonl"
    jobs = {
        "GPOINT": {"id": "point-1", "target": "GPOINT", "tenant_id": "tenant-point",
                   "request": {"persona": {"name": "point-persona"}}},
        "GSALES": {"id": "sales-1", "target": "GSALES", "tenant_id": "tenant-sales",
                   "request": {"persona": {"name": "sales-persona"}}},
    }
    env = os.environ.copy()
    env.update({"TEST_COUNTER": str(counter), "TEST_OUTCOMES": str(outcomes)})
    original_env = os.environ.copy()
    os.environ.update({"TEST_COUNTER": str(counter), "TEST_OUTCOMES": str(outcomes)})
    runner = ProcessSlotRunner(PoolPaths(tmp_path / "pool"), [sys.executable, str(script)])
    pool = PoolSupervisor(
        PoolPaths(tmp_path / "pool"),
        PoolConfig(concurrency=2, provider_parallel_capability="VERIFIED"),
        {"GPOINT": lambda: jobs.pop("GPOINT", None), "GSALES": lambda: jobs.pop("GSALES", None)},
        runner=runner,
    )
    try:
        assert pool.poll_once() == 2
        deadline = time.monotonic() + 5
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.03)
            pool.poll_once()
        assert not pool.active
    finally:
        pool.close()
        for key in ("TEST_COUNTER", "TEST_OUTCOMES"):
            os.environ.pop(key, None)
        os.environ.update({key: value for key, value in original_env.items() if key in {"TEST_COUNTER", "TEST_OUTCOMES"}})
    assert json.loads(counter.read_text())["maximum"] == 2
    observed = {item["id"]: (item["tenant"], item["persona"])
                for item in map(json.loads, outcomes.read_text().splitlines())}
    assert observed == {"point-1": ("tenant-point", "point-persona"),
                        "sales-1": ("tenant-sales", "sales-persona")}
