from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts.agata.launch_agent_lifecycle import LaunchAgentController
from scripts.agata.launch_agent_lifecycle import DEFAULT_OPERATION_LOCK


def test_waits_for_service_removal_and_old_pid_exit_before_bootstrap(tmp_path: Path) -> None:
    events: list[str] = []
    service_present = True
    service_has_pid = True
    old_pid_alive = True

    class Runner:
        def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal service_present, service_has_pid
            operation = command[1]
            events.append(operation)
            if operation == "print":
                if service_present:
                    state = "pid = 4321\n" if service_has_pid else "state = waiting\n"
                    return subprocess.CompletedProcess(command, 0, state, "")
                return subprocess.CompletedProcess(command, 113, "", "Could not find service")
            if operation == "bootout":
                return subprocess.CompletedProcess(command, 0, "", "")
            if operation == "bootstrap":
                assert not service_present
                assert not old_pid_alive
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(operation)

    def wait_for_poll(_: float) -> None:
        nonlocal service_present, service_has_pid, old_pid_alive
        events.append("poll")
        if old_pid_alive:
            old_pid_alive = False
            service_has_pid = False
        else:
            service_present = False

    controller = LaunchAgentController(
        lock_path=tmp_path / "operation.lock",
        receipt_path=tmp_path / "receipt.jsonl",
        runner=Runner(),
        pid_is_alive=lambda pid: pid == 4321 and old_pid_alive,
        wait=wait_for_poll,
        timeout_seconds=1,
    )

    controller.replace_plist(
        domain="gui/501",
        label="one.gpoint.agata-codex-worker",
        plist_path=tmp_path / "agent.plist",
        candidate=b"candidate",
    )

    assert events.index("bootout") < events.index("poll") < events.index("bootstrap")
    assert events.count("poll") == 2


def test_error_37_is_recorded_and_rollback_waits_before_bootstrap(tmp_path: Path) -> None:
    calls: list[str] = []
    loaded = True
    running_pid = 9876
    alive_pids = {9876}
    candidate_attempted = False

    class Runner:
        def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal loaded, candidate_attempted, running_pid
            operation = command[1]
            calls.append(operation)
            if operation == "print":
                if loaded:
                    return subprocess.CompletedProcess(command, 0, f"pid = {running_pid}\n", "")
                return subprocess.CompletedProcess(command, 113, "", "Could not find service")
            if operation == "bootout":
                loaded = False
                return subprocess.CompletedProcess(command, 0, "", "")
            if operation == "bootstrap":
                if not candidate_attempted:
                    candidate_attempted = True
                    loaded = True
                    running_pid = 8765
                    alive_pids.add(8765)
                    return subprocess.CompletedProcess(
                        command,
                        37,
                        "",
                        "Operation already in progress\nGITHUB_TOKEN = ghp_123456789012345678901234567890123456",
                    )
                assert not loaded
                assert not alive_pids
                loaded = True
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(operation)

    def wait_for_poll(_: float) -> None:
        nonlocal loaded
        calls.append("poll")
        loaded = False
        alive_pids.clear()

    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(b"original")
    controller = LaunchAgentController(
        lock_path=tmp_path / "operation.lock",
        receipt_path=tmp_path / "receipt.jsonl",
        runner=Runner(),
        pid_is_alive=lambda pid: pid in alive_pids,
        wait=wait_for_poll,
        timeout_seconds=1,
    )

    with pytest.raises(RuntimeError, match="error 37") as raised:
        controller.replace_plist(
            domain="gui/501",
            label="one.gpoint.agata-codex-worker",
            plist_path=plist_path,
            candidate=b"candidate",
        )

    assert plist_path.read_bytes() == b"original"
    entries = [json.loads(line) for line in (tmp_path / "receipt.jsonl").read_text().splitlines()]
    failed = next(entry for entry in entries if entry["stage"] == "candidate_bootstrap")
    assert failed["exit_code"] == 37
    assert failed["stderr"].startswith("Operation already in progress")
    assert "ghp_" not in failed["stderr"]
    assert "[REDACTED]" in failed["stderr"]
    assert "ghp_" not in str(raised.value)
    assert failed["timestamp"]
    assert failed["command_description"]
    candidate_index = calls.index("bootstrap")
    rollback_bootstrap = max(index for index, call in enumerate(calls) if call == "bootstrap")
    assert "print" in calls[candidate_index + 1:rollback_bootstrap]
    assert "poll" in calls[candidate_index + 1:rollback_bootstrap]


def test_lifecycle_operations_share_exclusive_lock(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    second_bootstrap = threading.Event()

    class Runner:
        count = 0

        def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.count += 1
            if command[1] == "print":
                return subprocess.CompletedProcess(command, 113, "", "Could not find service")
            if command[1] == "bootstrap":
                if started.is_set():
                    second_bootstrap.set()
                if not started.is_set():
                    started.set()
                    release.wait(timeout=2)
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = Runner()
    kwargs = dict(
        lock_path=tmp_path / "operation.lock",
        receipt_path=tmp_path / "receipt.jsonl",
        runner=runner,
        pid_is_alive=lambda _: False,
        timeout_seconds=1,
        lock_timeout_seconds=2,
        wait=time.sleep,
    )
    first = LaunchAgentController(**kwargs)
    second = LaunchAgentController(**kwargs)

    first_thread = threading.Thread(target=lambda: first.activate("gui/501", "label", tmp_path / "first.plist"))
    second_thread = threading.Thread(target=lambda: second.activate("gui/501", "label", tmp_path / "second.plist"))
    first_thread.start()
    assert started.wait(timeout=1)
    second_thread.start()
    assert not second_bootstrap.wait(timeout=0.1)
    release.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_bootstrap.is_set()


def test_wait_for_unload_is_bounded_and_does_not_bootstrap_on_timeout(tmp_path: Path) -> None:
    now = 0.0
    calls: list[str] = []

    class Runner:
        def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            operation = command[1]
            calls.append(operation)
            if operation == "print":
                return subprocess.CompletedProcess(command, 0, "pid = 5555\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    controller = LaunchAgentController(
        lock_path=tmp_path / "operation.lock",
        receipt_path=tmp_path / "receipt.jsonl",
        runner=Runner(),
        pid_is_alive=lambda _: True,
        monotonic=lambda: now,
        wait=advance,
        timeout_seconds=0.3,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(TimeoutError, match="removal and old worker exit"):
        controller.replace_plist(
            domain="gui/501",
            label="one.gpoint.agata-codex-worker",
            plist_path=tmp_path / "agent.plist",
            candidate=b"candidate",
        )

    assert "bootout" in calls
    assert "bootstrap" not in calls
    entries = [json.loads(line) for line in (tmp_path / "receipt.jsonl").read_text().splitlines()]
    assert any(entry["stage"].endswith("_timeout") and entry["exit_code"] == 124 for entry in entries)



def test_codex_installer_remains_fail_closed_without_owner_transition_approval() -> None:
    from services.agata.worker import install_codex_launch_agent

    with pytest.raises(RuntimeError, match="owner_approved_transition_controller_required"):
        install_codex_launch_agent.install()
    with pytest.raises(RuntimeError, match="explicit_rollback_controller_required"):
        install_codex_launch_agent.remove()


def test_lifecycle_controller_shares_v3_transition_lock() -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[3] / "scripts/transition-agata-worker-v3.py"
    spec = importlib.util.spec_from_file_location("agata_v3_transition_for_lock_check", script)
    assert spec and spec.loader
    transition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transition)

    assert DEFAULT_OPERATION_LOCK == transition.TRANSITION_LOCK
