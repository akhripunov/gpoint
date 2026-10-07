"""Migration safety contracts; no LaunchAgent or installed files are changed."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "worker/migrate_worker_runtime.py"
SPEC = importlib.util.spec_from_file_location("worker_runtime_migration", MODULE_PATH)
migration = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(migration)


def test_status_inventory_projection_omits_secrets_and_job_payloads(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({
        "state": "idle", "current_job_present": False, "last_poll_at": "now",
        "token": "secret", "request": {"prompt": "private"},
        "targets": {"GSALES": {"state": "idle", "last_poll_at": "then", "response": "private"},
                    "IGNORE": {"state": "running"}},
    }))
    safe = migration.safe_worker_status(path)
    assert safe == {
        "state": "idle", "current_job_present": False, "last_poll_at": "now",
        "targets": {"GSALES": {"state": "idle", "last_poll_at": "then"}},
    }


def test_canonical_installer_includes_exact_worker_bundle_assets():
    worker_dir = MODULE_PATH.parent
    expected = (
        "gpoint_mac_worker.py",
        "agata_worker_pool.py",
        "lab_queue.py",
        "lab_fixture_manifest.json",
        "lab_promptfoo_compat_manifest.json",
        "run_codex_worker.sh",
        "requirements.txt",
    )

    wrapper_path = worker_dir / "install_codex_launch_agent.py"
    wrapper_spec = importlib.util.spec_from_file_location("worker_install_wrapper", wrapper_path)
    wrapper = importlib.util.module_from_spec(wrapper_spec)
    assert wrapper_spec and wrapper_spec.loader
    wrapper_spec.loader.exec_module(wrapper)

    worker_tree = ast.parse((worker_dir / "gpoint_mac_worker.py").read_text(encoding="utf-8"))
    bundle_function = next(
        node for node in worker_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_bundle_sha256"
    )
    bundle_loop = next(node for node in bundle_function.body if isinstance(node, ast.For))
    bundle_names = tuple(item.elts[0].value for item in bundle_loop.iter.elts)

    assert migration.ASSETS == expected
    assert tuple(path.name for path in wrapper.SOURCES) == expected
    assert bundle_names == expected


def test_candidate_launch_diagnostics_survive_legacy_tree_rollback(tmp_path, monkeypatch):
    root = tmp_path / "Library/Application Support/GPoint/agata-codex-worker"
    root.mkdir(parents=True)
    legacy = tmp_path / "rollback/legacy-runtime"
    legacy.mkdir(parents=True)
    (legacy / "worker.py").write_text("legacy")
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)

    attempt_id = "attempt-123"
    diagnostics = migration._create_diagnostic_attempt("a" * 40, attempt_id)
    (diagnostics / "candidate.stdout.log").write_text("candidate stdout\n")
    (diagnostics / "candidate.stderr.log").write_text("worker_initialization_failed\n")
    receipt = migration._write_diagnostic_evidence(
        diagnostics,
        manifest={"source_sha": "a" * 40, "bundle_sha256": "b" * 64},
        attempt_id=attempt_id,
        rendered_plist=b"plist",
        launchctl_print="state = spawn scheduled\nlast exit code = 78: EX_CONFIG\n",
        process_evidence={"pid": None, "command": ""},
        command_evidence={"returncode": 78},
        error=ValueError("worker_concurrency_invalid"),
    )

    migration._restore_legacy_snapshot(legacy)

    assert receipt.is_file()
    assert (diagnostics / "candidate.stderr.log").read_text() == "worker_initialization_failed\n"
    assert receipt.stat().st_mode & 0o077 == 0
    assert diagnostics.stat().st_mode & 0o777 == 0o700
    assert (diagnostics / "candidate.stderr.log").stat().st_mode & 0o777 == 0o600


def test_candidate_diagnostic_redacts_secret_bearing_output_and_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "support/agata-codex-worker")
    diagnostics = migration._create_diagnostic_attempt("c" * 40, "attempt-secret")
    (diagnostics / "candidate.stdout.log").write_text("Authorization: Bearer top-secret-token\n")
    (diagnostics / "candidate.stderr.log").write_text("ValueError: top-secret-token\n")

    receipt = migration._write_diagnostic_evidence(
        diagnostics,
        manifest={"source_sha": "c" * 40, "bundle_sha256": "d" * 64},
        attempt_id="attempt-secret",
        rendered_plist=b"plist",
        launchctl_print="environment = { GPOINT_AGATA_WORKER_TOKEN => top-secret-token }",
        process_evidence={"command": "worker top-secret-token"},
        command_evidence={"returncode": 1},
        error=ValueError("top-secret-token"),
        secrets=["top-secret-token"],
    )

    serialized = receipt.read_text() + (diagnostics / "candidate.stdout.log").read_text() + (
        diagnostics / "candidate.stderr.log"
    ).read_text()
    assert "top-secret-token" not in serialized
    assert json.loads(receipt.read_text())["error"]["code"] == "worker_initialization_failed"


def test_launchagent_candidate_logs_are_outside_rollback_replaced_tree(tmp_path, monkeypatch):
    root = tmp_path / "support/agata-codex-worker"
    monkeypatch.setattr(migration, "ROOT", root)
    diagnostics = migration._create_diagnostic_attempt("e" * 40, "attempt-plist")
    runtime = root / "runtime/current"

    plist = plistlib.loads(migration._render_plist(runtime, diagnostics))

    assert Path(plist["StandardOutPath"]).parent == diagnostics
    assert Path(plist["StandardErrorPath"]).parent == diagnostics
    assert not Path(plist["StandardErrorPath"]).is_relative_to(root)


def test_prepare_startup_check_uses_candidate_python_and_exact_launch_environment(tmp_path, monkeypatch):
    root = tmp_path / "Library/Application Support/GPoint/agata-codex-worker"
    root.mkdir(parents=True)
    staged = tmp_path / "staged/candidate"
    staged.mkdir(parents=True)
    (staged / ".venv/bin").mkdir(parents=True)
    (staged / ".venv/bin/python3.12").touch()
    (staged / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "CONFIG", tmp_path / "worker.env")
    manifest = {"source_sha": "f" * 40, "bundle_sha256": "e" * 64}
    stdout = json.dumps({
        "state": "PASS", "configured_concurrency": 1,
        "provider_parallel_capability": "NOT_VERIFIED",
        "lanes": {lane: {"configured": True} for lane in ("GPOINT", "GSALES", "LAB")},
        "claim_requests": 0, "inference_requests": 0,
        "runtime_current_changed": False, "launchagent_changed": False,
    })
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        observed["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(migration.subprocess, "run", run)
    result = migration._validate_candidate_startup(staged, manifest)

    assert result["state"] == "PASS"
    assert observed["command"] == [str(staged / ".venv/bin/python3.12"),
                                    str(staged / "gpoint_mac_worker.py"), "validate-startup"]
    assert observed["env"]["GPOINT_AGATA_WORKER_SUPPORT"] == str(root)
    assert observed["env"]["AGATA_WORKER_POOL_CONFIG"] == str(root / "config/pool.env")
    assert observed["env"]["GPOINT_AGATA_WORKER_CONFIG"] == str(tmp_path / "worker.env")
    assert observed["timeout"] == 30
    assert not (root / "runtime/current").exists()
    assert not (tmp_path / "Library/LaunchAgents").exists()


def test_candidate_startup_rejects_incomplete_pass_payload(tmp_path, monkeypatch):
    root = tmp_path / "support/agata-codex-worker"
    staged = tmp_path / "staged/candidate"
    (staged / ".venv/bin").mkdir(parents=True)
    (staged / ".venv/bin/python3.12").touch()
    (staged / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "ROOT", root)
    manifest = {"source_sha": "9" * 40, "bundle_sha256": "8" * 64}
    result = subprocess.CompletedProcess([], 0, json.dumps({"state": "PASS"}), "")
    monkeypatch.setattr(migration.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(RuntimeError, match="worker_startup_check_contract_invalid"):
        migration._validate_candidate_startup(staged, manifest)


def test_production_runtime_and_pool_root_are_application_support():
    canonical = Path.home() / "Library/Application Support/GPoint/agata-codex-worker"
    assert migration.ROOT == canonical
    assert migration.LEGACY == canonical


def test_rendered_launchagent_keeps_canonical_label_and_pool_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "AGATA Workers")
    monkeypatch.setattr(migration, "CONFIG", tmp_path / "config/agata-worker.env")
    runtime = migration.ROOT / "runtime" / "current"
    value = plistlib.loads(migration._render_plist(runtime))
    assert value["Label"] == "one.gpoint.agata-codex-worker"
    assert value["ProgramArguments"] == [str(runtime / "run_codex_worker.sh"), "watch"]
    assert value["EnvironmentVariables"]["AGATA_WORKER_POOL_CONFIG"] == str(migration.ROOT / "config/pool.env")
    assert value["EnvironmentVariables"]["GPOINT_AGATA_WORKER_SUPPORT"] == str(migration.ROOT)


def test_apply_rechecks_and_blocks_active_inference_before_any_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {"active_inference": True, "production_release_lock_busy": False})
    monkeypatch.setattr(migration, "ROOT", tmp_path / "root")
    try:
        migration.apply()
    except RuntimeError as exc:
        assert str(exc) == "active_inference_blocks_transition"
    else:
        raise AssertionError("active inference must fail closed")
    assert not migration.ROOT.exists()


def test_migration_rebinds_trusted_tenant_sidecar_to_exact_worker_source(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    old_worker = legacy / "gpoint_mac_worker.py"
    old_worker.write_text("verified old worker")
    sidecar = legacy / "tenant-bindings.json"
    value = {
        "version": 1, "candidate_sha": "a" * 40,
        "worker_sha256": migration.sha256(old_worker),
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    sidecar.write_text(json.dumps(value))
    sidecar.chmod(0o600)
    root = tmp_path / "pool"
    (root / "config").mkdir(parents=True)
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "ROOT", root)
    manifest = {"source_sha": "c" * 40, "assets": {"gpoint_mac_worker.py": "d" * 64}}
    migration._migrate_tenant_binding(manifest)
    result = json.loads((root / "config/tenant-bindings.json").read_text())
    assert result["tenant_id"] == value["tenant_id"]
    assert result["source_id"] == value["source_id"]
    assert result["candidate_sha"] == manifest["source_sha"]
    assert result["worker_sha256"] == manifest["assets"]["gpoint_mac_worker.py"]


def test_pool_config_migration_refuses_to_start_with_unverified_parallelism(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    path = root / "config/pool.env"
    path.write_text("AGATA_WORKER_CONCURRENCY=2\nAGATA_CODEX_PARALLEL_CAPABILITY=NOT_VERIFIED\n")
    path.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)
    try:
        migration._ensure_pool_config()
    except RuntimeError as exc:
        assert str(exc) == "worker_pool_must_start_at_n1_unverified"
    else:
        raise AssertionError("unverified parallel execution must be blocked")


def test_ensure_rollback_tree_creates_missing_rollback_directory(tmp_path, monkeypatch):
    root = tmp_path / "AGATA Workers"
    (root / "state").mkdir(parents=True)
    monkeypatch.setattr(migration, "ROOT", root)

    rollback = migration._ensure_rollback_tree("a" * 40)

    assert rollback == root.parent / ".agata-codex-worker-rollback" / ("a" * 40)
    assert rollback.is_dir()
    assert not rollback.is_symlink()
    assert rollback.stat().st_mode & 0o777 == 0o700
    assert rollback.stat().st_uid == os.getuid()


def test_apply_with_absent_rollback_tree_saves_snapshot_and_restart_evidence(tmp_path, monkeypatch):
    root = tmp_path / "AGATA Workers"
    root.mkdir()
    legacy = tmp_path / "legacy-runtime"
    legacy.mkdir()
    (legacy / "run_codex_worker.sh").write_text("legacy runtime")
    plist = tmp_path / "LaunchAgents/worker.plist"
    plist.parent.mkdir()
    original_plist = plistlib.dumps({"Label": migration.LABEL,
                                     "ProgramArguments": [str(legacy / "run_codex_worker.sh"), "watch"]})
    plist.write_bytes(original_plist)
    rollback_root = root.parent / ".agata-codex-worker-rollback" / ("a" * 40)
    assert not rollback_root.exists()

    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "PLIST", plist)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40,
                                                           "bundle_sha256": "b" * 64, "assets": {}})
    monkeypatch.setattr(migration, "inventory", lambda: {"production_release_lock_busy": False,
                                                            "launchagent_loaded": True,
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_read_production_queue_status",
                        lambda: _fresh_empty_queue_status(datetime.now(UTC)))
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "stable-network-state")

    def stage(manifest):
        release = root.parent / ".candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release

    monkeypatch.setattr(migration, "_stage_runtime", stage)
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda _stage, _manifest: {"state": "PASS"})
    monkeypatch.setattr(migration, "_install_staged_runtime", lambda _staged, _manifest:
                        root / "runtime" / ("a" * 40))
    monkeypatch.setattr(migration, "_ensure_pool_config", lambda: None)
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: 99999999)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])
    monkeypatch.setattr(migration, "_migrate_tenant_binding", lambda _manifest: None)
    healthy_status = {"configured_concurrency": 1, "lanes": {
        "GPOINT": {"health": "HEALTHY"}, "GSALES": {"health": "HEALTHY"},
    }}
    restart_evidence = {"verification": "ROLLBACK", "command": {
        "outcome": "AMBIGUOUS_RESTART_COMMAND_RESULT", "timeout_seconds": 20,
        "stdout": "partial", "stderr": "timeout",
    }, "adjudication": {"passed": False, "attempts": [{"checks": {"service": False}}]}}
    monkeypatch.setattr(migration, "_health_check", lambda: healthy_status)
    monkeypatch.setattr(migration, "_kickstart_and_verify", lambda _sha: restart_evidence)
    monkeypatch.setattr(migration, "_wait_for_successful_polls", lambda _baseline: {"passed": True})
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0))

    try:
        migration.apply()
    except RuntimeError as exc:
        assert str(exc) == "restart_verification_failed"
    else:
        raise AssertionError("the failed restart verification should trigger the normal rollback path")

    rollback = rollback_root
    assert (rollback / "original-launchagent.plist").read_bytes() == original_plist
    assert (rollback / "legacy-runtime/run_codex_worker.sh").read_text() == "legacy runtime"
    assert plist.read_bytes() == original_plist
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(legacy / "run_codex_worker.sh"), "watch"]
    assert (legacy / "run_codex_worker.sh").read_text() == "legacy runtime"


def test_fresh_empty_production_queue_status_allows_transition_and_ignores_lab():
    now = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    status = _fresh_empty_queue_status(now)
    status["lanes"]["LAB"] = {"queue_snapshot_state": "VERIFIED", "pending_inference_jobs": 9,
                               "running_inference_jobs": 0}
    evidence = migration.require_fresh_empty_production_queues(status, now=now)
    assert set(evidence["lanes"]) == {"GPOINT", "GSALES"}


def test_pending_running_or_busy_production_work_blocks_transition():
    now = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    for lane, field in (("GPOINT", "pending_inference_jobs"), ("GSALES", "running_inference_jobs")):
        status = _fresh_empty_queue_status(now)
        status["lanes"][lane][field] = 1
        with pytest.raises(RuntimeError, match="production_queue_not_empty"):
            migration.require_fresh_empty_production_queues(status, now=now)
    status = _fresh_empty_queue_status(now)
    status["pool"]["busy"] = 1
    with pytest.raises(RuntimeError, match="worker_slots_busy"):
        migration.require_fresh_empty_production_queues(status, now=now)


def test_missing_or_stale_production_queue_snapshot_fails_closed():
    now = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    status = _fresh_empty_queue_status(now)
    del status["lanes"]["GPOINT"]["pending_inference_jobs"]
    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.require_fresh_empty_production_queues(status, now=now)
    status = _fresh_empty_queue_status(now)
    status["lanes"]["GSALES"]["queue_snapshot_at"] = "2026-10-07T09:58:00+00:00"
    with pytest.raises(RuntimeError, match="production_queue_snapshot_stale"):
        migration.require_fresh_empty_production_queues(status, now=now)
    status = _fresh_empty_queue_status(now)
    status["pool_snapshot_at"] = "2026-10-07T09:58:00+00:00"
    with pytest.raises(RuntimeError, match="production_queue_snapshot_stale"):
        migration.require_fresh_empty_production_queues(status, now=now)


def test_apply_stops_before_runtime_mutation_when_production_queue_is_pending(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    status = _fresh_empty_queue_status(now)
    status["lanes"]["GPOINT"]["pending_inference_jobs"] = 1
    staged = []
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40,
                                                           "bundle_sha256": "b" * 64, "assets": {}})
    monkeypatch.setattr(migration, "inventory", lambda: {"production_release_lock_busy": False,
                                                            "launchagent_loaded": True,
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda: status)
    monkeypatch.setattr(migration, "_stage_runtime", lambda *_args: staged.append(True))

    with pytest.raises(RuntimeError, match="production_queue_not_empty"):
        migration.apply()

    assert staged == []


@pytest.mark.parametrize(
    ("paused_pid", "expected_error"),
    ((424242, "production_queue_not_empty"),
     (None, "worker_launchagent_unloaded_during_transition")),
)
def test_apply_aborts_before_bootout_when_pause_or_second_queue_check_is_unsafe(
        tmp_path, monkeypatch, paused_pid, expected_error):
    root = tmp_path / "AGATA Workers"
    root.mkdir()
    legacy = tmp_path / "legacy-runtime"
    legacy.mkdir()
    (legacy / "run_codex_worker.sh").write_text("legacy runtime")
    plist = tmp_path / "LaunchAgents/worker.plist"
    plist.parent.mkdir()
    original_plist = plistlib.dumps({
        "Label": migration.LABEL,
        "ProgramArguments": [str(legacy / "run_codex_worker.sh"), "watch"],
    })
    plist.write_bytes(original_plist)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "PLIST", plist)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {
        "source_sha": "a" * 40, "bundle_sha256": "b" * 64, "assets": {},
    })
    monkeypatch.setattr(migration, "inventory", lambda: {
        "production_release_lock_busy": False, "launchagent_loaded": True,
        "active_inference": False,
    })
    first_snapshot = _fresh_empty_queue_status(datetime.now(UTC))
    second_snapshot = _fresh_empty_queue_status(datetime.now(UTC))
    second_snapshot["lanes"]["GPOINT"]["pending_inference_jobs"] = 1
    queue_reads = iter((first_snapshot, second_snapshot))
    queue_read_calls = []
    def read_queue():
        queue_read_calls.append(True)
        return next(queue_reads)
    monkeypatch.setattr(migration, "_read_production_queue_status", read_queue)
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "stable-network-state")
    monkeypatch.setattr(migration, "_stage_runtime", lambda *_args: tmp_path / "candidate-stage")
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda *_args: {"state": "PASS"})
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: paused_pid)
    signals = []
    monkeypatch.setattr(migration.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    commands = []
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or subprocess.CompletedProcess(command, 0)
    ))

    with pytest.raises(RuntimeError, match=expected_error):
        migration.apply()

    expected_signals = [(424242, migration.signal.SIGCONT)] if paused_pid else []
    assert signals == expected_signals
    assert len(queue_read_calls) == (2 if paused_pid else 1)
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert plist.read_bytes() == original_plist
    assert not (root / "runtime/current").exists()
    assert not (root / "state/maintenance-stop").exists()


def test_worker_queue_status_validation_rejects_cross_lane_or_payload_counts():
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GPOINT"]["queue_snapshot_source_id"] = "agata-worker-queue-status-v1:gsales"
    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.require_fresh_empty_production_queues(status)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GSALES"]["pending_inference_jobs"] = True
    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.require_fresh_empty_production_queues(status)


def _fresh_empty_queue_status(now):
    return {
        "launch_agent_loaded": True,
        "pool": {"state": "HEALTHY", "busy": 0},
        "pool_snapshot_at": now.isoformat(),
        "lanes": {
            lane: {"health": "HEALTHY", "queue_snapshot_state": "VERIFIED",
                   "queue_snapshot_source_id": f"agata-worker-queue-status-v1:{lane.lower()}",
                   "queue_snapshot_at": now.isoformat(), "pending_inference_jobs": 0,
                   "running_inference_jobs": 0}
            for lane in ("GPOINT", "GSALES")
        },
    }
    assert not (root / "runtime/current").exists()
    receipt = json.loads((root / "receipts/runtime-migration-rollback.json").read_text())
    assert receipt["state"] == "ROLLED_BACK"
    assert receipt["restart_evidence"] == restart_evidence
    diagnostics = Path(receipt["diagnostics"])
    assert diagnostics.is_file()
    assert diagnostics.parent.parent.parent.name == ".agata-codex-worker-diagnostics"
    assert (diagnostics.parent / "candidate.stderr.log").is_file()


def test_in_place_upgrade_keeps_application_support_root_and_verified_rollback(tmp_path, monkeypatch):
    root = tmp_path / "Library/Application Support/GPoint/agata-codex-worker"
    root.mkdir(parents=True)
    (root / "gpoint_mac_worker.py").write_text("existing V3 worker")
    (root / "worker-status.json").write_text(json.dumps({
        "current_job_present": False,
        "targets": {
            "GPOINT": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
            "GSALES": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
        },
    }))
    plist = tmp_path / "Library/LaunchAgents/worker.plist"
    plist.parent.mkdir(parents=True)
    old_plist = plistlib.dumps({"Label": migration.LABEL,
        "ProgramArguments": [str(root / "run_codex_worker.sh"), "watch"]})
    plist.write_bytes(old_plist)
    rollback_base = tmp_path / "Library/Application Support/GPoint/.agata-codex-worker-rollback"
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)
    monkeypatch.setattr(migration, "PLIST", plist)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    manifest = {"source_sha": "f" * 40, "bundle_sha256": "a" * 64, "assets": {}}
    monkeypatch.setattr(migration, "candidate", lambda: manifest)
    monkeypatch.setattr(migration, "inventory", lambda: {"production_release_lock_busy": False,
                                                            "launchagent_loaded": True,
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_read_production_queue_status",
                        lambda: _fresh_empty_queue_status(datetime.now(UTC)))
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "unchanged")
    def stage(_manifest):
        release = tmp_path / "candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release
    monkeypatch.setattr(migration, "_stage_runtime", stage)
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda _stage, _manifest: {"state": "PASS"})
    def install(_staged, _manifest):
        release = root / "runtime" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release
    monkeypatch.setattr(migration, "_install_staged_runtime", install)
    monkeypatch.setattr(migration, "_ensure_pool_config", lambda: None)
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: 1234)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])
    monkeypatch.setattr(migration.os, "kill", lambda *_args: None)
    monkeypatch.setattr(migration, "_migrate_tenant_binding", lambda _manifest: None)
    status = {"configured_concurrency": 1, "lanes": {
        "GPOINT": {"health": "HEALTHY"}, "GSALES": {"health": "HEALTHY"}, "LAB": {"state": "AVAILABLE"}}}
    monkeypatch.setattr(migration, "_health_check", lambda: status)
    restart = {"verification": "PASS", "adjudication": {"status": status}}
    def kickstart(_sha):
        worker_status = root / "status/worker-status.json"
        value = json.loads(worker_status.read_text())
        value["targets"] = {
            "GPOINT": {"last_poll_at": "after", "last_error_code": None},
            "GSALES": {"last_poll_at": "after", "last_error_code": None},
        }
        worker_status.write_text(json.dumps(value))
        return restart
    monkeypatch.setattr(migration, "_kickstart_and_verify", kickstart)
    poll_baselines = []
    def wait_for_polls(baseline):
        poll_baselines.append(baseline)
        return {"passed": True}
    monkeypatch.setattr(migration, "_wait_for_successful_polls", wait_for_polls)
    monkeypatch.setattr(migration, "launchctl_loaded", lambda: True)
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0))

    result = migration.apply()

    assert result["state"] == "COMPLETE"
    assert poll_baselines == [{
        "current_job_present": False,
        "targets": {
            "GPOINT": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
            "GSALES": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
        },
    }]
    assert root.is_dir()
    assert (root / "runtime/current").resolve() == root / "runtime" / manifest["source_sha"]
    snapshot = rollback_base / manifest["source_sha"]
    assert (snapshot / "legacy-runtime/gpoint_mac_worker.py").read_text() == "existing V3 worker"
    assert (snapshot / "original-launchagent.plist").read_bytes() == old_plist
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [
        str(root / "runtime/current/run_codex_worker.sh"), "watch"]


def test_prepare_stages_and_snapshots_without_stopping_or_switching_live_worker(tmp_path, monkeypatch):
    root = tmp_path / "Library/Application Support/GPoint/agata-codex-worker"
    root.mkdir(parents=True)
    (root / "run_codex_worker.sh").write_text("legacy worker")
    (root / "worker-status.json").write_text('{"last_poll_at":"before"}')
    plist = tmp_path / "Library/LaunchAgents/worker.plist"
    plist.parent.mkdir(parents=True)
    original_plist = plistlib.dumps({"Label": migration.LABEL,
        "ProgramArguments": [str(root / "run_codex_worker.sh"), "watch"]})
    plist.write_bytes(original_plist)
    source_sha = "e" * 40
    manifest = {"source_sha": source_sha, "bundle_sha256": "b" * 64, "assets": {}}
    staged = tmp_path / "staging" / source_sha
    staged.mkdir(parents=True)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)
    monkeypatch.setattr(migration, "PLIST", plist)
    monkeypatch.setattr(migration, "candidate", lambda: manifest)
    monkeypatch.setattr(migration, "inventory", lambda: {
        "production_release_lock_busy": False, "active_inference": False,
        "launchagent_program_arguments": [str(root / "run_codex_worker.sh"), "watch"]})
    monkeypatch.setattr(migration, "_stage_runtime", lambda _manifest: staged)
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda _stage, _manifest: {"state": "PASS"})
    monkeypatch.setattr(migration, "_verify_staged_runtime", lambda *_args: None)
    capture_snapshot = migration._prepare_legacy_snapshot
    snapshot_attempts = []
    def snapshot_then_worker_poll(backup):
        snapshot_attempts.append(backup)
        if len(snapshot_attempts) == 1:
            raise shutil.Error([(str(root / "worker-status.tmp"), str(backup / "worker-status.tmp"),
                                 "No such file or directory: worker-status.tmp")])
        captured = capture_snapshot(backup)
        (root / "worker-status.json").write_text('{"last_poll_at":"after"}')
        return captured
    monkeypatch.setattr(migration, "_prepare_legacy_snapshot", snapshot_then_worker_poll)

    result = migration.prepare()

    snapshot = Path(result["rollback_snapshot"])
    assert result["state"] == "PREPARED"
    assert len(snapshot_attempts) == 2
    assert result["launchagent_stopped"] is False
    assert result["runtime_current_changed"] is False
    assert not (root / "runtime/current").exists()
    assert plist.read_bytes() == original_plist
    assert (snapshot / "run_codex_worker.sh").read_text() == "legacy worker"
    assert json.loads((snapshot / "worker-status.json").read_text()) == {"last_poll_at": "before"}
    assert json.loads((root / "worker-status.json").read_text()) == {"last_poll_at": "after"}
    assert (snapshot.parent / "original-launchagent.plist").read_bytes() == original_plist


@pytest.mark.parametrize("lab_status", [
    {"state": "AVAILABLE", "last_error_code": "lab_approval_required",
     "inference_gate": "APPROVAL_REQUIRED"},
    {"state": "DISABLED", "inference_gate": "CLOSED_DISABLED"},
])
def test_restart_snapshot_requires_exact_runtime_identity_and_all_lanes(tmp_path, monkeypatch, lab_status):
    root = tmp_path / "AGATA Workers"
    source_sha = "c" * 40
    release = root / "runtime" / source_sha
    _write_runtime_bundle(release, source_sha)
    (root / "runtime/current").symlink_to(source_sha)
    monkeypatch.setattr(migration, "ROOT", root)
    launchctl = (f"gui/{os.getuid()}/{migration.LABEL} = {{\n"
                "\tstate = running\n\tpid = 4312\n"
                f"\tprogram = {root}/runtime/current/run_codex_worker.sh\n}}\n")
    worker_command = (f"{release}/.venv/bin/python3.12 "
                      f"{release}/gpoint_mac_worker.py watch")
    status = {
        "configured_concurrency": 1, "current_job_present": False,
        "pool": {"state": "running"},
        "lanes": {"GPOINT": {"health": "HEALTHY", "current_job_present": False},
                  "GSALES": {"health": "HEALTHY", "current_job_present": False},
                  "LAB": lab_status},
    }

    def run(command, **kwargs):
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, launchctl, "")
        if command[:3] == ["ps", "-p", "4312"]:
            return subprocess.CompletedProcess(command, 0, worker_command, "")
        if command[:2] == [str(root / "runtime/current/.venv/bin/python3.12"),
                           str(root / "runtime/current/gpoint_mac_worker.py")]:
            return subprocess.CompletedProcess(command, 0, json.dumps(status), "")
        if command == ["ps", "-axo", "pid=,command="]:
            return subprocess.CompletedProcess(command, 0, f"4312 {worker_command}\n", "")
        raise AssertionError(f"unexpected read-only command: {command}")

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])

    evidence = migration._restart_state_snapshot(expected_source_sha=source_sha)

    assert evidence["passed"] is True
    assert all(evidence["checks"].values())
    assert evidence["pid"] == 4312
    assert evidence["runtime_source_sha"] == source_sha
    assert evidence["checks"]["lab_approval_fail_closed"] is True
    assert evidence["worker_status"]["configured_concurrency"] == 1
    assert set(evidence["worker_status"]["lanes"]) == {"GPOINT", "GSALES", "LAB"}


def test_restart_snapshot_rejects_current_symlink_pointing_at_other_sha(tmp_path, monkeypatch):
    root = tmp_path / "AGATA Workers"
    expected_sha = "c" * 40
    wrong_sha = "d" * 40
    _write_runtime_bundle(root / "runtime" / expected_sha, expected_sha)
    _write_runtime_bundle(root / "runtime" / wrong_sha, wrong_sha)
    (root / "runtime/current").symlink_to(wrong_sha)
    monkeypatch.setattr(migration, "ROOT", root)
    launchctl = (f"gui/{os.getuid()}/{migration.LABEL} = {{\n"
                 "\tstate = running\n\tpid = 4312\n}\n")
    worker_command = (f"{root}/runtime/{wrong_sha}/.venv/bin/python3.12 "
                      f"{root}/runtime/{wrong_sha}/gpoint_mac_worker.py watch")
    status = {"configured_concurrency": 1, "current_job_present": False, "pool": {"state": "running"},
              "lanes": {"GPOINT": {"health": "HEALTHY"}, "GSALES": {"health": "HEALTHY"},
                        "LAB": {"state": "AVAILABLE", "inference_gate": "APPROVAL_REQUIRED"}}}

    def run(command, **kwargs):
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 0, launchctl, "")
        if command[:3] == ["ps", "-p", "4312"]:
            return subprocess.CompletedProcess(command, 0, worker_command, "")
        if command[:2] == [str(root / "runtime/current/.venv/bin/python3.12"),
                           str(root / "runtime/current/gpoint_mac_worker.py")]:
            return subprocess.CompletedProcess(command, 0, json.dumps(status), "")
        if command == ["ps", "-axo", "pid=,command="]:
            return subprocess.CompletedProcess(command, 0, f"4312 {worker_command}\n", "")
        raise AssertionError(f"unexpected read-only command: {command}")

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])

    evidence = migration._restart_state_snapshot(expected_source_sha=expected_sha)

    assert evidence["passed"] is False
    assert evidence["checks"]["runtime_parity"] is False
    assert evidence["runtime_source_sha"] == wrong_sha


def _write_runtime_bundle(release, source_sha):
    release.mkdir(parents=True)
    asset_names = migration.ASSETS
    assets = {}
    for name in asset_names:
        content = f"{source_sha}:{name}".encode()
        (release / name).write_bytes(content)
        assets[name] = hashlib.sha256(content).hexdigest()
    bundle_hash = hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (release / "bundle-manifest.json").write_text(json.dumps({
        "source_sha": source_sha, "assets": assets, "bundle_sha256": bundle_hash,
    }))


def test_kickstart_timeout_with_healthy_new_worker_is_adjudicated_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "LABEL", "one.gpoint.agata-codex-worker")
    healthy = {
        "passed": True,
        "status": {"configured_concurrency": 1, "lanes": {
            "GPOINT": {"health": "HEALTHY"}, "GSALES": {"health": "HEALTHY"},
            "LAB": {"state": "approval_required"},
        }},
        "attempts": [{"checks": {"service": True, "pid": True, "process_identity": True,
            "status": True, "concurrency": True, "gpoint": True, "gsales": True,
            "lab_lane": True, "no_active_inference": True, "no_duplicate_worker": True}}],
    }
    timeout = subprocess.TimeoutExpired(["launchctl", "kickstart", "-k", "service"], 20,
                                       output=b"partial stdout", stderr=b"partial stderr")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise timeout

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration, "_adjudicate_restart_state", lambda _sha: healthy)

    result = migration._kickstart_and_verify(expected_source_sha="a" * 40)

    assert result["verification"] == "PASS"
    assert result["command"]["outcome"] == "AMBIGUOUS_RESTART_COMMAND_RESULT"
    assert result["command"]["timeout_seconds"] == 20
    assert result["command"]["stdout"] == "partial stdout"
    assert result["command"]["stderr"] == "partial stderr"
    assert result["adjudication"] == healthy
    assert calls == [["launchctl", "kickstart", "-k",
                      f"gui/{os.getuid()}/one.gpoint.agata-codex-worker"]]


def test_kickstart_timeout_with_unhealthy_new_worker_requires_rollback(monkeypatch):
    timeout = subprocess.TimeoutExpired(["launchctl", "kickstart", "-k", "service"], 20)

    def run(_command, **_kwargs):
        raise timeout

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration, "_adjudicate_restart_state", lambda _sha: {
        "passed": False,
        "status": {"configured_concurrency": 1, "lanes": {"GPOINT": {"health": "HEALTHY"},
                                                              "GSALES": {"health": "DEGRADED"}}},
        "attempts": [{"checks": {"gsales": False}}],
    })

    result = migration._kickstart_and_verify(expected_source_sha="a" * 40)

    assert result["verification"] == "ROLLBACK"
    assert result["command"]["outcome"] == "AMBIGUOUS_RESTART_COMMAND_RESULT"
    assert result["adjudication"]["passed"] is False


def test_rollback_restores_exact_legacy_plist_and_runtime_path(tmp_path, monkeypatch):
    root = tmp_path / "new-runtime"
    legacy = tmp_path / "legacy-runtime"
    plist = tmp_path / "LaunchAgents/worker.plist"
    rollback = root.parent / ".agata-codex-worker-rollback" / ("c" * 40)
    (rollback / "legacy-runtime").mkdir(parents=True)
    (rollback / "legacy-runtime/run_codex_worker.sh").write_text("legacy worker bytes")
    legacy.mkdir(parents=True)
    (legacy / "run_codex_worker.sh").write_text("partially removed legacy tree")
    exact_plist = plistlib.dumps({"Label": migration.LABEL,
                                  "ProgramArguments": [str(legacy / "run_codex_worker.sh"), "watch"]})
    (rollback / "original-launchagent.plist").write_bytes(exact_plist)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"new plist")
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "PLIST", plist)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "inventory", lambda: {"production_release_lock_busy": False,
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: 99999999)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])
    monkeypatch.setattr(migration, "launchctl_loaded", lambda: True)
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0))

    evidence = migration.rollback()

    assert evidence["state"] == "ROLLED_BACK"
    assert plist.read_bytes() == exact_plist
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(legacy / "run_codex_worker.sh"), "watch"]
    assert (legacy / "run_codex_worker.sh").read_text() == "legacy worker bytes"


def test_stale_rollback_snapshot_is_atomically_refreshed_from_legacy(tmp_path, monkeypatch):
    root = tmp_path / "AGATA Workers"
    legacy = tmp_path / "legacy-runtime"
    legacy.mkdir()
    (legacy / "run_codex_worker.sh").write_text("exact live runtime")
    (legacy / "worker-status.json").write_text('{"last_poll_at":"new"}')
    rollback = root.parent / ".agata-codex-worker-rollback" / ("d" * 40)
    rollback.mkdir(parents=True)
    backup = rollback / "legacy-runtime"
    backup.mkdir(parents=True)
    (backup / "run_codex_worker.sh").write_text("exact live runtime")
    (backup / "worker-status.json").write_text('{"last_poll_at":"old"}')
    original_plist = rollback / "original-launchagent.plist"
    original_plist.write_bytes(b"preserve original LaunchAgent")
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", legacy)

    result = migration._prepare_legacy_snapshot(backup)

    assert result == backup
    assert migration._same_tree(backup, legacy)
    assert (backup / "worker-status.json").read_text() == '{"last_poll_at":"new"}'
    assert original_plist.read_bytes() == b"preserve original LaunchAgent"
    assert not (rollback / ".legacy-runtime-previous").exists()
    assert not list(rollback.glob(".legacy-runtime-staging-*"))


def test_legacy_runtime_is_removed_only_after_health_and_restart_pass(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy-runtime"
    legacy.mkdir()
    (legacy / "worker.py").write_text("keep until every gate passes")
    monkeypatch.setattr(migration, "LEGACY", legacy)

    try:
        migration._remove_legacy_after_verification(
            health_passed=True,
            restart_evidence={"verification": "ROLLBACK"},
        )
    except RuntimeError as exc:
        assert str(exc) == "legacy_cleanup_requires_full_verification"
    else:
        raise AssertionError("legacy must remain when restart verification fails")
    assert legacy.exists()

    migration._remove_legacy_after_verification(
        health_passed=True,
        restart_evidence={"verification": "PASS"},
    )
    assert not legacy.exists()


def test_fresh_successful_pool_polls_clear_stale_lane_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "AGATA Workers")
    status = migration.ROOT / "status/worker-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T07:00:00+00:00", "last_error_code": None},
        "GSALES": {"last_poll_at": "2026-10-05T07:00:01+00:00", "last_error_code": None},
    }}))
    baseline = {"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T06:59:00+00:00", "last_error_code": "worker_target_unavailable"},
        "GSALES": {"last_poll_at": "2026-10-05T06:59:01+00:00", "last_error_code": "worker_target_unavailable"},
    }}

    result = migration._wait_for_successful_polls(baseline, deadline=1)

    assert result["passed"] is True
    assert all(lane["poll_advanced"] and lane["stale_error_cleared"]
               for lane in result["lanes"].values())


def test_successful_poll_gate_fails_closed_while_stale_lane_error_remains(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "AGATA Workers")
    status = migration.ROOT / "status/worker-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T07:00:00+00:00", "last_error_code": None},
        "GSALES": {"last_poll_at": "2026-10-05T07:00:01+00:00", "last_error_code": "worker_target_unavailable"},
    }}))
    baseline = {"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T06:59:00+00:00"},
        "GSALES": {"last_poll_at": "2026-10-05T06:59:01+00:00"},
    }}

    result = migration._wait_for_successful_polls(baseline, deadline=0.05)

    assert result["passed"] is False
    assert result["lanes"]["GSALES"]["stale_error_cleared"] is False


def test_stage_runtime_uses_running_python312_when_missing_from_path(tmp_path, monkeypatch):
    worker_dir = tmp_path / "source"
    worker_dir.mkdir()
    assets = {}
    for name in migration.ASSETS:
        payload = f"asset:{name}".encode()
        (worker_dir / name).write_bytes(payload)
        assets[name] = hashlib.sha256(payload).hexdigest()
    manifest = {"source_sha": "e" * 40, "assets": assets, "bundle_sha256": "f" * 64}
    current_python = tmp_path / "legacy-venv/bin/python"
    current_python.parent.mkdir(parents=True)
    current_python.write_text("verified Python 3.12 interpreter")
    current_python.chmod(0o700)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration, "ROOT", tmp_path / "new-runtime")
    monkeypatch.setattr(migration.sys, "version_info", (3, 12, 8, "final", 0))
    monkeypatch.setattr(migration.sys, "executable", str(current_python))
    monkeypatch.setattr(migration.shutil, "which", lambda _name: None)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            venv_python = Path(command[3]) / "bin/python3.12"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("staged Python 3.12")
            venv_python.chmod(0o700)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(migration.subprocess, "run", run)

    release = migration._stage_runtime(manifest)

    assert release == migration._runtime_staging_base() / manifest["source_sha"]
    assert commands[0][:3] == [str(current_python), "-m", "venv"]
    assert len(commands) == 3
    assert (release / "bundle-manifest.json").is_file()
    assert os.access(release / "run_codex_worker.sh", os.X_OK)
    assert all((release / name).read_bytes() == (worker_dir / name).read_bytes()
               for name in migration.ASSETS)
    assert (release / "lab_promptfoo_compat_manifest.json").is_file()
