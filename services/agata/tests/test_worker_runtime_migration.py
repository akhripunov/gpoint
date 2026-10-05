"""Migration safety contracts; no LaunchAgent or installed files are changed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import subprocess
from pathlib import Path

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
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "stable-network-state")

    def stage(manifest):
        release = root.parent / ".candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release

    monkeypatch.setattr(migration, "_stage_runtime", stage)
    monkeypatch.setattr(migration, "_install_staged_runtime", lambda _staged, _manifest:
                        root / "runtime" / ("a" * 40))
    monkeypatch.setattr(migration, "_ensure_pool_config", lambda: None)
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: None)
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
    assert not (root / "runtime/current").exists()
    receipt = json.loads((root / "receipts/runtime-migration-rollback.json").read_text())
    assert receipt["state"] == "ROLLED_BACK"
    assert receipt["restart_evidence"] == restart_evidence


def test_in_place_upgrade_keeps_application_support_root_and_verified_rollback(tmp_path, monkeypatch):
    root = tmp_path / "Library/Application Support/GPoint/agata-codex-worker"
    root.mkdir(parents=True)
    (root / "gpoint_mac_worker.py").write_text("existing V3 worker")
    (root / "worker-status.json").write_text('{"current_job_present":false}')
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
                                                            "active_inference": False})
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "unchanged")
    def stage(_manifest):
        release = tmp_path / "candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release
    monkeypatch.setattr(migration, "_stage_runtime", stage)
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
    monkeypatch.setattr(migration, "_kickstart_and_verify", lambda _sha: restart)
    monkeypatch.setattr(migration, "_wait_for_successful_polls", lambda _baseline: {"passed": True})
    monkeypatch.setattr(migration, "launchctl_loaded", lambda: True)
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0))

    result = migration.apply()

    assert result["state"] == "COMPLETE"
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
    monkeypatch.setattr(migration, "_verify_staged_runtime", lambda *_args: None)

    result = migration.prepare()

    snapshot = Path(result["rollback_snapshot"])
    assert result["state"] == "PREPARED"
    assert result["launchagent_stopped"] is False
    assert result["runtime_current_changed"] is False
    assert not (root / "runtime/current").exists()
    assert plist.read_bytes() == original_plist
    assert (snapshot / "run_codex_worker.sh").read_text() == "legacy worker"
    assert (snapshot.parent / "original-launchagent.plist").read_bytes() == original_plist


def test_restart_snapshot_requires_exact_runtime_identity_and_all_lanes(tmp_path, monkeypatch):
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
                  "LAB": {"state": "AVAILABLE", "last_error_code": "lab_approval_required",
                          "inference_gate": "APPROVAL_REQUIRED"}},
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
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: None)
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
