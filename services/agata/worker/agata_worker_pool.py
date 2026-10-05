"""Shared AGATA Worker Pool paths, scheduler, locks, and safe status contract."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CANONICAL_ROOT = Path.home() / "Library/Application Support/GPoint/agata-codex-worker"
LANES = ("GPOINT", "GSALES", "LAB")
MAX_SLOTS = 16
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TOP_LEVEL = {"pool", "provider", "configured_concurrency", "lanes", "slots", "updated_at", "targets"}
_SENSITIVE_KEY_PARTS = ("token", "secret", "lease", "prompt", "request", "context", "response", "credential")


class PoolLockError(RuntimeError):
    """Pool/lifecycle/slot/job lock could not be acquired safely."""


@dataclass(frozen=True)
class PoolConfig:
    concurrency: int = 1
    provider_parallel_capability: str = "NOT_VERIFIED"
    poll_seconds: float = 3.0

    def __post_init__(self) -> None:
        if isinstance(self.concurrency, bool) or not 1 <= self.concurrency <= MAX_SLOTS:
            raise ValueError("worker_concurrency_invalid")
        if self.provider_parallel_capability not in {"NOT_VERIFIED", "VERIFIED"}:
            raise ValueError("parallel_provider_capability_invalid")
        if self.concurrency > 1 and self.provider_parallel_capability != "VERIFIED":
            raise ValueError("parallel_provider_capability_not_verified")
        if not 1 <= self.poll_seconds <= 60:
            raise ValueError("worker_poll_interval_invalid")

    @classmethod
    def from_env(cls, env: dict[str, str]) -> PoolConfig:
        raw = env.get("AGATA_WORKER_CONCURRENCY", "1")
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_SLOTS:
            raise ValueError("worker_concurrency_invalid")
        concurrency = int(raw)
        capability = env.get("AGATA_CODEX_PARALLEL_CAPABILITY", "NOT_VERIFIED")
        if capability not in {"NOT_VERIFIED", "VERIFIED"}:
            raise ValueError("parallel_provider_capability_invalid")
        if concurrency > 1 and capability != "VERIFIED":
            raise ValueError("parallel_provider_capability_not_verified")
        try:
            poll_seconds = float(env.get("AGATA_WORKER_POLL_SECONDS", "3"))
        except (TypeError, ValueError) as exc:
            raise ValueError("worker_poll_interval_invalid") from exc
        if not 1 <= poll_seconds <= 60:
            raise ValueError("worker_poll_interval_invalid")
        return cls(concurrency, capability, poll_seconds)


@dataclass(frozen=True)
class PoolPaths:
    root: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("worker_root_must_be_absolute")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PoolPaths:
        values = os.environ if env is None else env
        root = values.get("GPOINT_AGATA_WORKER_SUPPORT") or values.get("AGATA_WORKER_ROOT")
        return cls(Path(root).expanduser() if root else CANONICAL_ROOT)

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def receipts(self) -> Path:
        return self.root / "receipts"

    @property
    def status(self) -> Path:
        return self.root / "status"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def queues(self) -> Path:
        return self.root / "queues"

    @property
    def lab_queue(self) -> Path:
        return self.queues / "lab"

    @property
    def slots(self) -> Path:
        return self.root / "slots"

    @property
    def pool_lock(self) -> Path:
        return self.locks / "pool.lock"

    @property
    def lifecycle_lock(self) -> Path:
        return self.locks / "lifecycle.lock"

    @property
    def maintenance_stop(self) -> Path:
        return self.state / "maintenance-stop"

    @property
    def status_file(self) -> Path:
        return self.status / "pool-status.json"

    @property
    def worker_status_file(self) -> Path:
        return self.status / "worker-status.json"

    def slot_dir(self, slot_id: str) -> Path:
        _validate_id(slot_id, "slot_identity_invalid")
        return self.slots / slot_id

    def slot_lock(self, slot_id: str) -> Path:
        return self.slot_dir(slot_id) / "ownership.lock"

    def job_lock(self, job_id: str) -> Path:
        _validate_id(job_id, "job_identity_invalid")
        return self.locks / "jobs" / f"{job_id}.lock"

    def ensure(self) -> None:
        directories = (
            self.root, self.runtime, self.config, self.state, self.locks,
            self.locks / "jobs", self.receipts, self.status, self.logs,
            self.queues, self.lab_queue, self.slots,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid():
                raise ValueError("worker_path_owner_or_symlink_invalid")
            directory.chmod(0o700)


class LaneScheduler:
    """Weighted production rotation; LAB is considered only after production is empty."""

    _CYCLE = ("GPOINT", "GPOINT", "GSALES", "GPOINT", "GPOINT", "GSALES")

    def __init__(self) -> None:
        self._cursor = 0

    def next_production_lane(self, configured: set[str]) -> str | None:
        if not configured.intersection({"GPOINT", "GSALES"}):
            return None
        for _ in self._CYCLE:
            lane = self._CYCLE[self._cursor]
            self._cursor = (self._cursor + 1) % len(self._CYCLE)
            if lane in configured:
                return lane
        return None

    def production_order(self, configured: set[str]) -> list[str]:
        """Return each configured lane once, beginning at the weighted cursor."""
        result: list[str] = []
        for offset in range(len(self._CYCLE)):
            lane = self._CYCLE[(self._cursor + offset) % len(self._CYCLE)]
            if lane in configured and lane not in result:
                result.append(lane)
        return result

    def record_dispatch(self, lane: str) -> None:
        if lane not in {"GPOINT", "GSALES"}:
            raise ValueError("production_lane_invalid")
        for offset in range(len(self._CYCLE)):
            index = (self._cursor + offset) % len(self._CYCLE)
            if self._CYCLE[index] == lane:
                self._cursor = (index + 1) % len(self._CYCLE)
                return

    @staticmethod
    def lab_may_run(*, production_claims_empty: bool) -> bool:
        return production_claims_empty


def _validate_id(value: str, error: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(error)


def acquire_lock(path: Path, *, blocking: bool = False, shared: bool = False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(descriptor, flags)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise PoolLockError("lock_already_held") from exc
    return os.fdopen(descriptor, "a", encoding="utf-8")


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_value(item)
            for key, item in value.items()
            if isinstance(key, str) and not any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def write_pool_status(paths: PoolPaths, status: dict[str, Any]) -> None:
    paths.ensure()
    safe = {key: _safe_value(value) for key, value in status.items() if key in _SAFE_TOP_LEVEL}
    safe.setdefault("updated_at", datetime.now(UTC).isoformat())
    payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".pool-status-", dir=paths.status)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, paths.status_file)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_slot_status(paths: PoolPaths, slot_id: str, state: dict[str, Any]) -> None:
    directory = paths.slot_dir(slot_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    current: dict[str, Any] = {}
    try:
        current = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    payload = {**current, **state, "updated_at": datetime.now(UTC).isoformat()}
    fd, temporary = tempfile.mkstemp(prefix=".slot-state-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "state.json")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass
class _ActiveSlot:
    slot_id: str
    lane: str
    job_id: str
    process: subprocess.Popen
    slot_lock: Any
    job_lock: Any


class ProcessSlotRunner:
    """Launch one isolated Python process for one claimed inference job."""

    def __init__(self, paths: PoolPaths, command_prefix: list[str] | None = None) -> None:
        self.paths = paths
        self.command_prefix = command_prefix or [
            sys.executable,
            str(Path(__file__).with_name("gpoint_mac_worker.py")),
        ]

    def start(self, slot_id: str, lane: str, offer: dict[str, Any], slot_lock, job_lock):
        env = os.environ.copy()
        env["GPOINT_AGATA_WORKER_SUPPORT"] = str(self.paths.root)
        env["AGATA_WORKER_SLOT_ID"] = slot_id
        env["AGATA_WORKER_POOL_ROOT"] = str(self.paths.root)
        env["AGATA_WORKER_SLOT_LOCK_FD"] = str(slot_lock.fileno())
        env["AGATA_WORKER_JOB_LOCK_FD"] = str(job_lock.fileno())
        command = [*self.command_prefix, "execute-slot", "--lane", lane, "--slot", slot_id]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            pass_fds=(slot_lock.fileno(), job_lock.fileno()),
            start_new_session=True,
        )
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(offer, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            process.stdin.close()
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            raise
        return process


class PoolSupervisor:
    """Own the pool and schedule atomic remote/LAB claims onto isolated slots."""

    def __init__(
        self,
        paths: PoolPaths,
        config: PoolConfig,
        claimers: dict[str, Any],
        *,
        runner: Any | None = None,
        reject_offer: Any | None = None,
        lab_enabled: bool | None = None,
        clock=time.monotonic,
    ) -> None:
        self.paths = paths
        self.config = config
        self.claimers = claimers
        self.runner = runner or ProcessSlotRunner(paths)
        self.reject_offer = reject_offer
        self.lab_enabled = lab_enabled
        self.clock = clock
        self.scheduler = LaneScheduler()
        self.active: dict[str, _ActiveSlot] = {}
        self.failed_until: dict[str, float] = {}
        self.lane_errors: dict[str, str] = {}
        self.successful_polls: set[str] = set()
        self._pool_lock = None
        self._lifecycle_lock = None

    def start(self) -> None:
        self.paths.ensure()
        self._lifecycle_lock = acquire_lock(self.paths.lifecycle_lock, blocking=True, shared=True)
        self._pool_lock = acquire_lock(self.paths.pool_lock)
        self.reconcile_stale_slots()

    def close(self) -> None:
        for active in list(self.active.values()):
            if active.process.poll() is None:
                active.process.terminate()
        for active in list(self.active.values()):
            try:
                active.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                active.process.kill()
                active.process.wait(timeout=5)
            self._release(active)
        self.active.clear()
        if self._pool_lock is not None:
            self._pool_lock.close()
            self._pool_lock = None
        if self._lifecycle_lock is not None:
            self._lifecycle_lock.close()
            self._lifecycle_lock = None

    def reconcile_stale_slots(self) -> None:
        ambiguous = self.paths.state / "ambiguous-jobs"
        ambiguous.mkdir(parents=True, exist_ok=True, mode=0o700)
        for state_file in self.paths.slots.glob("slot-*/state.json"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("state") != "BUSY":
                continue
            slot_id = state_file.parent.name
            try:
                held = acquire_lock(self.paths.slot_lock(slot_id))
            except PoolLockError:
                continue
            held.close()
            job_id = state.get("job_id")
            lane = state.get("lane")
            phase = state.get("phase")
            if lane in {"GPOINT", "GSALES"} and phase in {"INFERENCE_STARTED", "COMPLETING"}:
                try:
                    _validate_id(job_id, "job_identity_invalid")
                    marker = ambiguous / f"{job_id}.json"
                    marker.write_text(json.dumps({"job_id": job_id, "lane": lane, "state": "AMBIGUOUS"}))
                    marker.chmod(0o600)
                except (ValueError, OSError):
                    pass
            self._write_slot_state(slot_id, {
                "state": "AMBIGUOUS" if phase in {"INFERENCE_STARTED", "COMPLETING"} else "FAILED",
                "lane": lane if lane in LANES else None,
                "job_id": job_id if isinstance(job_id, str) else None,
                "phase": phase,
            })

    def poll_once(self, now: float | None = None) -> int:
        if self._pool_lock is None:
            self.start()
        self._reap()
        if self.paths.maintenance_stop.exists():
            self._write_status(maintenance=True)
            return 0
        launched = 0
        for slot_number in range(1, self.config.concurrency + 1):
            slot_id = f"slot-{slot_number:02d}"
            if slot_id in self.active:
                continue
            try:
                slot_lock = acquire_lock(self.paths.slot_lock(slot_id))
            except PoolLockError:
                continue
            offer = None
            lane = None
            production_error = False
            configured = set(self.claimers).intersection({"GPOINT", "GSALES"})
            for candidate in self.scheduler.production_order(configured):
                if self.failed_until.get(candidate, 0) > (self.clock() if now is None else now):
                    production_error = True
                    continue
                try:
                    offer = self.claimers[candidate]()
                    self.lane_errors.pop(candidate, None)
                    self.failed_until.pop(candidate, None)
                    self.successful_polls.add(candidate)
                except Exception as exc:
                    self.lane_errors[candidate] = _safe_error(exc)
                    delay = min(60.0, 2.0 ** min(5, len(self.lane_errors)))
                    self.failed_until[candidate] = (self.clock() if now is None else now) + delay
                    production_error = True
                    continue
                if offer:
                    lane = candidate
                    self.scheduler.record_dispatch(candidate)
                    break
            if lane is None and not production_error and self.scheduler.lab_may_run(production_claims_empty=True):
                lab_claim = self.claimers.get("LAB")
                if lab_claim is not None:
                    try:
                        offer = lab_claim()
                        self.lane_errors.pop("LAB", None)
                    except Exception as exc:
                        self.lane_errors["LAB"] = _safe_error(exc)
                        offer = None
                    if offer:
                        lane = "LAB"
            if lane is None or not isinstance(offer, dict):
                slot_lock.close()
                previous = self._read_slot_state(slot_id)
                if previous.get("state") not in {"FAILED", "AMBIGUOUS"}:
                    self._write_slot_state(slot_id, {"state": "IDLE", "lane": None, "job_id": None})
                continue
            job_id = offer.get("id") if lane != "LAB" else offer.get("job_id")
            if not isinstance(job_id, str):
                slot_lock.close()
                if self.reject_offer:
                    self.reject_offer(lane, offer, "worker_claim_invalid")
                continue
            try:
                job_lock = acquire_lock(self.paths.job_lock(job_id))
            except PoolLockError:
                slot_lock.close()
                if self.reject_offer:
                    self.reject_offer(lane, offer, "worker_duplicate_claim_blocked")
                continue
            if lane in {"GPOINT", "GSALES"} and (
                self.paths.state / "ambiguous-jobs" / f"{job_id}.json"
            ).is_file():
                job_lock.close()
                slot_lock.close()
                if self.reject_offer:
                    self.reject_offer(lane, offer, "worker_execution_ambiguous")
                self._write_slot_state(slot_id, {
                    "state": "AMBIGUOUS", "lane": lane, "job_id": job_id,
                    "phase": "AMBIGUOUS_REDELIVERY_BLOCKED",
                })
                continue
            self._write_slot_state(slot_id, {
                "state": "BUSY", "lane": lane, "job_id": job_id,
                "phase": "CLAIMED", "pid": None,
            })
            try:
                process = self.runner.start(slot_id, lane, offer, slot_lock, job_lock)
            except Exception as exc:
                self._write_slot_state(slot_id, {
                    "state": "FAILED", "lane": lane, "job_id": job_id,
                    "phase": "SLOT_START_FAILED", "error_code": _safe_error(exc),
                })
                job_lock.close()
                slot_lock.close()
                if self.reject_offer:
                    self.reject_offer(lane, offer, "worker_slot_start_failed")
                continue
            self._write_slot_state(slot_id, {
                "state": "BUSY", "lane": lane, "job_id": job_id,
                "phase": "CLAIMED", "pid": process.pid,
            })
            self.active[slot_id] = _ActiveSlot(slot_id, lane, job_id, process, slot_lock, job_lock)
            launched += 1
        self._write_status()
        return launched

    def run_forever(self) -> int:
        try:
            while True:
                self.poll_once()
                time.sleep(self.config.poll_seconds)
        finally:
            self.close()
        return 0

    def _reap(self) -> None:
        for slot_id, active in list(self.active.items()):
            code = active.process.poll()
            if code is None:
                continue
            if code != 0:
                phase = self._read_slot_state(slot_id).get("phase")
                if active.lane in {"GPOINT", "GSALES"} and phase in {"INFERENCE_STARTED", "COMPLETING"}:
                    directory = self.paths.state / "ambiguous-jobs"
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    marker = directory / f"{active.job_id}.json"
                    marker.write_text(json.dumps({"job_id": active.job_id, "lane": active.lane, "state": "AMBIGUOUS"}))
                    marker.chmod(0o600)
                self._write_slot_state(slot_id, {
                    "state": "FAILED", "lane": active.lane, "job_id": active.job_id,
                    "phase": "CHILD_EXITED", "exit_code": code,
                })
            else:
                state = self._read_slot_state(slot_id)
                if state.get("state") == "BUSY":
                    state.update({"state": "IDLE", "phase": "COMPLETE", "job_id": None, "lane": None, "pid": None})
                    self._write_slot_state(slot_id, state)
            self._release(active)
            del self.active[slot_id]

    def _release(self, active: _ActiveSlot) -> None:
        active.job_lock.close()
        active.slot_lock.close()

    def _read_slot_state(self, slot_id: str) -> dict[str, Any]:
        try:
            return json.loads((self.paths.slot_dir(slot_id) / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_slot_state(self, slot_id: str, state: dict[str, Any]) -> None:
        directory = self.paths.slot_dir(slot_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_file = directory / "state.json"
        payload = json.dumps({**state, "updated_at": datetime.now(UTC).isoformat()}, separators=(",", ":"))
        fd, temporary = tempfile.mkstemp(prefix=".slot-state-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, state_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _write_status(self, maintenance: bool = False) -> None:
        lanes: dict[str, dict[str, Any]] = {}
        for lane in LANES:
            active = [item for item in self.active.values() if item.lane == lane]
            error = self.lane_errors.get(lane)
            approval_block = lane == "LAB" and error == "lab_approval_required"
            lab_disabled = lane == "LAB" and self.lab_enabled is False
            lane_state = (
                "AVAILABLE" if approval_block else
                "UNCONFIGURED" if lane == "GSALES" and lane not in self.claimers else
                "DISABLED" if lane == "LAB" and (lane not in self.claimers or lab_disabled) else
                "AVAILABLE" if lane == "LAB" else
                "DEGRADED" if error else
                "RUNNING" if active else "HEALTHY"
            )
            lanes[lane] = {
                "state": lane_state,
                "queue": None,
                "running": len(active),
                "configured": lane in self.claimers or lane == "LAB",
                **({"enabled": self.lab_enabled} if lane == "LAB" and self.lab_enabled is not None else {}),
                "last_error_code": error,
                **({"inference_gate": "CLOSED_DISABLED"} if lab_disabled else
                   {"inference_gate": "APPROVAL_REQUIRED"} if approval_block else
                   {"inference_gate": "REQUIRED_BEFORE_INFERENCE"} if lane == "LAB" else {}),
            }
        slot_states = {f"slot-{number:02d}": self._read_slot_state(f"slot-{number:02d}")
                       for number in range(1, self.config.concurrency + 1)}
        busy = len(self.active)
        pool_state = "MAINTENANCE" if maintenance else "DEGRADED" if any(
            not (lane == "LAB" and error == "lab_approval_required")
            for lane, error in self.lane_errors.items()
        ) else "HEALTHY"
        write_pool_status(self.paths, {
            "pool": {"state": pool_state, "total": self.config.concurrency,
                     "busy": busy, "idle": self.config.concurrency - busy,
                     "failed": sum(state.get("state") == "FAILED" for state in slot_states.values())},
            "provider": {
                "state": "HEALTHY" if self.config.concurrency == 1 else "VERIFIED",
                "parallel_capability": self.config.provider_parallel_capability,
            },
            "configured_concurrency": self.config.concurrency,
            "lanes": lanes,
            "slots": slot_states,
        })
        self._write_legacy_status(lanes, slot_states, pool_state)

    def _write_legacy_status(self, lanes: dict[str, dict[str, Any]],
                             slots: dict[str, dict[str, Any]], pool_state: str) -> None:
        previous: dict[str, Any] = {}
        try:
            previous = json.loads(self.paths.worker_status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        targets = previous.get("targets") if isinstance(previous.get("targets"), dict) else {}
        for lane in LANES:
            saved = targets.get(lane) if isinstance(targets.get(lane), dict) else {}
            target_status = {**saved, "pool_state": lanes[lane]["state"]}
            if lane in self.successful_polls:
                target_status["last_error_code"] = self.lane_errors.get(lane)
            targets[lane] = target_status
        payload = {
            **{key: previous[key] for key in (
                "last_poll_at", "last_heartbeat_at", "last_success_at", "last_success_job_id",
                "last_network_recovery_at", "consecutive_network_failures",
            ) if key in previous},
            "worker_version": "4",
            "configured_concurrency": self.config.concurrency,
            "provider_parallel_capability": self.config.provider_parallel_capability,
            "enabled": bool(os.environ.get("GPOINT_AGATA_WORKER_TOKEN")),
            "launch_agent_loaded": previous.get("launch_agent_loaded", False),
            "current_job_present": any(state.get("state") == "BUSY" for state in slots.values()),
            "state": pool_state.lower(),
            "last_error_code": next(iter(self.lane_errors.values()), None),
            "queue_connectivity": "reachable" if pool_state == "HEALTHY" else "degraded",
            "targets": targets,
        }
        self.paths.status.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".worker-status-", dir=self.paths.status)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.paths.worker_status_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _safe_error(error: Exception) -> str:
    value = str(error)
    return value[:64] if value.replace("_", "").isalnum() else "worker_lane_unavailable"
