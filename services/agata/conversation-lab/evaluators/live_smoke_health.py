"""Fail-closed worker readiness checks for sequential local LAB smoke runs.

The LAB queue publishes a terminal receipt before the worker pool publishes its
post-job idle status. Callers must reconcile that expected transition before
starting another generation; this module never treats a transitional or stale
snapshot as healthy.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


GUARD_NAME = "worker_unhealthy_or_stale"
DEFAULT_FRESHNESS_SECONDS = 45
DEFAULT_RECONCILIATION_TIMEOUT_SECONDS = 8
DEFAULT_POLL_INTERVAL_SECONDS = 0.1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class WorkerHealthStop(RuntimeError):
    """A fail-closed readiness stop carrying a payload-free diagnostic receipt."""

    def __init__(self, receipt: dict[str, Any]):
        self.receipt = receipt
        super().__init__(GUARD_NAME)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def read_json_status(path: Path) -> dict[str, Any]:
    """Read one atomic JSON status snapshot; malformed/missing data fails closed."""
    try:
        with Path(path).open("rb") as stream:
            file_stat = os.fstat(stream.fileno())
            raw = stream.read()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("worker_status_unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("worker_status_invalid")
    value["_controller_readback"] = {
        "source": Path(path).name,
        "source_mtime_at": _iso(datetime.fromtimestamp(file_stat.st_mtime, timezone.utc)),
        "source_mtime_ns": file_stat.st_mtime_ns,
    }
    return value


def read_lab_queue_state(root: Path) -> dict[str, Any]:
    """Return only queue identifiers, never job payloads or receipt contents."""
    root = Path(root)
    result: dict[str, Any] = {"observed_at": _iso(datetime.now(timezone.utc))}
    receipt_states: dict[str, str] = {}
    for name in ("pending", "running", "receipts"):
        folder = root / name
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError("lab_queue_state_unreadable")
        files = list(folder.glob("*.json"))
        ids = [path.stem for path in files]
        if any(_safe_id(job_id) is None for job_id in ids):
            raise ValueError("lab_queue_state_invalid")
        result[f"{name}_job_ids"] = sorted(ids)
        if name == "receipts":
            for receipt_path in files:
                try:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ValueError("lab_queue_receipt_unreadable") from exc
                if not isinstance(receipt, dict) or receipt.get("job_id") != receipt_path.stem:
                    raise ValueError("lab_queue_receipt_invalid")
                state = receipt.get("status")
                if state not in {"completed", "failed", "cancelled"}:
                    raise ValueError("lab_queue_receipt_invalid")
                receipt_states[receipt_path.stem] = state
    result["completed_job_ids"] = sorted(
        job_id for job_id, state in receipt_states.items() if state == "completed"
    )
    running_ids = result["running_job_ids"]
    result["current_job_id"] = running_ids[0] if len(running_ids) == 1 else None
    result["readback_valid"] = True
    return result


def _pool_status(pool: dict[str, Any] | None) -> dict[str, Any]:
    source = pool.get("pool") if isinstance(pool, dict) else None
    source = source if isinstance(source, dict) else {}
    return {"state": source.get("state"), "busy": source.get("busy"),
            "idle": source.get("idle")}


def evaluate_worker_status(
    worker: dict[str, Any],
    *,
    observed_at: datetime,
    pool: dict[str, Any] | None = None,
    launch_agent: dict[str, Any] | None = None,
    queue: dict[str, Any] | None = None,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    source: str = "worker-status.json",
    completed_job_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate all readiness predicates and produce sanitized evidence."""
    now = observed_at.astimezone(timezone.utc) if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    now_text = _iso(now)
    checks: list[dict[str, Any]] = []

    def add(predicate: str, expected: str, observed: Any, *, field_source: str,
            observed_timestamp: Any = None, threshold: int | None = None,
            age: float | None = None, passed: bool) -> None:
        checks.append({
            "predicate": predicate,
            "expected": expected,
            "observed": observed,
            "source": field_source,
            "observed_timestamp": observed_timestamp,
            "controller_timestamp": now_text,
            "observed_age_seconds": round(age, 3) if age is not None else None,
            "threshold_seconds": threshold,
            "clock": "UTC wall clock for freshness; monotonic clock for bounded wait",
            "passed": passed,
        })

    heartbeat_raw = worker.get("last_heartbeat_at")
    heartbeat = _parse_time(heartbeat_raw)
    heartbeat_age = (now - heartbeat).total_seconds() if heartbeat else None
    add("worker.heartbeat_fresh", f"valid timestamp age <= {freshness_seconds}s",
        heartbeat_raw, field_source=f"{source}.last_heartbeat_at",
        observed_timestamp=heartbeat_raw, threshold=freshness_seconds, age=heartbeat_age,
        passed=heartbeat_age is not None and 0 <= heartbeat_age <= freshness_seconds)

    readback = worker.get("_controller_readback") if isinstance(worker.get("_controller_readback"), dict) else {}
    status_mtime_raw = readback.get("source_mtime_at")
    status_mtime = _parse_time(status_mtime_raw)
    status_age = (now - status_mtime).total_seconds() if status_mtime else None
    add("worker.status_snapshot_fresh", f"valid file mtime age <= {freshness_seconds}s",
        status_mtime_raw, field_source=f"{readback.get('source', source)} filesystem mtime",
        observed_timestamp=status_mtime_raw, threshold=freshness_seconds, age=status_age,
        passed=status_age is not None and 0 <= status_age <= freshness_seconds)

    state = worker.get("state")
    add("worker.state_healthy", "healthy or idle", state,
        field_source=f"{source}.state", passed=isinstance(state, str) and state.lower() in {"healthy", "idle"})
    current = worker.get("current_job_present")
    add("worker.current_job_cleared", "false", current,
        field_source=f"{source}.current_job_present", passed=current is False)
    error = worker.get("last_error_code")
    add("worker.last_error_code_is_clear", "null or empty", error,
        field_source=f"{source}.last_error_code", passed=error in (None, ""))

    pool_state = _pool_status(pool)
    add("pool.state_healthy", "HEALTHY", pool_state.get("state"),
        field_source="pool-status.json.pool.state",
        passed=pool_state.get("state") == "HEALTHY")
    add("pool.no_busy_slots", "busy == 0", pool_state.get("busy"),
        field_source="pool-status.json.pool.busy",
        passed=pool_state.get("busy") == 0)
    pool_readback = pool.get("_controller_readback") if isinstance(pool, dict) and isinstance(pool.get("_controller_readback"), dict) else {}
    pool_mtime_raw = pool_readback.get("source_mtime_at")
    pool_mtime = _parse_time(pool_mtime_raw)
    pool_age = (now - pool_mtime).total_seconds() if pool_mtime else None
    add("pool.status_snapshot_fresh", f"valid file mtime age <= {freshness_seconds}s",
        pool_mtime_raw, field_source=f"{pool_readback.get('source', 'pool-status.json')} filesystem mtime",
        observed_timestamp=pool_mtime_raw, threshold=freshness_seconds, age=pool_age,
        passed=pool_age is not None and 0 <= pool_age <= freshness_seconds)

    targets = worker.get("targets") if isinstance(worker.get("targets"), dict) else {}
    for lane in ("GPOINT", "GSALES"):
        target = targets.get(lane) if isinstance(targets.get(lane), dict) else {}
        lane_state = target.get("state")
        add(f"target.{lane}.state_healthy", "healthy or idle", lane_state,
            field_source=f"{source}.targets.{lane}.state",
            passed=isinstance(lane_state, str) and lane_state.lower() in {"healthy", "idle"})
        lane_error = target.get("last_error_code")
        add(f"target.{lane}.last_error_code_is_clear", "null or empty", lane_error,
            field_source=f"{source}.targets.{lane}.last_error_code",
            passed=lane_error in (None, ""))
        poll_raw = target.get("last_poll_at")
        poll = _parse_time(poll_raw)
        poll_age = (now - poll).total_seconds() if poll else None
        add(f"target.{lane}.poll_fresh", f"valid timestamp age <= {freshness_seconds}s",
            poll_raw, field_source=f"{source}.targets.{lane}.last_poll_at",
            observed_timestamp=poll_raw, threshold=freshness_seconds, age=poll_age,
            passed=poll_age is not None and 0 <= poll_age <= freshness_seconds)

    lab = targets.get("LAB") if isinstance(targets.get("LAB"), dict) else {}
    lab_state = lab.get("state")
    add("target.LAB.state_idle", "idle or healthy", lab_state,
        field_source=f"{source}.targets.LAB.state",
        passed=isinstance(lab_state, str) and lab_state.lower() in {"idle", "healthy"})
    add("target.LAB.current_job_cleared", "false", lab.get("current_job_present"),
        field_source=f"{source}.targets.LAB.current_job_present",
        passed=lab.get("current_job_present") is False)
    add("target.LAB.last_error_code_is_clear", "null or empty", lab.get("last_error_code"),
        field_source=f"{source}.targets.LAB.last_error_code",
        passed=lab.get("last_error_code") in (None, ""))

    agent = launch_agent if isinstance(launch_agent, dict) else {}
    loaded = agent.get("loaded")
    add("launch_agent.loaded", "true", loaded,
        field_source="launchctl service readback.loaded",
        observed_timestamp=agent.get("observed_at"), passed=loaded is True)
    agent_label = agent.get("label")
    add("launch_agent.expected_label", "one.gpoint.agata-codex-worker", agent_label,
        field_source="launchctl service readback.label",
        observed_timestamp=agent.get("observed_at"),
        passed=agent_label == "one.gpoint.agata-codex-worker")
    agent_observed_raw = agent.get("observed_at")
    agent_observed = _parse_time(agent_observed_raw)
    agent_age = (now - agent_observed).total_seconds() if agent_observed else None
    add("launch_agent.readback_fresh", f"valid timestamp age <= {freshness_seconds}s",
        agent_observed_raw, field_source="launchctl service readback.observed_at",
        observed_timestamp=agent_observed_raw, threshold=freshness_seconds, age=agent_age,
        passed=agent_age is not None and 0 <= agent_age <= freshness_seconds)

    queue_safe = _safe_queue(queue)
    queue_valid = queue_safe["readback_valid"]
    add("lab_queue.readback_valid", "valid pending/running/receipts inventory", queue_valid,
        field_source="LAB queue directories", observed_timestamp=queue_safe["observed_at"],
        passed=queue_valid)
    queue_observed = _parse_time(queue_safe["observed_at"])
    queue_age = (now - queue_observed).total_seconds() if queue_observed else None
    add("lab_queue.readback_fresh", f"valid timestamp age <= {freshness_seconds}s",
        queue_safe["observed_at"], field_source="LAB queue readback.observed_at",
        observed_timestamp=queue_safe["observed_at"], threshold=freshness_seconds, age=queue_age,
        passed=queue_age is not None and 0 <= queue_age <= freshness_seconds)
    add("lab_queue.no_pending_jobs", "empty pending_job_ids", queue_safe["pending_job_ids"],
        field_source="LAB queue pending directory", passed=queue_valid and not queue_safe["pending_job_ids"])
    add("lab_queue.no_running_jobs", "empty running_job_ids", queue_safe["running_job_ids"],
        field_source="LAB queue running directory", passed=queue_valid and not queue_safe["running_job_ids"])
    add("lab_queue.no_current_job", "null current_job_id", queue_safe["current_job_id"],
        field_source="LAB queue running directory", passed=queue_valid and queue_safe["current_job_id"] is None)
    receipt_present = (
        completed_job_id is not None and
        completed_job_id in queue_safe["completed_job_ids"]
    )
    add("lab_queue.completed_receipt_present", "completed job ID in receipt directory",
        completed_job_id if receipt_present else None,
        field_source="LAB queue receipts directory", observed_timestamp=queue_safe["observed_at"],
        passed=queue_valid and receipt_present)

    observed = {
        "worker": {
            "state": state,
            "current_job_present": current,
            "last_error_code": error,
            "last_heartbeat_at": heartbeat_raw,
            "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
            "status_snapshot_mtime_at": status_mtime_raw,
            "status_snapshot_age_seconds": round(status_age, 3) if status_age is not None else None,
        },
        "pool": {**pool_state, "status_snapshot_mtime_at": pool_mtime_raw,
                 "status_snapshot_age_seconds": round(pool_age, 3) if pool_age is not None else None},
        "targets": {
            lane: {
                "state": (targets.get(lane) or {}).get("state") if isinstance(targets.get(lane), dict) else None,
                "last_poll_at": (targets.get(lane) or {}).get("last_poll_at") if isinstance(targets.get(lane), dict) else None,
                "last_error_code": (targets.get(lane) or {}).get("last_error_code") if isinstance(targets.get(lane), dict) else None,
            }
            for lane in ("GPOINT", "GSALES", "LAB")
        },
        "launch_agent": _safe_launch_agent(launch_agent),
        "queue": queue_safe,
    }
    failed = [item for item in checks if not item["passed"]]
    return {
        "healthy": not failed,
        "guard": GUARD_NAME,
        "controller_timestamp": now_text,
        "source": source,
        "freshness_threshold_seconds": freshness_seconds,
        "failed_predicates": failed,
        "predicates": checks,
        "observed": observed,
    }


def _safe_queue(queue: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(queue, dict):
        return {"observed_at": None, "current_job_id": None, "pending_job_ids": [],
                "running_job_ids": [], "completed_job_ids": [], "readback_valid": False}

    def ids(key: str) -> list[str]:
        raw = queue.get(key)
        if not isinstance(raw, (tuple, list)):
            return []
        return sorted(value for value in raw if _safe_id(value))

    required = ("pending_job_ids", "running_job_ids", "completed_job_ids")
    structurally_valid = all(
        isinstance(queue.get(key), (tuple, list)) and
        all(_safe_id(value) is not None for value in queue[key])
        for key in required
    )
    current_raw = queue.get("current_job_id")
    current_valid = current_raw is None or _safe_id(current_raw) is not None
    observed_at = queue.get("observed_at")
    observed_at_valid = _parse_time(observed_at) is not None

    return {
        "observed_at": observed_at,
        "current_job_id": _safe_id(current_raw),
        "pending_job_ids": ids("pending_job_ids"),
        "running_job_ids": ids("running_job_ids"),
        "completed_job_ids": ids("completed_job_ids"),
        "readback_valid": queue.get("readback_valid") is True and structurally_valid and current_valid and observed_at_valid,
    }


def _safe_launch_agent(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: value.get(key) for key in ("label", "loaded", "pid", "last_exit_code", "observed_at")}


_RECONCILIABLE_PREDICATES = {
    "worker.state_healthy",
    "worker.current_job_cleared",
    "pool.no_busy_slots",
    "target.LAB.state_idle",
    "target.LAB.current_job_cleared",
    "lab_queue.no_running_jobs",
    "lab_queue.no_current_job",
}


def wait_for_post_completion_ready(
    *,
    completed_job_id: str,
    status_reader: Callable[[], dict[str, Any]],
    launch_agent_reader: Callable[[], dict[str, Any]],
    queue_reader: Callable[[], dict[str, Any]],
    diagnostic_receipt_path: Path,
    pool_status_reader: Callable[[], dict[str, Any]],
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_RECONCILIATION_TIMEOUT_SECONDS,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    on_observation: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Wait for canonical post-receipt idle evidence; never starts/retries a job."""
    if not _ID_RE.fullmatch(completed_job_id):
        raise ValueError("completed_job_id_invalid")
    if poll_interval_seconds <= 0 or timeout_seconds < 0:
        raise ValueError("reconciliation_bounds_invalid")

    started = monotonic()
    deadline = started + timeout_seconds
    last_report: dict[str, Any] | None = None
    observations = 0
    while True:
        controller_timestamp = utcnow()
        try:
            worker = status_reader()
            pool = pool_status_reader() if pool_status_reader else None
            agent = launch_agent_reader()
            queue = queue_reader()
            last_report = evaluate_worker_status(
                worker,
                observed_at=controller_timestamp,
                pool=pool,
                launch_agent=agent,
                queue=queue,
                freshness_seconds=freshness_seconds,
                completed_job_id=completed_job_id,
            )
        except Exception as exc:
            # Do not serialize exception text: adapters may accidentally include paths or payloads.
            last_report = {
                "healthy": False,
                "guard": GUARD_NAME,
                "controller_timestamp": _iso(controller_timestamp),
                "freshness_threshold_seconds": freshness_seconds,
                "failed_predicates": [{
                    "predicate": "status_readback.available",
                    "expected": "all read-only status sources available and valid",
                    "observed": type(exc).__name__,
                    "source": "status/launchd/queue readback adapters",
                    "observed_timestamp": None,
                    "controller_timestamp": _iso(controller_timestamp),
                    "observed_age_seconds": None,
                    "threshold_seconds": None,
                    "clock": "UTC wall clock for evidence; monotonic clock for bounded wait",
                    "passed": False,
                }],
                "predicates": [],
                "observed": {
                    "worker": None,
                    "pool": None,
                    "targets": {"GPOINT": None, "GSALES": None, "LAB": None},
                    "launch_agent": None,
                    "queue": None,
                },
            }

        last_report["completed_job_id"] = completed_job_id
        last_report["reconciliation_elapsed_seconds"] = round(monotonic() - started, 3)
        observations += 1
        last_report["observation_count"] = observations
        if on_observation is not None:
            on_observation(last_report)
        if last_report["healthy"]:
            return last_report

        failures = last_report["failed_predicates"]
        failed_names = {item["predicate"] for item in failures}
        # The completed receipt is published before the matching running entry
        # is removed. Reconcile only that exact, evidenced job cleanup race.
        queue_evidence = last_report.get("observed", {}).get("queue") or {}
        cleanup_only = (
            queue_evidence.get("readback_valid") is True and
            queue_evidence.get("completed_job_ids") is not None and
            completed_job_id in queue_evidence.get("completed_job_ids", []) and
            queue_evidence.get("running_job_ids") == [completed_job_id] and
            queue_evidence.get("current_job_id") == completed_job_id and
            not queue_evidence.get("pending_job_ids")
        )
        if cleanup_only:
            failed_names.discard("lab_queue.no_running_jobs")
            failed_names.discard("lab_queue.no_current_job")
        if not failed_names.issubset(_RECONCILIABLE_PREDICATES):
            write_private_diagnostic_receipt(diagnostic_receipt_path, last_report)
            raise WorkerHealthStop(last_report)
        remaining = deadline - monotonic()
        if remaining <= 0:
            write_private_diagnostic_receipt(diagnostic_receipt_path, last_report)
            raise WorkerHealthStop(last_report)
        sleep(min(poll_interval_seconds, remaining))


def write_private_diagnostic_receipt(path: Path, receipt: dict[str, Any]) -> None:
    """Atomically persist sanitized diagnostic evidence with owner-only mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".health-receipt-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
