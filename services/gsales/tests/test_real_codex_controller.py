"""Offline safeguards for the one-shot installed LaunchAgent controller."""
import importlib.util
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/run-gsales-one-shot-real-codex.py"
spec = importlib.util.spec_from_file_location("real_codex_controller", SCRIPT)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def import_postgres_runner(monkeypatch):
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        psycopg_stub = types.ModuleType("psycopg")
        psycopg_stub.sql = types.ModuleType("psycopg.sql")
        monkeypatch.setitem(sys.modules, "psycopg", psycopg_stub)
    spec = importlib.util.spec_from_file_location("run_postgres", ROOT / "services/gsales/tests/run_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def authoritative_snapshot(**overrides):
    api_image_id = "sha256:940236e984cff5e0123af62c58cea6ae5986fe879ca219daa2c91ff6ca8620e2"
    agata_image_id = "sha256:61299c506ed77971df0f75cb64cb91f739644ebbddfafc21d5cb903c4488dbb2"
    release_sha = "68bf0212d7aa5125394b0b39ffba9226eb106c0d"
    snapshot = {
        "snapshot_at": "2026-10-04T07:00:00.000000Z",
        "observed_monotonic": 100.0,
        "source_id": f"gpoint-vps:{release_sha}:api={api_image_id}:agata={agata_image_id}",
        "source_system": "gpoint",
        "database_name": "gpoint",
        "release_sha": release_sha,
        "api_build_sha": release_sha,
        "agata_build_sha": release_sha,
        "api_image_id": api_image_id,
        "agata_image_id": agata_image_id,
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "active_gpoint_profiles": 1,
        "tenant_identity_verified": True,
        "transaction_read_only": True,
        "same_login": True,
        "role_superuser": False,
        "role_bypassrls": False,
        "visible_tenant_rows": 1,
        "pending_inference_jobs": 0,
        "running_inference_jobs": 0,
        "effective_inference_ttl_seconds": 420,
    }
    return snapshot | overrides


def test_restore_bootstraps_original_and_waits_for_poll(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for path in paths:
        path.write_text("original:" + path.name)
    hashes = {str(path): controller.digest(path) for path in paths}
    monkeypatch.setattr(controller, "PLIST", paths[0])
    monkeypatch.setattr(controller, "loaded", lambda: state["loaded"])
    state = {"loaded": False, "calls": []}

    def run(args, **kwargs):
        state["calls"].append(args[0])
        state["loaded"] = True
        return subprocess.CompletedProcess(args, 0)

    poll_state = {"count": 0}
    def status():
        poll_state["count"] += 1
        return {
            "last_poll_at": str(poll_state["count"]),
            "updated_at": str(poll_state["count"]),
            "state": "idle",
            "current_job_present": False,
            "last_error_code": None,
        }
    monkeypatch.setattr(controller.subprocess, "run", run)
    monkeypatch.setattr(controller, "status", status)
    monkeypatch.setattr(controller.time, "sleep", lambda _seconds: None)
    controller.restore(hashes, stopped=True)
    assert state["calls"] == ["launchctl"]
    assert controller.loaded()
    assert all(controller.digest(path) == hashes[str(path)] for path in paths)


def test_restore_rejects_changed_v2_artifact_before_bootstrap(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for path in paths:
        path.write_text("original:" + path.name)
    hashes = {str(path): controller.digest(path) for path in paths}
    paths[1].write_text("changed worker")
    monkeypatch.setattr(controller, "PLIST", paths[0])
    monkeypatch.setattr(controller, "WORKER", paths[1])
    monkeypatch.setattr(controller, "WRAPPER", paths[2])
    calls = []
    state = {"loaded": False}
    monkeypatch.setattr(controller, "loaded", lambda: state["loaded"])
    def run(args, **_kwargs):
        calls.append(args)
        state["loaded"] = True
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(controller.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="installed_worker_hash_changed"):
        controller.restore(hashes, stopped=True)
    assert calls == []


def test_preflight_fails_closed_when_queue_snapshot_is_missing(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for path in paths:
        path.write_text("fixture")
    paths[0].write_bytes(plistlib.dumps({"Label": controller.LABEL, "RunAtLoad": True, "KeepAlive": True}))
    monkeypatch.setattr(controller, "PLIST", paths[0])
    monkeypatch.setattr(controller, "WORKER", paths[1])
    monkeypatch.setattr(controller, "WRAPPER", paths[2])
    monkeypatch.setattr(controller, "PYTHON", paths[1])
    monkeypatch.setattr(controller.subprocess, "check_output", lambda *a, **k: controller.PREPARATION_BASE_SHA + "\n")
    original_run = controller.subprocess.run
    def run(args, **kwargs):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, **kwargs)
    monkeypatch.setattr(controller.subprocess, "run", run)
    monkeypatch.setattr(controller, "loaded", lambda: True)
    monkeypatch.setattr(controller, "queue_snapshot", lambda: {"state": "idle", "current_job_present": False})
    monkeypatch.setattr(controller, "status", lambda: {"launch_agent_loaded": True, "state": "idle", "current_job_present": False, "last_poll_at": "2099-01-01T00:00:00+00:00", "queue_connectivity": "reachable"})
    with pytest.raises(RuntimeError, match="queue_snapshot_runtime_identity_mismatch"):
        controller.preflight(controller.PREPARATION_BASE_SHA)


def test_authoritative_snapshot_requires_empty_gpoint_queue_and_ttl_margin():
    snapshot = authoritative_snapshot()
    controller.validate_queue_snapshot(snapshot, now_monotonic=100.1)
    with pytest.raises(RuntimeError, match="gpoint_queue_not_empty"):
        controller.validate_queue_snapshot(authoritative_snapshot(pending_inference_jobs=1), now_monotonic=100.1)
    with pytest.raises(RuntimeError, match="effective_inference_ttl_below_outage_budget"):
        controller.validate_queue_snapshot(authoritative_snapshot(effective_inference_ttl_seconds=419), now_monotonic=100.1)


def test_authoritative_snapshot_rejects_wrong_runtime_or_untrusted_db_role():
    for override in (
        {"source_system": "gsales"},
        {"database_name": "other"},
        {"api_build_sha": "a" * 40},
        {"tenant_identity_verified": False},
        {"transaction_read_only": False},
        {"same_login": False},
        {"role_superuser": True},
        {"role_bypassrls": True},
        {"visible_tenant_rows": 0},
        {"tenant_id": "7508f36f-774a-48aa-8c6b-42f6f160415d"},
        {"active_gpoint_profiles": 0},
        {"release_sha": "8c30d676809f8c0f6d3254891bf096b0b8740bd4"},
        {"api_image_id": "sha256:" + "a" * 64},
    ):
        with pytest.raises(RuntimeError):
            controller.validate_queue_snapshot(authoritative_snapshot(**override), now_monotonic=100.1)


def test_candidate_sha_must_be_full_and_match_current_head():
    candidate = "a" * 40
    assert controller.validate_candidate_sha(candidate, candidate) == candidate
    for invalid in ("a" * 39, "a" * 40 + "b", "A" * 40, "branch-name"):
        with pytest.raises(RuntimeError, match="candidate_sha_mismatch"):
            controller.validate_candidate_sha(invalid, candidate)
    with pytest.raises(RuntimeError, match="candidate_sha_mismatch"):
        controller.validate_candidate_sha("b" * 40, candidate)


def test_authoritative_snapshot_must_be_recent_at_worker_recheck():
    with pytest.raises(RuntimeError, match="queue_snapshot_stale_before_switch"):
        controller.validate_queue_snapshot(authoritative_snapshot(), now_monotonic=106.0)


def test_outage_budget_reserves_bootout_and_confirmed_restore_time():
    # 15 seconds already spent booting out; keep 10 seconds for process
    # termination and 60 seconds for bootstrap plus polling confirmation.
    assert controller.remaining_test_budget_seconds(outage_deadline=400.0, now_monotonic=115.0) == 215.0


def test_immediate_switch_preflight_reads_queue_then_rechecks_local_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller, "queue_snapshot",
        lambda: calls.append("queue") or authoritative_snapshot(observed_monotonic=controller.time.monotonic()),
    )
    monkeypatch.setattr(controller, "assert_worker_idle", lambda: calls.append("local-idle"))
    result = controller.immediate_switch_preflight()
    assert result["pending_inference_jobs"] == 0
    assert calls == ["queue", "local-idle"]


def test_restore_confirmation_requires_fresh_idle_poll_without_error():
    before = {"last_poll_at": "poll-1", "updated_at": "update-1"}
    assert controller.authenticated_poll_confirmed(
        {"last_poll_at": "poll-2", "state": "idle", "current_job_present": False, "last_error_code": None},
        before,
    )
    assert not controller.authenticated_poll_confirmed(
        {"last_poll_at": "poll-1", "state": "idle", "current_job_present": False, "last_error_code": None},
        before,
    )
    assert not controller.authenticated_poll_confirmed(
        {"last_poll_at": "poll-2", "state": "idle", "current_job_present": False, "last_error_code": "AUTH"},
        before,
    )


def test_watchdog_recovers_when_controller_process_disappears(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    calls = []
    result = controller.run_recovery_watchdog(
        path,
        now_fn=lambda: 101.0,
        sleep_fn=lambda _seconds: None,
        parent_exists_fn=lambda _pid: False,
        restore_fn=lambda *args, **kwargs: calls.append("restore"),
    )
    assert result == 0
    assert calls == ["restore"]
    assert controller.read_recovery_state(path)["restore_confirmed"] is True


def test_watchdog_stops_recorded_qa_process_before_restoring(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    controller._record_qa_pid(path, 123)
    calls = []
    assert controller.run_recovery_watchdog(
        path,
        now_fn=lambda: 101.0,
        sleep_fn=lambda _seconds: None,
        parent_exists_fn=lambda _pid: False,
        terminate_qa_fn=lambda state: calls.append(("terminate", state["qa_pid"])),
        restore_fn=lambda *_a, **_k: calls.append(("restore", None)),
    ) == 0
    assert calls == [("terminate", 123), ("restore", None)]


def test_watchdog_starts_recovery_by_240_seconds_if_controller_is_alive(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    clock = {"now": 100.0}
    calls = []
    def sleep(seconds):
        clock["now"] += seconds
    result = controller.run_recovery_watchdog(
        path,
        now_fn=lambda: clock["now"],
        sleep_fn=sleep,
        parent_exists_fn=lambda _pid: True,
        restore_fn=lambda *args, **kwargs: calls.append(clock["now"]),
    )
    assert result == 0
    assert calls and calls[0] <= 100.0 + controller.RECOVERY_START_AFTER_SECONDS


def test_watchdog_gives_active_controller_restore_lock_time_before_takeover(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    clock = {"now": 100.0 + controller.RECOVERY_START_AFTER_SECONDS}
    restore_attempts = {"count": 0}
    forced = []
    def fake_restore_once(state_path, **kwargs):
        restore_attempts["count"] += 1
        if restore_attempts["count"] <= 3:
            raise TimeoutError("recovery_lock_busy")
        return controller.restore_once(
            state_path,
            source=kwargs["source"],
            restore_fn=lambda *_a, **_k: None,
        )
    def sleep(_seconds):
        clock["now"] += 10.0
    result = controller.run_recovery_watchdog(
        path,
        now_fn=lambda: clock["now"],
        sleep_fn=sleep,
        parent_exists_fn=lambda _pid: True,
        restore_once_fn=fake_restore_once,
        terminate_controller_fn=lambda _state: forced.append(clock["now"]),
        terminate_qa_fn=lambda _state: None,
    )
    assert result == 0
    assert forced == [100.0 + controller.RECOVERY_START_AFTER_SECONDS + 20.0]


def test_watchdog_is_idempotent_after_successful_controller_restore(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    controller.restore_once(path, source="controller", restore_fn=lambda *_a, **_k: None)
    calls = []
    assert controller.run_recovery_watchdog(
        path,
        now_fn=lambda: 500.0,
        sleep_fn=lambda _seconds: None,
        parent_exists_fn=lambda _pid: False,
        restore_fn=lambda *_a, **_k: calls.append("restore"),
    ) == 0
    assert calls == []


def test_detached_watchdog_starts_and_exits_for_cancelled_session(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    controller._cancel_recovery_session(path)
    process = controller.start_recovery_watchdog(path)
    controller.require_watchdog_success(process)
    assert controller.read_recovery_state(path)["watchdog_pid"] == process.pid


def test_controller_and_watchdog_restoration_are_serialized(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    marker = tmp_path / "restore-calls.txt"
    child = (
        "import importlib.util, pathlib, sys, time; "
        "spec=importlib.util.spec_from_file_location('recovery', sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "marker=pathlib.Path(sys.argv[3]); "
        "def_restore=lambda *a, **k: (marker.open('a').write('restore\\n'), time.sleep(.15)); "
        "module.restore_once(pathlib.Path(sys.argv[2]), source='controller', restore_fn=def_restore)"
    )
    first = subprocess.Popen([sys.executable, "-c", child, str(SCRIPT), str(path), str(marker)])
    second = subprocess.Popen([sys.executable, "-c", child, str(SCRIPT), str(path), str(marker)])
    assert first.wait(timeout=5) == 0
    assert second.wait(timeout=5) == 0
    assert marker.read_text().splitlines() == ["restore"]


def test_guarded_window_restores_after_bootout_exception():
    calls = []
    def bootout():
        calls.append("bootout")
        raise subprocess.CalledProcessError(1, ["launchctl", "bootout"])
    with pytest.raises(subprocess.CalledProcessError):
        controller.run_guarded_window(
            bootout_fn=bootout,
            launch_qa_fn=lambda: pytest.fail("QA must not launch after bootout failure"),
            restore_fn=lambda: calls.append("restore"),
            outage_deadline=500.0,
            now_fn=lambda: 100.0,
        )
    assert calls == ["bootout", "restore"]


def test_guarded_window_terminates_qa_before_restore_on_interrupt(monkeypatch):
    calls = []
    class Process:
        def __init__(self): self.running = True
        def wait(self, timeout=None): raise KeyboardInterrupt()
        def poll(self): return None if self.running else 0
    process = Process()
    monkeypatch.setattr(controller, "terminate_process_tree", lambda proc: (calls.append("terminate"), setattr(proc, "running", False)))
    with pytest.raises(KeyboardInterrupt):
        controller.run_guarded_window(
            bootout_fn=lambda: calls.append("bootout"),
            launch_qa_fn=lambda: process,
            restore_fn=lambda: calls.append("restore"),
            outage_deadline=500.0,
            now_fn=lambda: 100.0,
        )
    assert calls == ["bootout", "terminate", "restore"]


def test_guarded_window_success_completes_qa_then_confirms_restore():
    calls = []
    class Process:
        pid = 123
        def wait(self, timeout=None):
            calls.append("qa-complete")
            return 0
        def poll(self): return 0
    result = controller.run_guarded_window(
        bootout_fn=lambda: calls.append("bootout"),
        launch_qa_fn=lambda: calls.append("launch-two-sequential-jobs") or Process(),
        restore_fn=lambda: calls.append("restore-and-poll"),
        outage_deadline=500.0,
        now_fn=lambda: 100.0,
        record_qa_fn=lambda pid: calls.append(f"qa-pid-{pid}"),
    )
    assert result == 0
    assert calls == ["bootout", "launch-two-sequential-jobs", "qa-pid-123", "qa-complete", "qa-pid-None", "restore-and-poll"]


def test_guarded_window_timeout_terminates_qa_before_restoration(monkeypatch):
    calls = []
    class Process:
        pid = 123
        def wait(self, timeout=None): raise subprocess.TimeoutExpired(["codex"], timeout)
        def poll(self): return 0
    process = Process()
    monkeypatch.setattr(controller, "terminate_process_tree", lambda _proc: calls.append("terminate-qa-group"))
    result = controller.run_guarded_window(
        bootout_fn=lambda: calls.append("bootout"),
        launch_qa_fn=lambda: process,
        restore_fn=lambda: calls.append("restore-and-poll"),
        outage_deadline=500.0,
        now_fn=lambda: 100.0,
    )
    assert result == 124
    assert calls == ["bootout", "terminate-qa-group", "restore-and-poll"]


def test_terminate_process_tree_stops_a_hung_codex_child():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    controller.terminate_process_tree(process)
    assert process.poll() is not None


def test_insufficient_budget_fails_before_bootout():
    calls = []
    with pytest.raises(RuntimeError, match="insufficient_test_budget"):
        controller.run_guarded_window(
            bootout_fn=lambda: calls.append("bootout"),
            launch_qa_fn=lambda: pytest.fail("QA must not launch with no reserved recovery budget"),
            restore_fn=lambda: calls.append("restore"),
            outage_deadline=100.0,
            now_fn=lambda: 100.0,
        )
    assert calls == []


def test_restore_bootstrap_error_propagates_without_marking_recovered(monkeypatch, tmp_path):
    artifacts = [tmp_path / name for name in ("plist", "worker", "wrapper")]
    for artifact in artifacts:
        artifact.write_text("v2:" + artifact.name)
    monkeypatch.setattr(controller, "PLIST", artifacts[0])
    monkeypatch.setattr(controller, "WORKER", artifacts[1])
    monkeypatch.setattr(controller, "WRAPPER", artifacts[2])
    hashes = {str(artifact): controller.digest(artifact) for artifact in artifacts}
    path = controller.create_recovery_session(tmp_path, hashes, parent_pid=99)
    monkeypatch.setattr(controller, "loaded", lambda: False)
    monkeypatch.setattr(controller.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["launchctl", "bootstrap"])))
    with pytest.raises(subprocess.CalledProcessError):
        controller.restore_once(path, source="controller")
    assert controller.read_recovery_state(path)["restore_confirmed"] is False


def test_restore_rejects_state_that_does_not_name_exact_v2_artifacts(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99)
    with pytest.raises(RuntimeError, match="recovery_target_identity_invalid"):
        controller.restore_once(path, source="controller")
    assert controller.read_recovery_state(path)["restore_confirmed"] is False


def test_watchdog_does_not_claim_recovery_after_authentication_error(tmp_path):
    path = controller.create_recovery_session(tmp_path, {}, parent_pid=99, now=100.0)
    def failed_restore(*_args, **_kwargs): raise RuntimeError("worker_authentication_failed")
    assert controller.run_recovery_watchdog(
        path,
        now_fn=lambda: 400.0,
        sleep_fn=lambda _seconds: None,
        parent_exists_fn=lambda _pid: False,
        restore_fn=failed_restore,
    ) == 1
    state = controller.read_recovery_state(path)
    assert state["restore_confirmed"] is False
    assert state["last_recovery_error"] == "worker_authentication_failed"


def test_recovery_error_redacts_secret_shaped_exception_text():
    secret_shaped = "ab12cd34ef56" * 4
    assert controller._safe_recovery_error(RuntimeError(secret_shaped)) == "runtimeerror"
    assert controller._safe_recovery_error(RuntimeError("worker_authentication_failed")) == "worker_authentication_failed"


def test_controller_installs_sigint_and_sigterm_cleanup_handlers(monkeypatch):
    installed = {}
    monkeypatch.setattr(controller.signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
    controller.install_controller_signal_handlers()
    assert set(installed) == {controller.signal.SIGINT, controller.signal.SIGTERM}
    for handler in installed.values():
        with pytest.raises(KeyboardInterrupt):
            handler(0, None)


def test_disposable_postgres_runner_converts_termination_to_cleanup(monkeypatch):
    module = import_postgres_runner(monkeypatch)
    installed = {}
    monkeypatch.setattr(module.signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
    module.install_cleanup_signal_handlers()
    assert set(installed) == {module.signal.SIGINT, module.signal.SIGTERM}
    with pytest.raises(KeyboardInterrupt):
        installed[module.signal.SIGTERM](module.signal.SIGTERM, None)


def test_postgres_container_start_keeps_password_out_of_argv_and_errors(monkeypatch):
    module = import_postgres_runner(monkeypatch)
    password = "synthetic-disposable-password"
    observed = {}
    def failed_docker(args, **kwargs):
        observed["args"] = args
        env_path = Path(args[args.index("--env-file") + 1])
        observed["mode"] = env_path.stat().st_mode & 0o777
        observed["contents"] = env_path.read_text()
        return subprocess.CompletedProcess(args, 1, "", "synthetic docker failure")
    monkeypatch.setattr(module.subprocess, "run", failed_docker)
    with pytest.raises(RuntimeError, match="disposable_postgres_start_failed") as error:
        module.start_test_container("gpoint-test-container", password)
    assert password not in str(error.value)
    assert all(password not in str(arg) for arg in observed["args"])
    assert observed["mode"] == 0o600
    assert observed["contents"] == "POSTGRES_PASSWORD=" + password + "\n"
    assert not Path(observed["args"][observed["args"].index("--env-file") + 1]).exists()


def test_postgres_runner_timing_phase_reports_pass_and_failure(monkeypatch, capsys):
    module = import_postgres_runner(monkeypatch)
    ticks = iter((10.0, 12.5, 20.0, 23.0, 26.0, 29.0))
    monkeypatch.setattr(module, "monotonic_seconds", lambda: next(ticks))

    with module.timed_phase("database_setup"):
        pass
    with pytest.raises(RuntimeError, match="safe_failure"):
        with module.timed_phase("migration_replay"):
            raise RuntimeError("safe_failure")

    nonzero = subprocess.CompletedProcess(["pytest"], 17)
    monkeypatch.setattr(module, "run_process", lambda *_args, **_kwargs: nonzero)
    result = module.timed_process("failing_pytest", ["pytest"])
    assert result.returncode == 17

    assert capsys.readouterr().out.splitlines() == [
        "GS_POSTGRES_PHASE=database_setup STATUS=PASS SECONDS=2.500",
        "GS_POSTGRES_PHASE=migration_replay STATUS=FAIL SECONDS=3.000",
        "GS_POSTGRES_PHASE=failing_pytest STATUS=FAIL SECONDS=3.000",
    ]
