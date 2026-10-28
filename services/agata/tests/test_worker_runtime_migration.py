"""Migration safety contracts; no LaunchAgent or installed files are changed."""
from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "worker/migrate_worker_runtime.py"
SPEC = importlib.util.spec_from_file_location("worker_runtime_migration", MODULE_PATH)
migration = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(migration)


def _write_verified_runtime(root: Path, directory_sha: str, source_sha: str) -> tuple[Path, Path]:
    runtime = root / "runtime" / directory_sha
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    assets = {}
    for name in migration.ASSETS:
        path = runtime / name
        path.write_text(f"verified {name}")
        path.chmod(0o700 if name == "run_codex_worker.sh" else 0o600)
        assets[name] = migration.sha256(path)
    worker = runtime / "gpoint_mac_worker.py"
    bundle_sha = hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"source_sha": source_sha, "bundle_sha256": bundle_sha, "assets": assets}
    manifest_path = runtime / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    return runtime, worker


def _fresh_gpoint_live_snapshot(tenant_id: str, *, captured_at: datetime | None = None) -> dict:
    captured = captured_at or datetime.now(UTC)
    release_sha = "f" * 40
    api_image = "sha256:" + "1" * 64
    core_image = "sha256:" + "2" * 64
    source_id = (f"{migration.LIVE_QUEUE_SOURCE}:gpoint:release={release_sha}"
                 f":api={api_image}:core={core_image}:tenant={tenant_id}")
    provenance = {
        "snapshot_version": "agata_live_queue_snapshot_v1",
        "source_id": source_id,
        "lane": "GPOINT",
        "source_system": "gpoint",
        "release_sha": release_sha,
        "api_build_sha": release_sha,
        "core_build_sha": release_sha,
        "api_image_id": api_image,
        "core_image_id": core_image,
        "tenant_id": tenant_id,
        "tenant_identity_verified": True,
        "transaction_read_only": True,
        "same_login": True,
        "role_superuser": False,
        "role_bypassrls": False,
        "visible_tenant_rows": 1,
        "database_name": "gpoint_test",
    }
    return {
        "health": "HEALTHY",
        "queue_snapshot_state": "VERIFIED",
        "queue_snapshot_source_id": f"{migration.LIVE_QUEUE_SOURCE}:gpoint",
        "queue_snapshot_at": captured.isoformat(),
        "pending_inference_jobs": 0,
        "running_inference_jobs": 0,
        "queue_snapshot_authority": "live_runtime_readonly_snapshot_v1",
        "queue_snapshot_runtime_release_sha": release_sha,
        "queue_snapshot_tenant_id": tenant_id,
        "queue_snapshot_provenance": provenance,
    }


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
        "lab_v2_dev_manifest.json",
        "run_codex_worker.sh",
        "requirements.txt",
    )

    wrapper_path = worker_dir / "install_codex_launch_agent.py"
    wrapper_spec = importlib.util.spec_from_file_location("worker_install_wrapper", wrapper_path)
    wrapper = importlib.util.module_from_spec(wrapper_spec)
    assert wrapper_spec and wrapper_spec.loader
    wrapper_spec.loader.exec_module(wrapper)

    worker_tree = ast.parse((worker_dir / "gpoint_mac_worker.py").read_text(encoding="utf-8"))
    queue_tree = ast.parse((worker_dir / "lab_queue.py").read_text(encoding="utf-8"))
    bundle_function = next(
        node for node in queue_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "worker_bundle_sha256"
    )
    bundle_assets = next(
        node.value for node in queue_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "WORKER_BUNDLE_ASSET_NAMES"
                for target in node.targets)
    )
    bundle_names = tuple(item.value for item in bundle_assets.elts)
    assert any(
        isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "WORKER_BUNDLE_ASSET_NAMES"
        for node in bundle_function.body
    )
    worker_bundle_function = next(
        node for node in worker_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_bundle_sha256"
    )
    assert any(isinstance(node, ast.Return) for node in worker_bundle_function.body)

    assert migration.ASSETS == expected
    assert tuple(path.name for path in wrapper.SOURCES) == expected
    assert bundle_names == expected


def test_lab_slot_pointer_switch_changes_only_lab_and_keeps_production_slots(tmp_path):
    root = tmp_path / "worker"
    runtime = root / "runtime"
    slots = runtime / "slots"
    old_sha, new_sha = "a" * 40, "b" * 40
    (runtime / old_sha).mkdir(parents=True)
    (runtime / new_sha).mkdir()
    (runtime / old_sha).chmod(0o700)
    (runtime / new_sha).chmod(0o700)
    root.chmod(0o700)
    runtime.chmod(0o700)
    slots.mkdir(parents=True, exist_ok=True)
    slots.chmod(0o700)
    for lane in ("GPOINT", "GSALES", "LAB"):
        lane_dir = slots / lane
        lane_dir.mkdir(parents=True)
        lane_dir.chmod(0o700)
        (lane_dir / "current").symlink_to(os.path.relpath(runtime / old_sha, lane_dir))

    previous = migration._replace_lane_runtime_pointer(root, "LAB", new_sha)

    assert previous == "../../" + old_sha
    assert (slots / "LAB/current").resolve() == runtime / new_sha
    assert (slots / "GPOINT/current").resolve() == runtime / old_sha
    assert (slots / "GSALES/current").resolve() == runtime / old_sha


def test_lab_slot_install_is_fail_closed_when_live_supervisor_lacks_lane_pointer_contract(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    current_release = root / "runtime" / "current-release"
    current_release.mkdir(parents=True)
    root.chmod(0o700)
    (root / "runtime").chmod(0o700)
    (current_release / "agata_worker_pool.py").write_text("legacy supervisor")
    (root / "runtime/current").symlink_to(current_release)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(migration, "_release_lock_busy", lambda: False)

    with pytest.raises(RuntimeError, match="worker_pool_bootstrap_required_use_full_transition_apply"):
        migration.install_lab_slot()

    assert not (root / "runtime/slots/LAB/current").exists()
    assert (root / "runtime/current").resolve().samefile(current_release)


def test_lab_slot_install_never_queries_production_queues(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    runtime = root / "runtime"
    old_sha, new_sha = "a" * 40, "b" * 40
    old_release, new_release = runtime / old_sha, runtime / new_sha
    old_release.mkdir(parents=True)
    new_release.mkdir()
    old_release.chmod(0o700)
    new_release.chmod(0o700)
    root.chmod(0o700)
    runtime.chmod(0o700)
    (old_release / "agata_worker_pool.py").write_text("def slot_runtime_current(lane): pass")
    (runtime / "current").symlink_to(old_sha)
    (runtime / "slots").mkdir()
    (runtime / "slots").chmod(0o700)
    for lane in ("GPOINT", "GSALES", "LAB"):
        lane_dir = runtime / "slots" / lane
        lane_dir.mkdir()
        lane_dir.chmod(0o700)
        (lane_dir / "current").symlink_to(os.path.relpath(old_release, lane_dir))
    (root / "slots/slot-03").mkdir(parents=True)
    (root / "slots").chmod(0o700)
    (root / "slots/slot-03").chmod(0o700)
    (root / "receipts").mkdir()
    (root / "receipts").chmod(0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(migration, "_release_lock_busy", lambda: False)
    monkeypatch.setattr(migration, "candidate", lambda: {
        "source_sha": new_sha, "bundle_sha256": "c" * 64, "assets": {},
    })
    monkeypatch.setattr(migration, "_stage_runtime", lambda _manifest: new_release)
    monkeypatch.setattr(migration, "_install_staged_runtime", lambda _staged, _manifest: new_release)
    monkeypatch.setattr(migration, "_verify_staged_runtime", lambda _release, _manifest: None)
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda _manifest, **_kwargs: pytest.fail(
        "LAB-only installs must not require production queue snapshots"))

    receipt = migration.install_lab_slot()

    assert receipt["scope"] == "LAB_SLOT_ONLY"
    assert "queue_preflight" not in receipt
    assert (runtime / "slots/LAB/current").resolve().samefile(new_release)
    assert (runtime / "slots/GPOINT/current").resolve().samefile(old_release)
    assert (runtime / "slots/GSALES/current").resolve().samefile(old_release)
    assert (runtime / "current").resolve().samefile(old_release)


def test_lab_slot_install_switches_only_lab_without_launchagent_or_production_gate(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    runtime = root / "runtime"
    old_sha, new_sha = "a" * 40, "b" * 40
    old_release, new_release = runtime / old_sha, runtime / new_sha
    old_release.mkdir(parents=True)
    new_release.mkdir()
    old_release.chmod(0o700)
    new_release.chmod(0o700)
    root.chmod(0o700)
    runtime.chmod(0o700)
    (old_release / "agata_worker_pool.py").write_text("def slot_runtime_current(lane): pass")
    (runtime / "current").symlink_to(old_sha)
    (runtime / "slots").mkdir()
    (runtime / "slots").chmod(0o700)
    for lane in ("GPOINT", "GSALES", "LAB"):
        lane_dir = runtime / "slots" / lane
        lane_dir.mkdir(parents=True)
        lane_dir.chmod(0o700)
        (lane_dir / "current").symlink_to(os.path.relpath(old_release, lane_dir))
    (root / "slots/slot-03").mkdir(parents=True)
    (root / "slots").chmod(0o700)
    (root / "slots/slot-03").chmod(0o700)
    (root / "receipts").mkdir()
    (root / "receipts").chmod(0o700)
    manifest = {"source_sha": new_sha, "bundle_sha256": "c" * 64, "assets": {}}
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(migration, "_release_lock_busy", lambda: False)
    monkeypatch.setattr(migration, "candidate", lambda: manifest)
    monkeypatch.setattr(migration, "_stage_runtime", lambda _manifest: new_release)
    monkeypatch.setattr(migration, "_install_staged_runtime", lambda _staged, _manifest: new_release)
    monkeypatch.setattr(migration, "_verify_staged_runtime", lambda _release, _manifest: None)

    receipt = migration.install_lab_slot()

    assert receipt["state"] == "COMPLETE"
    assert receipt["scope"] == "LAB_SLOT_ONLY"
    assert receipt["other_lane_slots_changed"] is False
    assert receipt["launchagent_changed"] is False
    assert (runtime / "slots/LAB/current").resolve().samefile(new_release)
    assert (runtime / "slots/GPOINT/current").resolve().samefile(old_release)
    assert (runtime / "slots/GSALES/current").resolve().samefile(old_release)
    assert (runtime / "current").resolve().samefile(old_release)
    receipts = list((root / "receipts").glob("lane-slot-runtime-switch-lab-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text()) == receipt


def test_production_lane_slot_install_requires_fresh_empty_queue_preflight_before_staging(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    current = root / "runtime/current"
    current.mkdir(parents=True)
    current.joinpath("agata_worker_pool.py").write_text("def slot_runtime_current(lane): pass")
    root.chmod(0o700)
    (root / "runtime").chmod(0o700)
    current.chmod(0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(migration, "_release_lock_busy", lambda: False)
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda _manifest, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("production_queue_status_unavailable")))
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "b" * 40})
    monkeypatch.setattr(migration, "_stage_runtime", lambda _manifest: pytest.fail("runtime staging must follow queue preflight"))

    with pytest.raises(RuntimeError, match="production_queue_status_unavailable"):
        migration.install_lane_slot("GPOINT")


def test_install_lane_cli_requires_lane_before_any_apply(monkeypatch):
    monkeypatch.setattr(migration.sys, "argv", ["migrate_worker_runtime.py", "install-lane-slot"])
    monkeypatch.setattr(migration, "apply", lambda: pytest.fail("missing lane must never fall through to apply"))

    with pytest.raises(RuntimeError, match="worker_lane_required"):
        migration.main()


def test_cli_stopped_gsales_mode_is_explicit_and_apply_only(monkeypatch, capsys):
    monkeypatch.setattr(migration.sys, "argv", [
        "migrate_worker_runtime.py", "apply", "--preserve-stopped-gsales",
    ])
    calls = []
    monkeypatch.setattr(migration, "apply", lambda **kwargs: calls.append(kwargs) or {"state": "TEST"})

    assert migration.main() == 0
    assert calls == [{"preserve_stopped_gsales": True}]
    assert json.loads(capsys.readouterr().out) == {"state": "TEST"}

    monkeypatch.setattr(migration.sys, "argv", [
        "migrate_worker_runtime.py", "preflight", "--preserve-stopped-gsales",
    ])
    with pytest.raises(RuntimeError, match="preserve_stopped_gsales_apply_only"):
        migration.main()


def test_gsales_slot_install_switches_only_gsales_after_fresh_queue_gate(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    runtime = root / "runtime"
    old_sha, new_sha = "a" * 40, "b" * 40
    old_release, new_release = runtime / old_sha, runtime / new_sha
    old_release.mkdir(parents=True)
    new_release.mkdir()
    old_release.chmod(0o700)
    new_release.chmod(0o700)
    root.chmod(0o700)
    runtime.chmod(0o700)
    (old_release / "agata_worker_pool.py").write_text("def slot_runtime_current(lane): pass")
    (runtime / "current").symlink_to(old_sha)
    (runtime / "slots").mkdir()
    (runtime / "slots").chmod(0o700)
    for lane in ("GPOINT", "GSALES", "LAB"):
        lane_dir = runtime / "slots" / lane
        lane_dir.mkdir(parents=True)
        lane_dir.chmod(0o700)
        (lane_dir / "current").symlink_to(os.path.relpath(old_release, lane_dir))
    (root / "slots/slot-02").mkdir(parents=True)
    (root / "slots").chmod(0o700)
    (root / "slots/slot-02").chmod(0o700)
    (root / "receipts").mkdir()
    (root / "receipts").chmod(0o700)
    manifest = {"source_sha": new_sha, "bundle_sha256": "c" * 64, "assets": {}}
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "RELEASE_LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(migration, "_release_lock_busy", lambda: False)
    monkeypatch.setattr(migration, "candidate", lambda: manifest)
    monkeypatch.setattr(migration, "_stage_runtime", lambda _manifest: new_release)
    monkeypatch.setattr(migration, "_install_staged_runtime", lambda _staged, _manifest: new_release)
    monkeypatch.setattr(migration, "_verify_staged_runtime", lambda _release, _manifest: None)
    preflight = {"verified": True, "lanes": {"GPOINT": {"pending_jobs": 0}, "GSALES": {"pending_jobs": 0}}}
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda _manifest, **_kwargs: object())
    monkeypatch.setattr(migration, "require_fresh_empty_production_queues", lambda _status: preflight)

    receipt = migration.install_lane_slot("GSALES")

    assert receipt["scope"] == "GSALES_SLOT_ONLY"
    assert receipt["queue_preflight"] == preflight
    assert receipt["other_lane_slots_changed"] is False
    assert receipt["launchagent_changed"] is False
    assert (runtime / "slots/GSALES/current").resolve() == new_release
    assert (runtime / "slots/GPOINT/current").resolve() == old_release
    assert (runtime / "slots/LAB/current").resolve() == old_release
    assert (runtime / "current").resolve() == old_release


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
    monkeypatch.setattr(migration, "LEGACY", tmp_path / "legacy-runtime")
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


def test_prepare_startup_check_uses_verified_legacy_tenant_binding_without_persisting_it(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    old_worker = legacy / "gpoint_mac_worker.py"
    old_worker.write_text("verified old worker")
    tenant_id = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    (legacy / "tenant-bindings.json").write_text(json.dumps({
        "version": 1,
        "candidate_sha": "a" * 40,
        "worker_sha256": migration.sha256(old_worker),
        "tenant_id": tenant_id,
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }))
    (legacy / "tenant-bindings.json").chmod(0o600)
    root = tmp_path / "pool"
    root.mkdir()
    config = tmp_path / "worker.env"
    config.write_text("GPOINT_AGATA_WORKER_URL=https://gpoint.example.test\n")
    config.chmod(0o600)
    staged = tmp_path / "staged/candidate"
    (staged / ".venv/bin").mkdir(parents=True)
    (staged / ".venv/bin/python3.12").touch()
    (staged / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "CONFIG", config)
    manifest = {"source_sha": "f" * 40, "bundle_sha256": "e" * 64,
                "assets": {"gpoint_mac_worker.py": "d" * 64}}
    stdout = json.dumps({
        "state": "PASS", "configured_concurrency": 1,
        "provider_parallel_capability": "NOT_VERIFIED",
        "lanes": {lane: {"configured": True} for lane in ("GPOINT", "GSALES", "LAB")},
        "claim_requests": 0, "inference_requests": 0,
        "runtime_current_changed": False, "launchagent_changed": False,
    })
    observed = {}
    def run(command, **kwargs):
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout, "")
    monkeypatch.setattr(migration.subprocess, "run", run)

    result = migration._validate_candidate_startup(staged, manifest)

    assert result["state"] == "PASS"
    assert observed["env"]["GPOINT_AGATA_TENANT_ID"] == tenant_id
    assert not (root / "config/tenant-bindings.json").exists()
    diagnostics = Path(result["diagnostics"]).read_text()
    assert tenant_id not in diagnostics


def test_candidate_requires_exact_origin_main_head_not_just_a_main_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "WORKER_DIR", tmp_path)
    monkeypatch.setattr(migration, "ASSETS", ())
    git_calls = iter(("f" * 40, "a" * 40, "a" * 40 + "\trefs/heads/main"))
    monkeypatch.setattr(migration, "git", lambda *_args: next(git_calls))

    with pytest.raises(RuntimeError, match="candidate_must_be_origin_main"):
        migration.candidate()


def test_candidate_rejects_stale_local_origin_main_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "WORKER_DIR", tmp_path)
    monkeypatch.setattr(migration, "ASSETS", ())
    git_calls = iter(("a" * 40, "a" * 40, "b" * 40 + "\trefs/heads/main"))
    monkeypatch.setattr(migration, "git", lambda *_args: next(git_calls))

    with pytest.raises(RuntimeError, match="origin_main_ref_stale"):
        migration.candidate()


def test_candidate_accepts_only_clean_exact_origin_main_head(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "WORKER_DIR", tmp_path)
    monkeypatch.setattr(migration, "ASSETS", ())
    git_calls = iter(("a" * 40, "a" * 40, "a" * 40 + "\trefs/heads/main", ""))
    monkeypatch.setattr(migration, "git", lambda *_args: next(git_calls))

    candidate = migration.candidate()

    assert candidate["source_sha"] == "a" * 40
    assert candidate["assets"] == {}


def test_candidate_rejects_diagnostic_changes_in_candidate_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "WORKER_DIR", tmp_path)
    monkeypatch.setattr(migration, "ASSETS", ())
    main = "a" * 40
    def fake_git(*args):
        if args[:2] == ("rev-parse", "HEAD") or args[:2] == ("rev-parse", "origin/main"):
            return main
        if args[:2] == ("ls-remote", "--exit-code"):
            return main + "\trefs/heads/main"
        if args[:1] == ("status",):
            return " M services/agata/worker/migrate_worker_runtime.py\n M services/agata/tests/test_worker_runtime_migration.py"
        raise AssertionError(args)
    monkeypatch.setattr(migration, "git", fake_git)

    with pytest.raises(RuntimeError, match="candidate_worktree_dirty"):
        migration.candidate()


def test_candidate_startup_does_not_override_explicit_configured_tenant_pin(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    source_worker = legacy / "gpoint_mac_worker.py"
    source_worker.write_text("verified legacy worker")
    source = legacy / "tenant-bindings.json"
    source.write_text(json.dumps({"invalid": "must not be read when explicit config wins"}))
    source.chmod(0o600)
    root = tmp_path / "pool"
    root.mkdir()
    config = tmp_path / "worker.env"
    config.write_text("GPOINT_AGATA_TENANT_ID=explicit-pin\n")
    config.chmod(0o600)
    monkeypatch.setattr(migration, "LEGACY", legacy)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "CONFIG", config)

    environment = migration._candidate_startup_environment({"source_sha": "a" * 40, "assets": {}})

    assert "GPOINT_AGATA_TENANT_ID" not in environment


def test_candidate_startup_rejects_incomplete_pass_payload(tmp_path, monkeypatch):
    root = tmp_path / "support/agata-codex-worker"
    staged = tmp_path / "staged/candidate"
    (staged / ".venv/bin").mkdir(parents=True)
    (staged / ".venv/bin/python3.12").touch()
    (staged / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", tmp_path / "legacy-runtime")
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
    receipts = list((migration.ROOT / "receipts").glob("preflight-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["reason_code"] == "active_inference_present"
    assert not (migration.ROOT / "runtime").exists()


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
    snapshot = _fresh_gpoint_live_snapshot(value["tenant_id"])
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda *_args, **_kwargs: snapshot)
    migration._migrate_tenant_binding(manifest, _fresh_empty_queue_status(datetime.now(UTC)))
    result = json.loads((root / "config/tenant-bindings.json").read_text())
    assert result["tenant_id"] == value["tenant_id"]
    assert result["source_id"] != value["source_id"]
    assert result["snapshot_at"] == snapshot["queue_snapshot_at"]
    assert result["candidate_sha"] == manifest["source_sha"]
    assert result["worker_sha256"] == manifest["assets"]["gpoint_mac_worker.py"]


def test_existing_tenant_sidecar_is_rebound_only_from_verified_installed_runtime(tmp_path, monkeypatch):
    root = tmp_path / "support"
    runtime, installed_worker = _write_verified_runtime(root, "a" * 40, "a" * 40)
    current = root / "runtime/current"
    current.symlink_to(runtime.name)
    tenant_id = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    sidecar_path = root / "config/tenant-bindings.json"
    sidecar_path.parent.mkdir(parents=True)
    original = {
        "version": 1, "candidate_sha": "a" * 40,
        "worker_sha256": migration.sha256(installed_worker),
        "tenant_id": tenant_id,
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    sidecar_path.write_text(json.dumps(original))
    sidecar_path.chmod(0o600)
    config = tmp_path / "worker.env"
    config.write_text("GSALES_AGATA_TENANT_ID=c4d67889-877e-4d1b-a8d8-597c479d638f\n")
    config.chmod(0o600)
    manifest = {"source_sha": "c" * 40,
                "assets": {"gpoint_mac_worker.py": "d" * 64}}
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)
    monkeypatch.setattr(migration, "CONFIG", config)
    snapshot = _fresh_gpoint_live_snapshot(tenant_id)
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda *_args, **_kwargs: snapshot)

    assert migration._configured_tenant_id("GPOINT", manifest) == tenant_id
    migration._migrate_tenant_binding(manifest, _fresh_empty_queue_status(datetime.now(UTC)))

    updated = json.loads(sidecar_path.read_text())
    assert updated["tenant_id"] == original["tenant_id"]
    assert updated["source_id"] != original["source_id"]
    assert updated["snapshot_at"] == snapshot["queue_snapshot_at"]
    assert updated["candidate_sha"] == manifest["source_sha"]
    assert updated["worker_sha256"] == manifest["assets"]["gpoint_mac_worker.py"]


def test_current_tenant_binding_accepts_legacy_runtime_with_source_sha_directory(tmp_path, monkeypatch):
    root = tmp_path / "support"
    source_sha = "b" * 40
    runtime, worker = _write_verified_runtime(root, source_sha, source_sha)
    (root / "runtime/current").symlink_to(runtime.name)
    binding_path = root / "config/tenant-bindings.json"
    binding_path.parent.mkdir(parents=True)
    binding = {
        "version": 1,
        "candidate_sha": source_sha,
        "worker_sha256": migration.sha256(worker),
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "c" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    binding_path.write_text(json.dumps(binding))
    binding_path.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)

    assert migration._validated_current_tenant_binding() == binding


def test_current_tenant_binding_accepts_verified_product_migration_runtime(tmp_path, monkeypatch):
    root = tmp_path / "support"
    source_sha = "a" * 40
    source, _ = _write_verified_runtime(root, source_sha, source_sha)
    source_assets = json.loads((source / "bundle-manifest.json").read_text())
    runtime_root = root / "runtime"
    runtime_assets = {}
    runtime_id = "b" * 40
    runtime = runtime_root / runtime_id
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    for name in migration.ASSETS:
        destination = runtime / name
        shutil.copyfile(source / name, destination)
        destination.chmod(0o700 if name == "run_codex_worker.sh" else 0o600)
        runtime_assets[name] = migration.sha256(destination)
    python_runtime = {"resolved_binary_path": "/private/runtime/python", "python_binary_sha256": "c" * 64}
    rollback_runtime = {"resolved_binary_path": "/private/rollback/python", "python_binary_sha256": "d" * 64}
    bundle_sha = hashlib.sha256(json.dumps(
        runtime_assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    runtime_id = hashlib.sha256(json.dumps({
        "source_sha": source_sha,
        "bundle_sha256": bundle_sha,
        "python_runtime": python_runtime,
        "rollback_python_runtime": rollback_runtime,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:40]
    runtime.rename(runtime_root / runtime_id)
    runtime = runtime_root / runtime_id
    manifest = {
        "asset_source_root": str(source),
        "assets": runtime_assets,
        "bundle_sha256": bundle_sha,
        "candidate_kind": "INSTALLED_PRODUCT_WORKER_MIGRATION",
        "python_runtime": python_runtime,
        "rollback_python_runtime": rollback_runtime,
        "runtime_release_id": runtime_id,
        "source_sha": source_sha,
    }
    manifest_path = runtime / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    (runtime_root / "current").symlink_to(runtime_id)
    binding_path = root / "config/tenant-bindings.json"
    binding_path.parent.mkdir(parents=True)
    binding = {
        "version": 1,
        "candidate_sha": source_sha,
        "worker_sha256": runtime_assets["gpoint_mac_worker.py"],
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "c" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    binding_path.write_text(json.dumps(binding))
    binding_path.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)

    assert source_assets["assets"] == runtime_assets
    assert migration._validated_current_tenant_binding() == binding


def test_current_tenant_binding_rejects_product_migration_runtime_id_mismatch(tmp_path, monkeypatch):
    root = tmp_path / "support"
    source_sha = "a" * 40
    source, worker = _write_verified_runtime(root, source_sha, source_sha)
    manifest = {
        "asset_source_root": str(source),
        "assets": json.loads((source / "bundle-manifest.json").read_text())["assets"],
        "bundle_sha256": json.loads((source / "bundle-manifest.json").read_text())["bundle_sha256"],
        "candidate_kind": "INSTALLED_PRODUCT_WORKER_MIGRATION",
        "python_runtime": {"resolved_binary_path": "/private/runtime/python", "python_binary_sha256": "c" * 64},
        "rollback_python_runtime": {"resolved_binary_path": "/private/rollback/python", "python_binary_sha256": "d" * 64},
        "runtime_release_id": "d" * 40,
        "source_sha": source_sha,
    }
    runtime_root = root / "runtime"
    runtime = runtime_root / ("b" * 40)
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    for name in migration.ASSETS:
        shutil.copyfile(source / name, runtime / name)
        (runtime / name).chmod(0o700 if name == "run_codex_worker.sh" else 0o600)
    manifest_path = runtime / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    (runtime_root / "current").symlink_to(runtime.name)
    binding_path = root / "config/tenant-bindings.json"
    binding_path.parent.mkdir()
    binding_path.write_text(json.dumps({
        "version": 1,
        "candidate_sha": source_sha,
        "worker_sha256": migration.sha256(worker),
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "c" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }))
    binding_path.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)

    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._validated_current_tenant_binding()


def test_current_tenant_binding_rejects_manifest_sha_mismatch(tmp_path, monkeypatch):
    root = tmp_path / "support"
    runtime, worker = _write_verified_runtime(root, "b" * 40, "c" * 40)
    (root / "runtime/current").symlink_to(runtime.name)
    binding_path = root / "config/tenant-bindings.json"
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(json.dumps({
        "version": 1,
        "candidate_sha": "a" * 40,
        "worker_sha256": migration.sha256(worker),
        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "d" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }))
    binding_path.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)

    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._validated_current_tenant_binding()


def test_binding_migration_accepts_fresh_source_verified_same_tenant_snapshot():
    tenant_id = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    snapshot = _fresh_gpoint_live_snapshot(tenant_id)

    source_id = migration._validate_binding_migration_snapshot(
        tenant_id, snapshot, now=datetime.now(UTC),
    )

    provenance = snapshot["queue_snapshot_provenance"]
    assert source_id == (f"gpoint-vps:{provenance['release_sha']}"
                         f":api={provenance['api_image_id']}:agata={provenance['core_image_id']}")


def test_binding_migration_rejects_stale_queue_snapshot():
    tenant_id = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    snapshot = _fresh_gpoint_live_snapshot(
        tenant_id,
        captured_at=datetime.now(UTC) - timedelta(seconds=migration.QUEUE_SNAPSHOT_MAX_AGE_SECONDS + 1),
    )

    with pytest.raises(RuntimeError, match="queue_snapshot_stale"):
        migration._validate_binding_migration_snapshot(tenant_id, snapshot, now=datetime.now(UTC))


def test_binding_migration_rejects_wrong_tenant_snapshot():
    expected_tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    other_tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    snapshot = _fresh_gpoint_live_snapshot(other_tenant)

    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._validate_binding_migration_snapshot(expected_tenant, snapshot, now=datetime.now(UTC))


def test_binding_migration_does_not_write_when_live_tenant_differs(tmp_path, monkeypatch):
    root = tmp_path / "support"
    runtime, worker = _write_verified_runtime(root, "a" * 40, "a" * 40)
    (root / "runtime/current").symlink_to(runtime.name)
    sidecar_path = root / "config/tenant-bindings.json"
    sidecar_path.parent.mkdir(parents=True)
    expected_tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    wrong_tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    original = {
        "version": 1,
        "candidate_sha": "a" * 40,
        "worker_sha256": migration.sha256(worker),
        "tenant_id": expected_tenant,
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z",
    }
    sidecar_path.write_text(json.dumps(original))
    sidecar_path.chmod(0o600)
    config = root / "worker.env"
    config.write_text("GSALES_AGATA_TENANT_ID=c4d67889-877e-4d1b-a8d8-597c479d638f\n")
    config.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda *_args, **_kwargs: _fresh_gpoint_live_snapshot(wrong_tenant))
    manifest = {"source_sha": "c" * 40, "assets": {"gpoint_mac_worker.py": "d" * 64}}
    original_bytes = sidecar_path.read_bytes()

    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._migrate_tenant_binding(manifest, _fresh_empty_queue_status(datetime.now(UTC)))

    assert sidecar_path.read_bytes() == original_bytes


def test_existing_tenant_sidecar_with_unverified_runtime_is_not_rebound(tmp_path, monkeypatch):
    root = tmp_path / "support"
    (root / "config").mkdir(parents=True)
    original = {"version": 1, "candidate_sha": "a" * 40,
        "worker_sha256": "e" * 64, "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
        "snapshot_at": "2026-10-04T12:00:00Z"}
    path = root / "config/tenant-bindings.json"
    path.write_text(json.dumps(original))
    path.chmod(0o600)
    config = root / "worker.env"
    config.write_text("GSALES_AGATA_TENANT_ID=c4d67889-877e-4d1b-a8d8-597c479d638f\n")
    config.chmod(0o600)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "LEGACY", root)
    monkeypatch.setattr(migration, "CONFIG", config)
    manifest = {"source_sha": "c" * 40,
                "assets": {"gpoint_mac_worker.py": "d" * 64}}

    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._configured_tenant_id("GPOINT", manifest)
    with pytest.raises(RuntimeError, match="worker_tenant_binding_invalid"):
        migration._migrate_tenant_binding(manifest)
    assert json.loads(path.read_text()) == original


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
                        lambda *_args, **_kwargs: _fresh_empty_queue_status(datetime.now(UTC)))
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "stable-network-state")

    def stage(manifest):
        release = root.parent / ".candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release

    monkeypatch.setattr(migration, "_stage_runtime", stage)
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda _stage, _manifest: {"state": "PASS"})
    def install_staged(_staged, _manifest):
        (root / "runtime").mkdir(parents=True, exist_ok=True)
        (root / "runtime").chmod(0o700)
        release = root / "runtime" / ("a" * 40)
        release.mkdir(parents=True, exist_ok=True)
        release.chmod(0o700)
        return release
    monkeypatch.setattr(migration, "_install_staged_runtime", install_staged)
    monkeypatch.setattr(migration, "_ensure_pool_config", lambda: None)
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: 99999999)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])
    monkeypatch.setattr(migration, "_migrate_tenant_binding", lambda _manifest, _queue_status: None)
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
    monkeypatch.setattr(migration, "_wait_launchagent_unloaded", lambda *_args, **_kwargs: True, raising=False)
    monkeypatch.setattr(migration, "launchctl_loaded", lambda: True)
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
    diagnostics = Path(receipt["diagnostics"])
    assert diagnostics.is_file()
    assert diagnostics.parent.parent.parent.name == ".agata-codex-worker-diagnostics"
    assert (diagnostics.parent / "candidate.stderr.log").is_file()


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
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda *_args, **_kwargs: status)
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
    pause_observed = {"paused": False}
    queue_read_states = []

    def read_queue():
        queue_read_calls.append(True)
        queue_read_states.append((
            (root / "state/maintenance-stop").exists(), pause_observed["paused"],
        ))
        return next(queue_reads)
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda *_args, **_kwargs: read_queue())
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "stable-network-state")
    monkeypatch.setattr(migration, "_stage_runtime", lambda *_args: tmp_path / "candidate-stage")
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda *_args: {"state": "PASS"})
    def pause_idle_worker():
        pause_observed["paused"] = paused_pid is not None
        return paused_pid

    monkeypatch.setattr(migration, "_pause_idle_launchagent", pause_idle_worker)
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
    assert queue_read_states == ([(False, False), (True, True)] if paused_pid else [(False, False)])
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


@pytest.mark.parametrize("lane", ("GPOINT", "GSALES"))
def test_live_runtime_readonly_snapshot_is_normalized_with_provenance(lane):
    now = datetime.now(UTC)
    snapshot = _live_runtime_queue_snapshot(lane, now)

    normalized = migration.normalize_live_runtime_queue_snapshot(snapshot, lane, snapshot["tenant_id"])

    assert normalized["health"] == "HEALTHY"
    assert normalized["queue_snapshot_state"] == "VERIFIED"
    assert normalized["queue_snapshot_authority"] == "live_runtime_readonly_snapshot_v1"
    assert normalized["queue_snapshot_runtime_release_sha"] == "a" * 40
    assert normalized["queue_snapshot_tenant_id"] == snapshot["tenant_id"]
    status = _fresh_empty_queue_status(now)
    status["lanes"][lane] = normalized
    migration.require_fresh_empty_production_queues(status, now=now)


def test_live_runtime_readonly_snapshot_rejects_untrusted_or_mismatched_identity():
    now = datetime.now(UTC)
    bad_snapshots = (
        _live_runtime_queue_snapshot("GSALES", now) | {"source_system": "gpoint"},
        _live_runtime_queue_snapshot("GSALES", now) | {"tenant_identity_verified": False},
        _live_runtime_queue_snapshot("GSALES", now) | {"role_bypassrls": True},
        _live_runtime_queue_snapshot("GSALES", now) | {"visible_tenant_rows": True},
        _live_runtime_queue_snapshot("GSALES", now) | {"database_name": ""},
        _live_runtime_queue_snapshot("GSALES", now) | {"api_build_sha": "b" * 40},
        _live_runtime_queue_snapshot("GSALES", now) | {"snapshot_at": "not-a-time"},
    )
    for snapshot in bad_snapshots:
        with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
            migration.normalize_live_runtime_queue_snapshot(
                snapshot, "GSALES", "c4d67889-877e-4d1b-a8d8-597c479d638f",
            )


def test_remote_gsales_snapshot_uses_pinned_readonly_ephemeral_client_when_service_set_stopped():
    command = migration._remote_queue_snapshot_command("GSALES")
    collector = command.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    compile(collector, "<remote-gsales-queue-collector>", "exec")

    for container in (
        "compose-gsales-api-1",
        "compose-gsales-agata-api-1",
        "compose-gsales-agata-memory-worker-1",
    ):
        assert container in command
    assert '"docker", "run"' in command
    assert '"--pull=never"' in command
    assert '"shared_internal"' in command
    assert '"--read-only"' in command
    assert '"--cap-drop", "ALL"' in command
    assert '"--security-opt", "no-new-privileges"' in command
    assert '"--env-file", str(env_file)' in command
    assert '"--entrypoint", "python"' in command
    assert '"-i"' in command
    assert '"docker", "exec"' in command  # the existing healthy-GSales path remains supported
    assert '"docker", "start"' not in command and "compose up" not in command
    assert "stopped_gsales_mixed_state" in command
    assert "SNAPSHOT_EXPECTED_RELEASE_SHA" in command
    assert "org.opencontainers.image.revision" in command


def test_remote_gsales_snapshot_fails_closed_on_stopped_fingerprint_drift_or_readonly_identity_failure():
    command = migration._remote_queue_snapshot_command("GSALES")

    assert command.count("capture()") >= 2  # pre-run and post-run
    assert command.count("production_identity()") >= 2
    assert "if before != after" in command
    assert "STOPPED_GSALES_FINGERPRINT_MISMATCH" in command
    assert "AGATA_DATABASE_URL" in command
    assert "AGATA_TENANT_ID" in command
    snapshot_script = migration.QUEUE_SNAPSHOT_SCRIPT.read_text()
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in snapshot_script
    assert "transaction_read_only" in snapshot_script
    assert "role_bypassrls" in snapshot_script
    assert "same_login" in snapshot_script
    assert "queue_snapshot_trust_boundary_invalid" in snapshot_script
    assert '"docker", "rm", "-f", client_name' in command
    assert '"docker", "ps", "-a"' in command
    assert "stopped_gsales_client_cleanup_failed" in command
    assert "env_file.unlink(missing_ok=True)" in command
    assert '"stopped_runtime_fingerprint": before' in command


def test_stopped_gsales_ephemeral_collector_uses_private_allowlisted_env_and_cleans_up(tmp_path):
    command = migration._remote_queue_snapshot_command("GSALES")
    collector = command.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    release = "a" * 40
    tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    api_image, core_image = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    raw_snapshot = _live_runtime_queue_snapshot("GSALES", datetime.now(UTC))
    containers = {}
    for name, service, image, index in (
        ("compose-gsales-api-1", "gsales-api", api_image, 1),
        ("compose-gsales-agata-api-1", "gsales-agata-api", core_image, 2),
        ("compose-gsales-agata-memory-worker-1", "gsales-agata-memory-worker", core_image, 3),
    ):
        env = []
        if index == 2:
            env = [
                "AGATA_DATABASE_URL=postgresql+asyncpg://reader:db-secret-marker@postgres:5432/gsales",
                f"AGATA_TENANT_ID={tenant}", "AGATA_INTERNAL_API_TOKEN=synthetic-token",
                "AGATA_MESSAGE_AEAD_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "AGATA_REQUEST_FINGERPRINT_HMAC_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "AGATA_RETRIEVAL_QUERY_HMAC_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "AGATA_SHADOW_CANDIDATE_HMAC_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "AGATA_RESPONSE_FINGERPRINT_HMAC_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "AGATA_SOURCE_SYSTEM=gsales", "AGATA_SALES_DRAFTS_ENABLED=true",
                "AGATA_LLM_API_KEY=must-not-copy",
            ]
        containers[name] = [{
            "Name": "/" + name, "Id": f"{index:064x}", "Image": image, "RestartCount": 0,
            "Config": {"Image": f"immutable:{index}", "Labels": {
                "com.docker.compose.project": "compose", "com.docker.compose.service": service,
            }, "Env": env},
            "State": {"Status": "exited", "Running": False, "RestartCount": 0,
                      "StartedAt": "2026-10-01T00:00:00Z", "FinishedAt": "2026-10-02T00:00:00Z"},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        }]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker-argv.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as stream:
    stream.write(' '.join(args) + '\\n')
if args[0] == 'inspect':
    rows = json.loads(os.environ['FAKE_CONTAINERS'])[args[1]]
    marker = pathlib.Path(os.environ.get('FAKE_DRIFT_MARKER', '/nonexistent-marker'))
    if os.environ.get('FAKE_CONTAINER_DRIFT') == '1' and marker.exists() and args[1] == 'compose-gsales-agata-memory-worker-1':
        rows[0]['RestartCount'] += 1
    print(json.dumps(rows))
elif args[:2] == ['image', 'inspect']:
    image = args[2]
    print(json.dumps([{'Id': image, 'Config': {'Labels': {'org.opencontainers.image.revision': os.environ['FAKE_RELEASE']}}}]))
elif args[0] == 'run':
    assert '-i' in args, 'snapshot stdin must be attached to the exact-image client'
    env_path = pathlib.Path(args[args.index('--env-file') + 1])
    env_text = env_path.read_text()
    assert 'AGATA_SALES_DRAFTS_ENABLED=true' in env_text
    assert 'AGATA_LLM_PROVIDER=disabled' in env_text
    assert 'AGATA_SALES_PRODUCTION_MODE=false' in env_text
    assert 'AGATA_LLM_API_KEY=' in env_text
    assert 'must-not-copy' not in env_text
    assert 'db-secret-marker' not in ' '.join(args)
    sys.stdin.buffer.read()
    if os.environ.get('FAKE_CONTAINER_DRIFT') == '1':
        pathlib.Path(os.environ['FAKE_DRIFT_MARKER']).write_text('client ran')
    if os.environ.get('FAKE_PRODUCTION_DRIFT') == '1':
        pathlib.Path(os.environ['FAKE_PRODUCTION_ENV']).write_text(
            'RELEASE_SHA=' + 'b' * 40 + '\\nGSALES_AGATA_TENANT_ID=' + os.environ['FAKE_TENANT'] + '\\n')
    print(os.environ['FAKE_SNAPSHOT'])
elif args[:2] == ['rm', '-f']:
    pass
elif args[:2] == ['ps', '-a']:
    if os.environ.get('FAKE_CLIENT_REMAINS') == '1':
        print(args[args.index('--filter') + 1].removeprefix('name=^/').rstrip('$'))
else:
    raise SystemExit(9)
""")
    fake_docker.chmod(0o700)
    production = tmp_path / "production.env"
    production.write_text(f"RELEASE_SHA={release}\nGSALES_AGATA_TENANT_ID={tenant}\n")
    collector = collector.replace("/opt/gpoint/env/production.env", str(production))
    collector = collector.replace('dir="/run"', f'dir={str(tmp_path)!r}')
    env = os.environ | {
        "PATH": str(fake_bin) + ":" + os.environ["PATH"],
        "FAKE_DOCKER_LOG": str(log), "FAKE_CONTAINERS": json.dumps(containers),
        "FAKE_RELEASE": release, "FAKE_SNAPSHOT": json.dumps(raw_snapshot),
        "FAKE_PRODUCTION_ENV": str(production), "FAKE_TENANT": tenant,
        "SNAPSHOT_EXPECTED_RELEASE_SHA": release, "SNAPSHOT_EXPECTED_TENANT_ID": tenant,
    }

    result = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["collector_mode"] == "stopped_runtime_ephemeral_v1"
    assert snapshot["stopped_runtime_state"] == "INTENTIONALLY_STOPPED"
    assert snapshot["stopped_runtime_fingerprint"]
    args_logged = log.read_text()
    assert "db-secret-marker" not in args_logged
    assert "must-not-copy" not in args_logged
    assert not list(tmp_path.glob("gpoint-gsales-snapshot-*"))

    # The fake query response is 0/0; without the trusted runtime flag, the
    # SQL would omit sales_draft_inference rows, so that zero must not pass.
    drafts_disabled = json.loads(json.dumps(containers))
    core_env = drafts_disabled["compose-gsales-agata-api-1"][0]["Config"]["Env"]
    core_env[core_env.index("AGATA_SALES_DRAFTS_ENABLED=true")] = "AGATA_SALES_DRAFTS_ENABLED=false"
    env["FAKE_CONTAINERS"] = json.dumps(drafts_disabled)
    log.write_text("")
    rejected_zero = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)
    assert rejected_zero.returncode != 0
    assert "stopped_gsales_runtime_config_invalid" in rejected_zero.stderr
    assert '"run"' not in log.read_text()

    mixed = json.loads(json.dumps(containers))
    mixed["compose-gsales-api-1"][0]["State"].update({"Status": "running", "Running": True})
    env["FAKE_CONTAINERS"] = json.dumps(mixed)
    log.write_text("")
    failed = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)
    assert failed.returncode != 0
    assert "stopped_gsales_mixed_state" in failed.stderr
    assert '"run"' not in log.read_text()

    production.write_text(f"RELEASE_SHA={release}\nGSALES_AGATA_TENANT_ID={tenant}\n")
    env["FAKE_CONTAINERS"] = json.dumps(containers)
    env["FAKE_PRODUCTION_DRIFT"] = "1"
    log.write_text("")
    drifted = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)
    assert drifted.returncode != 0
    assert "STOPPED_GSALES_FINGERPRINT_MISMATCH" in drifted.stderr

    production.write_text(f"RELEASE_SHA={release}\nGSALES_AGATA_TENANT_ID={tenant}\n")
    env.pop("FAKE_PRODUCTION_DRIFT", None)
    marker = tmp_path / "post-client-container-change"
    marker.unlink(missing_ok=True)
    env["FAKE_CONTAINER_DRIFT"] = "1"
    env["FAKE_DRIFT_MARKER"] = str(marker)
    log.write_text("")
    changed_container = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)
    assert changed_container.returncode != 0
    assert "STOPPED_GSALES_FINGERPRINT_MISMATCH" in changed_container.stderr
    assert not list(tmp_path.glob("gpoint-gsales-snapshot-*"))
    marker.unlink(missing_ok=True)

    production.write_text(f"RELEASE_SHA={release}\nGSALES_AGATA_TENANT_ID={tenant}\n")
    env.pop("FAKE_CONTAINER_DRIFT", None)
    env["FAKE_CLIENT_REMAINS"] = "1"
    log.write_text("")
    leftover = subprocess.run([os.sys.executable, "-c", collector], capture_output=True, text=True, env=env)
    assert leftover.returncode != 0
    assert "stopped_gsales_client_cleanup_failed" in leftover.stderr
    assert not list(tmp_path.glob("gpoint-gsales-snapshot-*"))


def test_stopped_gsales_snapshot_is_explicitly_inactive_and_requires_container_fingerprint():
    now = datetime.now(UTC)
    snapshot = _live_runtime_queue_snapshot("GSALES", now) | {
        "collector_mode": "stopped_runtime_ephemeral_v1",
        "stopped_runtime_state": "INTENTIONALLY_STOPPED",
        "stopped_runtime_fingerprint": "c" * 64,
    }

    normalized = migration.normalize_live_runtime_queue_snapshot(
        snapshot, "GSALES", snapshot["tenant_id"],
    )
    assert normalized["health"] == "INTENTIONALLY_STOPPED"
    assert normalized["queue_snapshot_authority"] == "stopped_runtime_ephemeral_readonly_v1"
    status = _fresh_empty_queue_status(now)
    status["lanes"]["GSALES"] = normalized
    migration.require_fresh_empty_production_queues(status, now=now)

    for invalid in (
        snapshot | {"stopped_runtime_fingerprint": "bad"},
        snapshot | {"stopped_runtime_state": "HEALTHY"},
        snapshot | {"transaction_read_only": False},
        snapshot | {"tenant_identity_verified": False},
        snapshot | {"same_login": False},
        snapshot | {"role_superuser": True},
        snapshot | {"role_bypassrls": True},
        snapshot | {"visible_tenant_rows": 2},
    ):
        with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
            migration.normalize_live_runtime_queue_snapshot(
                invalid, "GSALES", snapshot["tenant_id"],
            )
    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.normalize_live_runtime_queue_snapshot(snapshot, "GPOINT", snapshot["tenant_id"])


def test_queue_status_preserves_live_gpoint_and_accepts_only_explicit_stopped_gsales_evidence(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    status = _fresh_empty_queue_status(now)
    status["lanes"]["GSALES"] = {
        "health": "DEGRADED", "health_error_code": "worker_gateway_network_error",
        "queue_snapshot_state": "UNAVAILABLE", "queue_snapshot_error_code": "worker_gateway_http_404",
    }
    root = tmp_path / "runtime"
    runtime = root / "runtime" / ("a" * 40)
    runtime.mkdir(parents=True)
    (runtime / ".venv/bin").mkdir(parents=True)
    python = runtime / ".venv/bin/python3.12"
    python.touch()
    python.chmod(0o700)
    worker_dir = tmp_path / "candidate-worker"
    worker_dir.mkdir()
    worker = worker_dir / "gpoint_mac_worker.py"
    worker.touch()
    (root / "runtime/current").symlink_to(runtime)
    config = tmp_path / "worker.env"
    config.write_text(f"GSALES_AGATA_TENANT_ID={tenant}\n")
    config.chmod(0o600)
    stopped = _live_runtime_queue_snapshot("GSALES", now) | {
        "collector_mode": "stopped_runtime_ephemeral_v1",
        "stopped_runtime_state": "INTENTIONALLY_STOPPED",
        "stopped_runtime_fingerprint": "d" * 64,
    }
    normalized = migration.normalize_live_runtime_queue_snapshot(stopped, "GSALES", tenant)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot", lambda *_args, **_kwargs: normalized)
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(status), ""))

    result = migration._read_production_queue_status({"source_sha": "a" * 40})
    evidence = migration.require_fresh_empty_production_queues(result, now=now)

    assert result["lanes"]["GPOINT"]["health"] == "HEALTHY"
    assert result["lanes"]["GSALES"]["health"] == "INTENTIONALLY_STOPPED"
    assert evidence["lanes"]["GSALES"]["pending_jobs"] == 0


def test_queue_status_404_and_gateway_network_error_use_live_readonly_snapshots(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    unavailable_status = _fresh_empty_queue_status(now)
    unavailable_status["pool"]["state"] = "DEGRADED"
    unavailable_status["pool"]["total"] = 1
    unavailable_status["pool_snapshot_at"] = (now.replace(year=now.year - 1)).isoformat()
    unavailable_status["slots"] = {"slot-01": {"state": "IDLE", "lane": None, "job_id": None}}
    unavailable_status["current_job_present"] = False
    for lane in ("GPOINT", "GSALES"):
        unavailable_status["lanes"][lane] = {
            "health": "DEGRADED", "health_error_code": "worker_gateway_network_error",
            "queue_snapshot_state": "UNAVAILABLE",
            "queue_snapshot_error_code": "worker_gateway_http_404",
        }
    snapshots = {
        lane: _live_runtime_queue_snapshot(lane, now)
        for lane in ("GPOINT", "GSALES")
    }
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if any("gpoint_mac_worker.py" in part for part in command):
            return subprocess.CompletedProcess(command, 0, json.dumps(unavailable_status), "")
        remote_command = kwargs["env"]["REMOTE_CMD"]
        lane = "GSALES" if "compose-gsales-agata-api-1" in remote_command else "GPOINT"
        return subprocess.CompletedProcess(command, 0, json.dumps(snapshots[lane]), "")

    config = tmp_path / "worker.env"
    config.write_text(
        "GPOINT_AGATA_TENANT_ID=81a4ba28-6de5-4b03-8332-0661ba4d46f4\n"
        "GSALES_AGATA_TENANT_ID=c4d67889-877e-4d1b-a8d8-597c479d638f\n"
    )
    config.chmod(0o600)
    root = tmp_path / "runtime"
    _create_installed_runtime(root)
    slot_lock = root / "slots/slot-01/ownership.lock"
    slot_lock.parent.mkdir(parents=True)
    slot_lock.touch(mode=0o600)
    slot_lock.chmod(0o600)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "f" * 40, "assets": {}})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))
    monkeypatch.setattr(migration, "VPS_RUNNER", Path("/trusted/gpoint-vps-runner"))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(os, "access", lambda *_args: True)
    monkeypatch.setattr(migration.subprocess, "run", run)

    status = migration._read_production_queue_status()
    evidence = migration.require_fresh_empty_production_queues(status, now=now)

    assert set(evidence["lanes"]) == {"GPOINT", "GSALES"}
    assert len(commands) == 3
    assert "compose-agata-api-1" in commands[1][1]["env"]["REMOTE_CMD"]
    assert "compose-gsales-agata-api-1" in commands[2][1]["env"]["REMOTE_CMD"]
    assert all("queue_snapshot_authority" in status["lanes"][lane]
               for lane in ("GPOINT", "GSALES"))
    assert status["pool"]["state"] == "IDLE_CONFIRMED"
    assert status["pool_snapshot_authority"] == "mac_worker_live_idle_inventory_v1"


def test_queue_status_uses_installed_runtime_python_not_orchestrator_python(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    runtime = root / "runtime" / ("a" * 40)
    python = runtime / ".venv/bin/python3.12"
    worker = tmp_path / "candidate-worker/gpoint_mac_worker.py"
    python.parent.mkdir(parents=True)
    python.touch()
    python.chmod(0o700)
    worker.parent.mkdir(parents=True)
    worker.touch()
    (runtime / "gpoint_mac_worker.py").touch()
    (root / "runtime/current").symlink_to(runtime)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker.parent)
    monkeypatch.setattr(migration, "_resolve_python312",
                        lambda: "/codex/runtime/python-without-worker-dependencies")
    status = _fresh_empty_queue_status(datetime.now(UTC))
    observed = {}
    tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    config = tmp_path / "worker.env"
    config.write_text(f"GSALES_AGATA_TENANT_ID={tenant}\n")
    config.chmod(0o600)
    fresh_snapshot = migration.normalize_live_runtime_queue_snapshot(
        _live_runtime_queue_snapshot("GSALES", datetime.now(UTC)), "GSALES", tenant,
    )

    def run(command, **_kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, json.dumps(status), "")

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "_refresh_local_pool_evidence", lambda _status, **_kwargs: None)
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot", lambda *_args, **_kwargs: fresh_snapshot)
    result = migration._read_production_queue_status({"source_sha": "a" * 40})

    assert observed["command"][0] == str(python)
    assert observed["command"][1] == str(worker)
    assert result["lanes"]["GPOINT"]["queue_snapshot_state"] == "VERIFIED"


def test_local_poll_lock_does_not_replace_authoritative_queue_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    slot_lock = root / "slots/slot-01/ownership.lock"
    slot_lock.parent.mkdir(parents=True)
    slot_lock.touch(mode=0o600)
    slot_lock.chmod(0o600)
    descriptor = os.open(slot_lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status.update({"configured_concurrency": 1, "current_job_present": False,
                   "launch_agent_loaded": True,
                   "slots": {"slot-01": {"state": "IDLE", "job_id": None}}})
    status["pool"]["total"] = 1
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))
    try:
        migration._refresh_local_pool_evidence(status)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert status["pool_snapshot_authority"] == "mac_worker_live_idle_inventory_v1"
    # This local poll/slot lock establishes no queue state; both verified
    # tenant-scoped DB snapshots are still mandatory before transition.
    for lane in ("GPOINT", "GSALES"):
        status["lanes"][lane] = {"health": "HEALTHY", "queue_snapshot_state": "UNAVAILABLE"}
    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.require_fresh_empty_production_queues(status)


def test_local_inventory_is_explicit_idle_evidence_not_worker_pool_health(monkeypatch):
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status.update({"configured_concurrency": 1, "current_job_present": False,
                   "launch_agent_loaded": True,
                   "slots": {"slot-01": {"state": "IDLE", "job_id": None}}})
    status["pool"]["total"] = 1
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))

    migration._refresh_local_pool_evidence(status)

    assert status["pool"]["state"] == "IDLE_CONFIRMED"
    assert status["pool_snapshot_authority"] == "mac_worker_live_idle_inventory_v1"
    assert status["pool_snapshot_inventory"] == {
        "configured_concurrency": 1,
        "slot_count": 1,
        "all_slots_idle": True,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "active_inference": False,
    }
    evidence = migration.validate_fresh_production_queue_snapshots(status)
    assert evidence["pool_authority"] == "mac_worker_live_idle_inventory_v1"

    with pytest.raises(RuntimeError, match="production_queue_snapshot_invalid"):
        migration.validate_fresh_production_queue_snapshots({**status, "configured_concurrency": True})
    status["configured_concurrency"] = True


def test_local_inventory_accepts_three_dedicated_idle_slots_at_provider_n1(monkeypatch):
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status.update({
        "configured_concurrency": 1,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "slots": {
            "slot-01": {"state": "IDLE", "lane": "GPOINT", "job_id": None},
            "slot-02": {"state": "IDLE", "lane": "GSALES", "job_id": None},
            "slot-03": {"state": "IDLE", "lane": "LAB", "job_id": None},
        },
    })
    status["pool"]["total"] = 3
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))

    migration._refresh_local_pool_evidence(status)

    assert status["configured_concurrency"] == 1
    assert status["pool_snapshot_inventory"]["slot_count"] == 3
    assert set(status["slots"]) == {"slot-01", "slot-02", "slot-03"}
    assert migration.validate_fresh_production_queue_snapshots(status)["busy_slots"] == 0


def test_local_inventory_accepts_eros_slot_without_weakening_production_queue_gates(monkeypatch):
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status.update({
        "configured_concurrency": 1,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "slots": {
            "slot-01": {"state": "IDLE", "lane": "GPOINT", "job_id": None},
            "slot-02": {"state": "IDLE", "lane": "GSALES", "job_id": None},
            "slot-03": {"state": "IDLE", "lane": "LAB", "job_id": None},
            "slot-04": {"state": "IDLE", "lane": "EROS", "job_id": None},
        },
    })
    status["pool"]["total"] = 4
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))

    migration._refresh_local_pool_evidence(status)

    assert status["configured_concurrency"] == 1
    assert status["pool_snapshot_inventory"]["slot_count"] == 4
    assert set(status["slots"]) == {"slot-01", "slot-02", "slot-03", "slot-04"}
    assert migration.validate_fresh_production_queue_snapshots(status)["busy_slots"] == 0


def test_local_inventory_rejects_noncanonical_dedicated_slot_set(monkeypatch):
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status.update({
        "configured_concurrency": 1,
        "current_job_present": False,
        "launch_agent_loaded": True,
        "slots": {
            "slot-01": {"state": "IDLE", "lane": "GPOINT", "job_id": None},
            "slot-02": {"state": "IDLE", "lane": "GSALES", "job_id": None},
            "slot-04": {"state": "IDLE", "lane": "LAB", "job_id": None},
        },
    })
    status["pool"]["total"] = 3
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))

    with pytest.raises(RuntimeError, match="production_queue_status_unavailable"):
        migration._refresh_local_pool_evidence(status)


def test_confirmed_gateway_transport_queue_failure_uses_live_runtime_bootstrap(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    monkeypatch.setattr(migration, "ROOT", root)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GPOINT"].update({
        "queue_snapshot_state": "UNAVAILABLE",
        "queue_snapshot_error_code": "worker_gateway_network_error",
    })
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(status), ""))
    snapshots = {
        lane: _live_runtime_queue_snapshot(lane, datetime.now(UTC))
        for lane in ("GPOINT", "GSALES")
    }
    snapshots = {
        lane: migration.normalize_live_runtime_queue_snapshot(raw, lane, raw["tenant_id"])
        for lane, raw in snapshots.items()
    }
    calls = []
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "worker watch"))
    monkeypatch.setattr(migration, "_configured_tenant_id", lambda lane, _manifest: snapshots[lane]["queue_snapshot_tenant_id"])
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda lane, _tenant, **_kwargs: calls.append(lane) or snapshots[lane])

    result = migration._read_production_queue_status({"source_sha": "a" * 40})
    evidence = migration.require_fresh_empty_production_queues(result)

    assert calls == ["GPOINT", "GSALES"]
    assert set(evidence["lanes"]) == {"GPOINT", "GSALES"}
    assert result["pool"]["state"] == "IDLE_CONFIRMED"
    assert all(result["lanes"][lane]["queue_snapshot_authority"] == "live_runtime_readonly_snapshot_v1"
               for lane in ("GPOINT", "GSALES"))


def test_gateway_transport_failure_does_not_substitute_for_vps_queue_counts(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    monkeypatch.setattr(migration, "ROOT", root)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GPOINT"].update({
        "queue_snapshot_state": "UNAVAILABLE",
        "queue_snapshot_error_code": "worker_gateway_network_error",
    })
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(status), ""))
    raw = _live_runtime_queue_snapshot("GPOINT", datetime.now(UTC))
    raw["pending_inference_jobs"] = 1
    gsales_raw = _live_runtime_queue_snapshot("GSALES", datetime.now(UTC))
    snapshots = {
        "GPOINT": migration.normalize_live_runtime_queue_snapshot(raw, "GPOINT", raw["tenant_id"]),
        "GSALES": migration.normalize_live_runtime_queue_snapshot(gsales_raw, "GSALES", gsales_raw["tenant_id"]),
    }
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False,
        "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "worker watch"))
    monkeypatch.setattr(migration, "_configured_tenant_id",
                        lambda lane, _manifest: snapshots[lane]["queue_snapshot_tenant_id"])
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda lane, _tenant, **_kwargs: snapshots[lane])

    result = migration._read_production_queue_status({"source_sha": "a" * 40})

    with pytest.raises(migration.PreflightFailure, match="production_queue_not_empty"):
        migration.require_fresh_empty_production_queues(result)


@pytest.mark.parametrize("error_code", (
    "worker_gateway_auth_rejected", "worker_gateway_http_403",
    "worker_queue_status_identity_invalid", "worker_queue_status_counts_invalid",
    "unclassified_queue_error", None, {"unexpected": "shape"},
))
def test_unconfirmed_or_non_transport_queue_errors_do_not_allow_live_fallback(error_code):
    lane_status = {
        "health": "HEALTHY", "queue_snapshot_state": "UNAVAILABLE",
        "queue_snapshot_error_code": error_code,
    }
    assert migration._live_snapshot_fallback_allowed(lane_status) is False


def test_gsales_unavailable_error_is_not_misclassified_as_gpoint_transport_failure(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    monkeypatch.setattr(migration, "ROOT", root)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GSALES"].update({
        "health": "DEGRADED", "health_error_code": "worker_gateway_network_error",
        "queue_snapshot_state": "UNAVAILABLE",
        "queue_snapshot_error_code": "authoritative_queue_snapshot_unavailable",
    })
    tenant = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    config = tmp_path / "worker.env"
    config.write_text(f"GSALES_AGATA_TENANT_ID={tenant}\n")
    config.chmod(0o600)
    live_snapshot = migration.normalize_live_runtime_queue_snapshot(
        _live_runtime_queue_snapshot("GSALES", datetime.now(UTC)), "GSALES", tenant,
    )
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(status), ""))
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "gpoint_mac_worker.py watch"))
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda *_args, **_kwargs: live_snapshot)

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_production_queue_status()

    assert raised.value.reason_code == "lane_health_queue_error_combination_disallowed"
    assert raised.value.lane == "GSALES"
    assert raised.value.safe_fields["health_error_code"] == "worker_gateway_network_error"
    assert "queue_snapshot_error_code" not in raised.value.safe_fields


def test_worker_status_timeout_has_structured_reason_code(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    worker_dir = tmp_path / "candidate-worker"
    worker_dir.mkdir()
    (worker_dir / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=35)

    monkeypatch.setattr(migration.subprocess, "run", timeout)

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_production_queue_status({"source_sha": "a" * 40})

    assert raised.value.reason_code == "worker_status_timeout"
    assert raised.value.stage == "worker_status"
    assert raised.value.lane is None


@pytest.mark.parametrize(
    ("completed", "expected_reason"),
    (
        (subprocess.CompletedProcess([], 7, "", "private stderr"), "worker_status_nonzero_exit"),
        (subprocess.CompletedProcess([], 0, "not json", ""), "worker_status_invalid_json"),
    ),
)
def test_worker_status_nonzero_and_invalid_json_have_distinct_reason_codes(
        tmp_path, monkeypatch, completed, expected_reason):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    worker_dir = tmp_path / "candidate-worker"
    worker_dir.mkdir()
    (worker_dir / "gpoint_mac_worker.py").touch()
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration.subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_production_queue_status()

    assert raised.value.reason_code == expected_reason
    assert raised.value.stage == "worker_status"
    assert "private stderr" not in str(raised.value)


def test_unclassified_runner_timeout_has_lane_scoped_fail_closed_reason(tmp_path, monkeypatch):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    worker_dir = tmp_path / "candidate-worker"
    worker_dir.mkdir()
    (worker_dir / "gpoint_mac_worker.py").touch()
    config = tmp_path / "worker.env"
    config.write_text("GPOINT_AGATA_TENANT_ID=81a4ba28-6de5-4b03-8332-0661ba4d46f4\n")
    config.chmod(0o600)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    for lane in ("GPOINT", "GSALES"):
        status["lanes"][lane].update({
            "health": "DEGRADED", "health_error_code": "worker_gateway_network_error",
            "queue_snapshot_state": "UNAVAILABLE", "queue_snapshot_error_code": "worker_gateway_http_404",
        })
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "VPS_RUNNER", Path("/trusted/runner"))
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "worker watch"))

    def run(command, **_kwargs):
        if any("gpoint_mac_worker.py" in str(part) for part in command):
            return subprocess.CompletedProcess(command, 0, json.dumps(status), "")
        raise subprocess.TimeoutExpired(command, timeout=30)

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(migration.os, "access", lambda *_args: True)

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_production_queue_status({"source_sha": "a" * 40})

    assert raised.value.reason_code == "authoritative_vps_runner_deadline_exceeded"
    assert raised.value.stage == "authoritative_vps_snapshot"
    assert raised.value.lane == "GPOINT"
    assert raised.value.error_category == "runner"
    assert raised.value.exit_class == "unknown"


def test_runner_interrupted_transport_is_the_only_non_timeout_transport_retry(monkeypatch):
    tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    budget = migration.QueueSnapshotRetryBudget()
    calls = []
    verified = migration.normalize_live_runtime_queue_snapshot(
        _live_runtime_queue_snapshot("GPOINT", datetime.now(UTC)), "GPOINT", tenant,
    )

    def once(_lane, _tenant, *, timeout_seconds):
        calls.append(timeout_seconds)
        if len(calls) == 1:
            raise migration.PreflightFailure("ssh_transport_interrupted", stage="authoritative_vps_snapshot",
                                             lane="GPOINT", error_category="transport",
                                             exit_class="transient_transport")
        return verified

    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot_once", once)
    monkeypatch.setattr(migration.time, "sleep", lambda _seconds: None)

    assert migration._read_live_runtime_queue_snapshot("GPOINT", tenant, retry_budget=budget) == verified
    assert len(calls) == 2
    assert [entry["outcome"] for entry in budget.attempts] == ["RETRYABLE_FAILURE", "RESPONSE"]


def test_vps_runner_timeout_then_valid_snapshot_retries_with_shared_budget(tmp_path, monkeypatch):
    tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    runner = tmp_path / "gpoint-vps-runner"
    runner.write_text("#!/bin/sh\nexit 0\n")
    runner.chmod(0o700)
    monkeypatch.setattr(migration, "VPS_RUNNER", runner)
    monkeypatch.setattr(migration, "_remote_queue_snapshot_command", lambda _lane: "read-only-command")
    snapshot = _live_runtime_queue_snapshot("GPOINT", datetime.now(UTC))
    responses = iter((subprocess.CompletedProcess([], 75, "", ""),
                      subprocess.CompletedProcess([], 0, json.dumps(snapshot), "")))
    def run_runner(*_args, env, **_kwargs):
        response = next(responses)
        if response.returncode == 75:
            Path(env["GPOINT_REMOTE_ATTEMPT_STATUS_FILE"]).write_text("PRE_EXEC_TRANSPORT_FAILURE:255\n")
            response.stderr = "ssh: connect timed out"
        return response
    monkeypatch.setattr(migration.subprocess, "run", run_runner)
    monkeypatch.setattr(migration.time, "sleep", lambda _seconds: None)
    budget = migration.QueueSnapshotRetryBudget()

    result = migration._read_live_runtime_queue_snapshot("GPOINT", tenant, retry_budget=budget)

    assert result["queue_snapshot_state"] == "VERIFIED"
    assert [entry["outcome"] for entry in budget.attempts] == ["RETRYABLE_FAILURE", "RESPONSE"]
    assert budget.attempts[0]["reason_code"] == "ssh_pre_exec_transport_timeout"


def test_runner_transport_marker_without_transient_evidence_does_not_retry(tmp_path, monkeypatch):
    runner = tmp_path / "gpoint-vps-runner"
    runner.touch()
    runner.chmod(0o700)
    monkeypatch.setattr(migration, "VPS_RUNNER", runner)
    monkeypatch.setattr(migration, "_remote_queue_snapshot_command", lambda _lane: "read-only-command")
    calls = []
    def run_runner(*_args, env, **_kwargs):
        calls.append(True)
        Path(env["GPOINT_REMOTE_ATTEMPT_STATUS_FILE"]).write_text("PRE_EXEC_TRANSPORT_FAILURE:255\n")
        return subprocess.CompletedProcess([], 75, "", "ssh: Connection refused")
    monkeypatch.setattr(migration.subprocess, "run", run_runner)

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_live_runtime_queue_snapshot("GPOINT", "81a4ba28-6de5-4b03-8332-0661ba4d46f4")

    assert raised.value.reason_code == "ssh_transport_unavailable"
    assert raised.value.exit_class == "permanent_transport"
    assert calls == [True]


@pytest.mark.parametrize(
    ("returncode", "stderr", "reason", "exit_class"),
    (
        (255, "Permission denied", "ssh_authentication_failed", "permanent_transport"),
        (255, "Host key verification failed", "ssh_host_identity_failed", "identity_provenance"),
        (255, "Connection refused", "ssh_transport_unavailable", "permanent_transport"),
        (255, "Connection timed out", "ssh_transport_timeout_unconfirmed", "unknown"),
        (124, "", "authoritative_vps_runner_deadline_exceeded", "unknown"),
    ),
)
def test_permanent_transport_and_identity_errors_never_retry(
        tmp_path, monkeypatch, returncode, stderr, reason, exit_class):
    runner = tmp_path / "gpoint-vps-runner"
    runner.touch()
    runner.chmod(0o700)
    monkeypatch.setattr(migration, "VPS_RUNNER", runner)
    monkeypatch.setattr(migration, "_remote_queue_snapshot_command", lambda _lane: "read-only-command")
    calls = []
    monkeypatch.setattr(migration.subprocess, "run", lambda *_args, **_kwargs: (
        calls.append(True) or subprocess.CompletedProcess([], returncode, "", stderr)
    ))
    budget = migration.QueueSnapshotRetryBudget()

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_live_runtime_queue_snapshot("GPOINT", "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
                                                   retry_budget=budget)

    assert raised.value.reason_code == reason
    assert raised.value.exit_class == exit_class
    assert calls == [True]
    assert budget.retries_remaining == migration.QUEUE_SNAPSHOT_MAX_RETRIES


def test_transient_vps_timeout_retries_twice_then_returns_verified_snapshot(monkeypatch):
    now = datetime.now(UTC)
    tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
    verified = migration.normalize_live_runtime_queue_snapshot(
        _live_runtime_queue_snapshot("GPOINT", now), "GPOINT", tenant,
    )
    budget = migration.QueueSnapshotRetryBudget()
    calls = []
    sleeps = []

    def read_once(lane, expected_tenant_id, *, timeout_seconds):
        calls.append((lane, expected_tenant_id, timeout_seconds))
        if len(calls) < 3:
            raise migration.PreflightFailure(
                "ssh_transport_timeout", stage="authoritative_vps_snapshot", lane=lane,
                exit_class="transient_transport",
            )
        return verified

    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot_once", read_once)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)

    result = migration._read_live_runtime_queue_snapshot("GPOINT", tenant, retry_budget=budget)

    assert result["queue_snapshot_state"] == "VERIFIED"
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert all(call[2] <= migration.QUEUE_SNAPSHOT_REQUEST_TIMEOUT_SECONDS for call in calls)
    assert [attempt["outcome"] for attempt in budget.attempts] == [
        "RETRYABLE_FAILURE", "RETRYABLE_FAILURE", "RESPONSE",
    ]
    assert all(attempt["lane"] == "GPOINT" for attempt in budget.attempts)
    assert all(attempt["started_at"] and attempt["finished_at"] for attempt in budget.attempts)


def test_retry_budget_is_shared_across_lanes_and_second_lane_failure_blocks_batch(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    tenants = {
        "GPOINT": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
        "GSALES": "c4d67889-877e-4d1b-a8d8-597c479d638f",
    }
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    worker_dir = tmp_path / "candidate-worker"
    worker_dir.mkdir()
    (worker_dir / "gpoint_mac_worker.py").touch()
    config = tmp_path / "worker.env"
    config.write_text("\n".join(f"{lane}_AGATA_TENANT_ID={tenant}" for lane, tenant in tenants.items()) + "\n")
    config.chmod(0o600)
    runner = tmp_path / "gpoint-vps-runner"
    runner.touch()
    runner.chmod(0o700)
    status = _fresh_empty_queue_status(now)
    for lane in tenants:
        status["lanes"][lane].update({
            "health": "DEGRADED", "health_error_code": "worker_gateway_network_error",
            "queue_snapshot_state": "UNAVAILABLE", "queue_snapshot_error_code": "worker_gateway_http_404",
        })
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration, "CONFIG", config)
    monkeypatch.setattr(migration, "VPS_RUNNER", runner)
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "worker watch"))
    monkeypatch.setattr(migration, "_configured_tenant_id", lambda lane, _manifest: tenants[lane])
    monkeypatch.setattr(migration.os, "access", lambda *_args: True)
    monkeypatch.setattr(migration.subprocess, "run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(status), ""))
    budget = migration.QueueSnapshotRetryBudget()
    calls = []

    def read_once(lane, _tenant, *, timeout_seconds):
        calls.append((lane, timeout_seconds))
        if lane == "GPOINT":
            return migration.normalize_live_runtime_queue_snapshot(
                _live_runtime_queue_snapshot(lane, datetime.now(UTC)), lane, tenants[lane],
            )
        raise migration.PreflightFailure(
            "ssh_transport_timeout", stage="authoritative_vps_snapshot", lane=lane,
            exit_class="transient_transport",
        )

    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot_once", read_once)
    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_production_queue_status({"source_sha": "a" * 40}, retry_budget=budget)

    assert raised.value.reason_code == "ssh_transport_timeout"
    assert raised.value.lane == "GSALES"
    assert [lane for lane, _timeout in calls] == ["GPOINT", "GSALES", "GSALES", "GSALES"]
    assert len(calls) == 4
    assert [attempt["lane"] for attempt in budget.attempts] == ["GPOINT", "GSALES", "GSALES", "GSALES"]


@pytest.mark.parametrize("reason", [
    "remote_tenant_or_lane_identity_mismatch",
    "queue_snapshot_provenance_invalid",
    "production_queue_not_empty",
])
def test_identity_provenance_and_nonempty_queue_errors_never_retry(monkeypatch, reason):
    budget = migration.QueueSnapshotRetryBudget()
    calls = []

    def read_once(lane, _tenant, *, timeout_seconds):
        calls.append(lane)
        raise migration.PreflightFailure(reason, stage="queue_validation", lane=lane,
                                         exit_class="identity_provenance")

    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot_once", read_once)
    with pytest.raises(migration.PreflightFailure) as raised:
        migration._read_live_runtime_queue_snapshot(
            "GPOINT", "81a4ba28-6de5-4b03-8332-0661ba4d46f4", retry_budget=budget,
        )

    assert raised.value.reason_code == reason
    assert calls == ["GPOINT"]
    assert budget.retries_remaining == migration.QUEUE_SNAPSHOT_MAX_RETRIES


def test_queue_retry_budget_enforces_one_common_deadline(monkeypatch):
    ticks = iter((100.0, 103.0, 111.0))
    monkeypatch.setattr(migration.time, "monotonic", lambda: next(ticks))
    budget = migration.QueueSnapshotRetryBudget(deadline_seconds=10)

    assert budget.request_timeout_seconds() == 7
    with pytest.raises(migration.PreflightFailure) as raised:
        budget.request_timeout_seconds()
    assert raised.value.reason_code == "authoritative_snapshot_deadline_exceeded"


@pytest.mark.parametrize(
    ("health", "health_error", "queue_error", "expected_reason"),
    (
        ("UNKNOWN", None, None, "lane_health_invalid"),
        ("DEGRADED", "worker_gateway_auth_error", "worker_gateway_http_404",
         "lane_health_queue_error_combination_disallowed"),
        ("HEALTHY", None, "worker_gateway_http_403", "queue_snapshot_error_not_allowlisted"),
    ),
)
def test_local_health_and_queue_error_states_have_structured_reasons(
        tmp_path, monkeypatch, health, health_error, queue_error, expected_reason):
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_launchagent_pid", lambda: (123, "worker watch"))
    status = {
        "pool": {"busy": 0, "total": 1}, "current_job_present": False,
        "configured_concurrency": 1, "launch_agent_loaded": True,
        "slots": {"slot-01": {"state": "IDLE", "job_id": None, "lane": "GPOINT"}},
        "lanes": {"GPOINT": {
            "configured": True, "health": health, "health_error_code": health_error,
            "queue_snapshot_state": "UNAVAILABLE", "queue_snapshot_error_code": queue_error,
        }, "GSALES": {"configured": False}},
    }

    with pytest.raises(migration.PreflightFailure) as raised:
        migration._refresh_local_pool_evidence(status)

    assert raised.value.reason_code == expected_reason
    assert raised.value.lane == "GPOINT"


def test_idle_requirement_classifies_stale_and_invalid_queue_evidence():
    now = datetime.now(UTC)
    stale = _fresh_empty_queue_status(now - timedelta(seconds=migration.QUEUE_SNAPSHOT_MAX_AGE_SECONDS + 1))
    with pytest.raises(migration.PreflightFailure) as stale_error:
        migration.require_fresh_empty_production_queues(stale, now=now)
    assert stale_error.value.reason_code == "queue_snapshot_stale"

    invalid = _fresh_empty_queue_status(now)
    invalid["lanes"]["GPOINT"]["pending_inference_jobs"] = "0"
    with pytest.raises(migration.PreflightFailure) as invalid_error:
        migration.require_fresh_empty_production_queues(invalid, now=now)
    assert invalid_error.value.reason_code == "queue_snapshot_counts_invalid"


def test_preflight_receipt_is_private_structured_and_contains_no_raw_evidence(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    failure = migration.PreflightFailure(
        "authoritative_vps_snapshot_timeout", stage="authoritative_vps_snapshot", lane="GPOINT",
        safe_fields={"runner_exit_code": 124, "queue_snapshot_error_code": "worker_gateway_network_error",
                     "health_error_code": "private.example"},
        error_category="transport", exit_class="transient_transport",
    )

    attempts = [{
        "lane": "GPOINT", "attempt": 1, "outcome": "RETRYABLE_FAILURE",
        "reason_code": "ssh_transport_timeout", "error_category": "transport",
        "exit_class": "transient_transport", "started_at": "2026-10-08T10:00:00+00:00",
        "finished_at": "2026-10-08T10:00:01+00:00", "endpoint": "private.example",
        "stderr": "secret queue payload",
    }]
    receipt_path = migration._write_preflight_receipt(
        "APPLY_INITIAL", failure, attempts=attempts,
        started_at="2026-10-08T09:59:59+00:00",
    )
    receipt = json.loads(receipt_path.read_text())

    assert receipt["outcome"] == "BLOCKED"
    assert receipt["schema_version"] == 2
    assert receipt["reason_code"] == "authoritative_vps_snapshot_timeout"
    assert receipt["stage"] == "APPLY_INITIAL"
    assert receipt["failure_stage"] == "authoritative_vps_snapshot"
    assert receipt["lane"] == "GPOINT"
    assert receipt["runner_exit_code"] == 124
    assert receipt["error_category"] == "transport"
    assert receipt["exit_class"] == "transient_transport"
    assert receipt["queue_snapshot_error_code"] == "worker_gateway_network_error"
    assert "health_error_code" not in receipt
    assert receipt["started_at"] == "2026-10-08T09:59:59+00:00"
    assert receipt["finished_at"]
    assert receipt["attempts"][0]["lane"] == "GPOINT"
    assert receipt["attempts"][0]["started_at"] == "2026-10-08T10:00:00+00:00"
    assert "endpoint" not in receipt["attempts"][0] and "stderr" not in receipt["attempts"][0]
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    serialized = json.dumps(receipt)
    assert "tenant" not in serialized
    assert "pending_inference_jobs" not in serialized
    assert "queue_snapshot_provenance" not in serialized
    assert "private.example" not in serialized and "secret queue payload" not in serialized
    assert str(tmp_path) not in serialized
    assert "stderr" not in receipt and "stdout" not in receipt


def test_apply_records_reason_code_before_staging_when_queue_gate_fails(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        migration.PreflightFailure("authoritative_vps_snapshot_timeout", stage="authoritative_vps_snapshot",
                                    lane="GPOINT", safe_fields={"runner_exit_code": 124})))
    monkeypatch.setattr(migration, "_stage_runtime", lambda *_args: pytest.fail("must abort before staging"))

    with pytest.raises(migration.PreflightFailure) as raised:
        migration.apply()

    assert raised.value.reason_code == "authoritative_vps_snapshot_timeout"
    receipts = list((root / "receipts").glob("preflight-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["stage"] == "APPLY_INITIAL"
    assert receipt["reason_code"] == "authoritative_vps_snapshot_timeout"
    assert receipt["lane"] == "GPOINT"
    assert receipt["runner_exit_code"] == 124
    assert not (root / "state/maintenance-stop").exists()


def test_missing_preflight_receipt_is_reported_and_still_blocks_before_staging(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(migration, "ROOT", root)
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    monkeypatch.setattr(migration, "candidate", lambda: {"source_sha": "a" * 40})
    monkeypatch.setattr(migration, "inventory", lambda: {
        "launchagent_loaded": True, "production_release_lock_busy": False, "active_inference": False,
    })
    monkeypatch.setattr(migration, "_write_preflight_receipt",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("private path")))
    monkeypatch.setattr(migration, "_read_production_queue_status", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        migration.PreflightFailure("authoritative_vps_snapshot_timeout", stage="authoritative_vps_snapshot",
                                    lane="GPOINT", exit_class="transient_transport")))
    monkeypatch.setattr(migration, "_stage_runtime", lambda *_args: pytest.fail("must not stage"))

    with pytest.raises(migration.PreflightFailure) as raised:
        migration.apply()

    assert raised.value.reason_code == "authoritative_vps_snapshot_timeout"
    assert raised.value.receipt_written is False
    assert not (root / "state/maintenance-stop").exists()


@pytest.mark.parametrize(
    ("health", "health_error", "queue_error"),
    (
        ("DEGRADED", "worker_gateway_auth_error", "worker_gateway_http_404"),
        ("DEGRADED", "worker_gateway_http_500", "worker_gateway_http_404"),
        ("HEALTHY", None, "worker_gateway_http_403"),
    ),
)
def test_live_snapshot_fallback_rejects_nonallowlisted_worker_errors(
        tmp_path, monkeypatch, health, health_error, queue_error):
    root = tmp_path / "worker-runtime"
    _create_installed_runtime(root)
    monkeypatch.setattr(migration, "ROOT", root)
    status = _fresh_empty_queue_status(datetime.now(UTC))
    status["lanes"]["GPOINT"].update({
        "health": health,
        "health_error_code": health_error,
        "queue_snapshot_state": "UNAVAILABLE",
        "queue_snapshot_error_code": queue_error,
    })
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(status), ""))
    monkeypatch.setattr(migration, "_read_live_runtime_queue_snapshot",
                        lambda *_args: pytest.fail("nonallowlisted state must not use VPS fallback"))

    with pytest.raises(RuntimeError, match="production_queue_status_unavailable"):
        migration._read_production_queue_status()


def _live_runtime_queue_snapshot(lane, now):
    source = lane.lower()
    tenant = "81a4ba28-6de5-4b03-8332-0661ba4d46f4" if lane == "GPOINT" else "c4d67889-877e-4d1b-a8d8-597c479d638f"
    api_image = "sha256:" + "1" * 64
    core_image = "sha256:" + "2" * 64
    return {
        "snapshot_version": "agata_live_queue_snapshot_v1",
        "lane": lane, "source_system": source, "source_id": (
            f"agata-live-queue-snapshot-v1:{source}:release={'a' * 40}:api={api_image}:core={core_image}:tenant={tenant}"
        ),
        "release_sha": "a" * 40, "api_build_sha": "a" * 40, "core_build_sha": "a" * 40,
        "api_image_id": api_image, "core_image_id": core_image,
        "tenant_id": tenant, "tenant_identity_verified": True,
        "snapshot_at": now.isoformat(), "transaction_read_only": True,
        "same_login": True, "role_superuser": False, "role_bypassrls": False,
        "visible_tenant_rows": 1, "database_name": "gpoint", "pending_inference_jobs": 0,
        "running_inference_jobs": 0, "effective_inference_ttl_seconds": 600,
    }


def _create_installed_runtime(root):
    runtime = root / "runtime" / ("a" * 40)
    python = runtime / ".venv/bin/python3.12"
    python.parent.mkdir(parents=True)
    python.touch()
    python.chmod(0o700)
    (runtime / "gpoint_mac_worker.py").touch()
    (root / "runtime/current").symlink_to(runtime)
    return runtime


def _fresh_empty_queue_status(now):
    return {
        "launch_agent_loaded": True,
        "configured_concurrency": 1,
        "pool": {"state": "HEALTHY", "busy": 0, "total": 1},
        "pool_snapshot_at": now.isoformat(),
        "current_job_present": False,
        "slots": {"slot-01": {"state": "IDLE", "job_id": None}},
        "lanes": {
            lane: {"health": "HEALTHY", "queue_snapshot_state": "VERIFIED",
                   "queue_snapshot_source_id": f"agata-worker-queue-status-v1:{lane.lower()}",
                   "queue_snapshot_at": now.isoformat(), "pending_inference_jobs": 0,
                   "running_inference_jobs": 0}
            for lane in ("GPOINT", "GSALES")
        },
    }


@pytest.mark.parametrize(("preserve_stopped_gsales", "paused_fingerprint_drift", "snapshot_failure"), [
    (False, False, None), (True, False, None), (True, True, None),
    (True, False, "absent"), (True, False, "stale"),
])
def test_in_place_upgrade_keeps_application_support_root_and_verified_rollback(
        tmp_path, monkeypatch, preserve_stopped_gsales, paused_fingerprint_drift, snapshot_failure):
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
    queue_status_calls = []
    def queue_status(*_args, **_kwargs):
        queue_status_calls.append(None)
        value = _fresh_empty_queue_status(datetime.now(UTC))
        if preserve_stopped_gsales:
            value["lanes"]["GSALES"] = _stopped_gsales_status()["lanes"]["GSALES"]
            if paused_fingerprint_drift and len(queue_status_calls) > 1:
                value["lanes"]["GSALES"]["queue_snapshot_provenance"]["stopped_runtime_fingerprint"] = "b" * 64
        return value
    monkeypatch.setattr(migration, "_read_production_queue_status", queue_status)
    monkeypatch.setattr(migration, "_network_state_sha256", lambda: "unchanged")
    def stage(_manifest):
        release = tmp_path / "candidate-stage" / manifest["source_sha"]
        release.mkdir(parents=True)
        return release
    monkeypatch.setattr(migration, "_stage_runtime", stage)
    monkeypatch.setattr(migration, "_validate_candidate_startup", lambda _stage, _manifest: {"state": "PASS"})
    def install(_staged, _manifest):
        release = root / "runtime" / manifest["source_sha"]
        release.mkdir(parents=True, mode=0o700)
        release.parent.chmod(0o700)
        release.chmod(0o700)
        return release
    monkeypatch.setattr(migration, "_install_staged_runtime", install)
    monkeypatch.setattr(migration, "_ensure_pool_config", lambda: None)
    monkeypatch.setattr(migration, "_pause_idle_launchagent", lambda: 1234)
    monkeypatch.setattr(migration, "_running_inference_pids", lambda: [])
    monkeypatch.setattr(migration.os, "kill", lambda *_args: None)
    monkeypatch.setattr(migration, "_migrate_tenant_binding", lambda _manifest, _queue_status: None)
    status = {"configured_concurrency": 1, "lanes": {
        "GPOINT": {"health": "HEALTHY"},
        "GSALES": {"health": "DEGRADED" if preserve_stopped_gsales else "HEALTHY"},
        "LAB": {"state": "AVAILABLE"}}}
    monkeypatch.setattr(migration, "_health_check", lambda: status)
    restart = {"verification": "PASS", "adjudication": {"status": status}}
    def kickstart(_sha, stopped_gsales_baseline=None):
        worker_status = root / "status/worker-status.json"
        value = json.loads(worker_status.read_text())
        value["targets"] = {
            "GPOINT": {"last_poll_at": "after", "last_error_code": None},
            "GSALES": {"last_poll_at": "after", "last_error_code": None},
        }
        worker_status.write_text(json.dumps(value))
        result = json.loads(json.dumps(restart))
        if stopped_gsales_baseline is not None:
            result["adjudication"]["attempts"] = [{"stopped_gsales_proof": stopped_gsales_baseline}]
        return result
    monkeypatch.setattr(migration, "_kickstart_and_verify", kickstart)
    monkeypatch.setattr(migration, "_assert_stopped_gsales_proof_matches", lambda expected: expected)
    poll_baselines = []
    def wait_for_polls(baseline, stopped_gsales_baseline=None):
        poll_baselines.append(baseline)
        if stopped_gsales_baseline is None:
            return {"passed": True}
        return {"passed": True, "stopped_gsales_proof": stopped_gsales_baseline,
                "lanes": {"GPOINT": {"poll_advanced": True, "stale_error_cleared": True},
                          "GSALES": {"poll_required": False,
                                     "poll_status": "NOT_REQUIRED_INTENTIONALLY_STOPPED",
                                     "stopped_proof_verified": True}}}
    monkeypatch.setattr(migration, "_wait_for_successful_polls", wait_for_polls)
    monkeypatch.setattr(migration, "launchctl_loaded", lambda: True)
    transition_events = []
    monkeypatch.setattr(migration, "_wait_launchagent_unloaded",
                        lambda pid, **_kwargs: transition_events.append(("unloaded", pid)), raising=False)
    def run(command, **kwargs):
        if command[:2] == ["launchctl", "bootout"]:
            transition_events.append(("bootout", None))
        elif command[:2] == ["launchctl", "bootstrap"]:
            transition_events.append(("bootstrap", None))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(migration.subprocess, "run", run)

    if snapshot_failure:
        live_job = root / "queues/durable-job.json"
        live_job.parent.mkdir()
        live_job.write_text('{"state":"queued-after-prepare"}')
        prior_snapshot = rollback_base / manifest["source_sha"] / "legacy-runtime"
        if snapshot_failure == "stale":
            shutil.copytree(root, prior_snapshot)
            prior_snapshot.parent.chmod(0o700)
            (prior_snapshot / "queues/durable-job.json").unlink()
        def failed_snapshot(_backup):
            raise RuntimeError("rollback_snapshot_refresh_mismatch")
        monkeypatch.setattr(migration, "_prepare_legacy_snapshot", failed_snapshot)

        with pytest.raises(RuntimeError, match="rollback_snapshot_refresh_mismatch"):
            migration.apply(preserve_stopped_gsales=True)

        assert live_job.read_text() == '{"state":"queued-after-prepare"}'
        assert plist.read_bytes() == old_plist
        assert not (root / "runtime/current").exists()
        assert not (root / "state/maintenance-stop").exists()
        receipt = json.loads((root / "receipts/runtime-migration-rollback.json").read_text())
        assert receipt["state"] == "ROLLED_BACK"
        assert receipt["launchagent_restored"] is True
        return

    if paused_fingerprint_drift:
        with pytest.raises(RuntimeError, match="stopped_gsales_preboot_fingerprint_changed"):
            migration.apply(preserve_stopped_gsales=True)
        assert not any(event[0] == "bootout" for event in transition_events)
        assert not (root / "state/maintenance-stop").exists()
        return

    result = migration.apply(preserve_stopped_gsales=preserve_stopped_gsales)

    assert result["state"] == "COMPLETE"
    assert [event[0] for event in transition_events] == ["bootout", "unloaded", "bootstrap"]
    assert transition_events[1][1] == 1234
    assert poll_baselines == [{
        "current_job_present": False,
        "targets": {
            "GPOINT": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
            "GSALES": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
        },
    }]
    assert root.is_dir()
    assert (root / "runtime/current").resolve() == root / "runtime" / manifest["source_sha"]
    for lane in ("GPOINT", "GSALES", "LAB"):
        assert (root / "runtime/slots" / lane / "current").resolve() == root / "runtime" / manifest["source_sha"]
    snapshot = rollback_base / manifest["source_sha"]
    assert (snapshot / "legacy-runtime/gpoint_mac_worker.py").read_text() == "existing V3 worker"
    assert (snapshot / "original-launchagent.plist").read_bytes() == old_plist
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [
        str(root / "runtime/current/run_codex_worker.sh"), "watch"]
    if preserve_stopped_gsales:
        assert result["gsales_runtime_mode"] == "INTENTIONALLY_STOPPED"
        assert result["gsales_stopped_proof_before"]["stopped_runtime_fingerprint"] == "a" * 64
        assert result["fresh_poll_check"]["lanes"]["GSALES"]["poll_required"] is False


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
    {"state": "AVAILABLE", "presence": "PERMANENT", "inference_gate": "CLOSED_NO_APPROVAL"},
    {"state": "AVAILABLE", "presence": "PERMANENT", "inference_gate": "CLOSED_EXPIRED"},
    {"state": "AVAILABLE", "presence": "PERMANENT", "inference_gate": "CLOSED_CONSUMED"},
    {"state": "AVAILABLE", "last_error_code": "lab_approval_required",
     "inference_gate": "APPROVAL_REQUIRED"},
    {"state": "DISABLED", "inference_gate": "CLOSED_DISABLED"},
])
@pytest.mark.parametrize("preserve_stopped_gsales", [False, True])
def test_restart_snapshot_requires_exact_runtime_identity_and_all_lanes(
        tmp_path, monkeypatch, lab_status, preserve_stopped_gsales):
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
                  "GSALES": {"health": "DEGRADED" if preserve_stopped_gsales else "HEALTHY",
                             "current_job_present": False},
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
    stopped_baseline = {
        "state": "INTENTIONALLY_STOPPED", "source_id": "source",
        "stopped_runtime_fingerprint": "a" * 64, "release_sha": "b" * 40,
        "api_image_id": "sha256:" + "1" * 64, "core_image_id": "sha256:" + "2" * 64,
        "tenant_id": "c4d67889-877e-4d1b-a8d8-597c479d638f",
        "pending_jobs": 0, "running_jobs": 0,
    }
    if preserve_stopped_gsales:
        monkeypatch.setattr(migration, "_assert_stopped_gsales_proof_matches",
                            lambda expected: expected)

    evidence = migration._restart_state_snapshot(
        expected_source_sha=source_sha,
        stopped_gsales_baseline=stopped_baseline if preserve_stopped_gsales else None,
    )

    assert evidence["passed"] is True
    assert all(evidence["checks"].values())
    assert evidence["pid"] == 4312
    assert evidence["runtime_source_sha"] == source_sha
    assert evidence["checks"]["lab_approval_fail_closed"] is True
    assert ("gsales_intentionally_stopped" in evidence["checks"]) is preserve_stopped_gsales
    assert evidence["worker_status"]["configured_concurrency"] == 1
    assert set(evidence["worker_status"]["lanes"]) == {"GPOINT", "GSALES", "LAB"}
    if lab_status.get("presence"):
        assert evidence["worker_status"]["lanes"]["LAB"]["presence"] == "PERMANENT"


@pytest.mark.parametrize("lab_status", [
    {"state": "AVAILABLE", "presence": "PERMANENT", "inference_gate": "OPEN_APPROVED"},
    {"state": "DEGRADED", "presence": "PERMANENT", "inference_gate": "CLOSED_NO_APPROVAL"},
    {"state": "AVAILABLE", "inference_gate": "CLOSED_NO_APPROVAL"},
    {"state": "AVAILABLE", "presence": "TEMPORARY", "inference_gate": "CLOSED_NO_APPROVAL"},
    {"state": "UNKNOWN", "presence": "PERMANENT", "inference_gate": "CLOSED_NO_APPROVAL"},
])
def test_restart_snapshot_does_not_treat_open_or_broken_lab_gate_as_closed(tmp_path, monkeypatch, lab_status):
    root = tmp_path / "AGATA Workers"
    source_sha = "c" * 40
    release = root / "runtime" / source_sha
    _write_runtime_bundle(release, source_sha)
    (root / "runtime/current").symlink_to(source_sha)
    monkeypatch.setattr(migration, "ROOT", root)
    launchctl = (f"gui/{os.getuid()}/{migration.LABEL} = {{\n"
                "\tstate = running\n\tpid = 4312\n}\n")
    worker_command = (f"{release}/.venv/bin/python3.12 "
                      f"{release}/gpoint_mac_worker.py watch")
    status = {
        "configured_concurrency": 1, "current_job_present": False,
        "pool": {"state": "running"},
        "lanes": {"GPOINT": {"health": "HEALTHY", "current_job_present": False},
                  "GSALES": {"health": "HEALTHY", "current_job_present": False}, "LAB": lab_status},
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

    assert evidence["checks"]["lab_approval_fail_closed"] is False


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
    monkeypatch.setattr(migration, "_wait_launchagent_unloaded", lambda *_args, **_kwargs: True, raising=False)
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0))

    evidence = migration.rollback()

    assert evidence["state"] == "ROLLED_BACK"
    assert plist.read_bytes() == exact_plist
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(legacy / "run_codex_worker.sh"), "watch"]
    assert (legacy / "run_codex_worker.sh").read_text() == "legacy worker bytes"


def test_sigtermed_launchagent_is_not_reported_as_loaded(monkeypatch):
    output = f'gui/{os.getuid()}/{migration.LABEL} = {{\n\tstate = SIGTERMed\n}}\n'
    monkeypatch.setattr(migration.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, output, ""))

    assert migration.launchctl_loaded() is False


def test_wait_launchagent_unloaded_waits_for_terminated_job_and_old_pid(monkeypatch):
    service = f"gui/{os.getuid()}/{migration.LABEL}"
    calls = []
    responses = [
        subprocess.CompletedProcess(["launchctl", "print", service], 0,
                                    f'{service} = {{\n\tstate = SIGTERMed\n\tpid = 4312\n}}\n', ""),
        subprocess.CompletedProcess(["ps", "-axo", "pid=,command="], 0,
                                    "4312 /runtime/gpoint_mac_worker.py watch\n", ""),
        subprocess.CompletedProcess(["launchctl", "print", service], 1, "",
                                    f'Could not find service "{migration.LABEL}" in domain'),
        subprocess.CompletedProcess(["ps", "-axo", "pid=,command="], 0, "", ""),
    ]

    def run(command, **kwargs):
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(migration.subprocess, "run", run)
    monkeypatch.setattr(migration.time, "sleep", lambda _delay: None)

    assert migration._wait_launchagent_unloaded(4312, deadline=1) is True
    assert calls == [
        ["launchctl", "print", service], ["ps", "-axo", "pid=,command="],
        ["launchctl", "print", service], ["ps", "-axo", "pid=,command="],
    ]


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


def test_stopped_gsales_apply_mode_requires_fresh_gpoint_poll_and_fresh_matching_stopped_proof(
        tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "AGATA Workers")
    status = migration.ROOT / "status/worker-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T07:00:00+00:00", "last_error_code": None},
        "GSALES": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
    }}))
    baseline = {"targets": {
        "GPOINT": {"last_poll_at": "2026-10-05T06:59:00+00:00", "last_error_code": "worker_target_unavailable"},
        "GSALES": {"last_poll_at": "before", "last_error_code": "worker_target_unavailable"},
    }}
    stopped = {"source_id": "live:gSales", "stopped_runtime_fingerprint": "a" * 64,
               "release_sha": "b" * 40, "tenant_id": "c4d67889-877e-4d1b-a8d8-597c479d638f",
               "api_image_id": "sha256:" + "1" * 64, "core_image_id": "sha256:" + "2" * 64,
               "pending_jobs": 0, "running_jobs": 0, "state": "INTENTIONALLY_STOPPED"}
    monkeypatch.setattr(migration, "_read_stopped_gsales_proof", lambda _expected: stopped)

    result = migration._wait_for_successful_polls(baseline, deadline=0.01,
                                                   stopped_gsales_baseline=stopped)

    assert result["passed"] is True
    assert result["lanes"]["GPOINT"]["poll_advanced"] is True
    assert result["lanes"]["GSALES"] == {
        "poll_required": False,
        "poll_status": "NOT_REQUIRED_INTENTIONALLY_STOPPED",
        "stopped_proof_verified": True,
    }
    assert result["stopped_gsales_proof"] == stopped


def test_stopped_gsales_apply_mode_fails_when_gpoint_poll_does_not_advance(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "ROOT", tmp_path / "AGATA Workers")
    status = migration.ROOT / "status/worker-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"targets": {
        "GPOINT": {"last_poll_at": "same", "last_error_code": None},
        "GSALES": {"last_poll_at": "same", "last_error_code": "worker_target_unavailable"},
    }}))
    stopped = {"stopped_runtime_fingerprint": "a" * 64}
    monkeypatch.setattr(migration, "_read_stopped_gsales_proof", lambda _expected: stopped)

    result = migration._wait_for_successful_polls(
        {"targets": {"GPOINT": {"last_poll_at": "same"}}}, deadline=0,
        stopped_gsales_baseline=stopped)

    assert result["passed"] is False


def test_stopped_gsales_apply_mode_fails_on_post_restart_fingerprint_drift(monkeypatch):
    expected = {"stopped_runtime_fingerprint": "a" * 64}
    monkeypatch.setattr(migration, "_read_stopped_gsales_proof",
                        lambda _expected: {"stopped_runtime_fingerprint": "b" * 64})

    with pytest.raises(RuntimeError, match="stopped_gsales_fingerprint_changed"):
        migration._assert_stopped_gsales_proof_matches(expected)


def _stopped_gsales_status(now=None):
    captured = now or datetime.now(UTC)
    tenant_id = "c4d67889-877e-4d1b-a8d8-597c479d638f"
    snapshot = _live_runtime_queue_snapshot("GSALES", captured)
    snapshot.update({
        "collector_mode": "stopped_runtime_ephemeral_v1",
        "stopped_runtime_state": "INTENTIONALLY_STOPPED",
        "stopped_runtime_fingerprint": "a" * 64,
    })
    normalized = migration.normalize_live_runtime_queue_snapshot(snapshot, "GSALES", tenant_id)
    return {"lanes": {"GSALES": normalized}}


def test_stopped_gsales_proof_binds_runtime_identity_and_readonly_queue_provenance():
    proof = migration._stopped_gsales_proof(_stopped_gsales_status())

    assert proof["state"] == "INTENTIONALLY_STOPPED"
    assert proof["stopped_runtime_fingerprint"] == "a" * 64
    assert proof["release_sha"] == "a" * 40
    assert proof["tenant_id"] == "c4d67889-877e-4d1b-a8d8-597c479d638f"
    assert proof["pending_jobs"] == proof["running_jobs"] == 0


@pytest.mark.parametrize("mutation", [
    lambda lane: lane.update(health="HEALTHY"),
    lambda lane: lane["queue_snapshot_provenance"].update(transaction_read_only=False),
    lambda lane: lane["queue_snapshot_provenance"].update(role_bypassrls=True),
    lambda lane: lane["queue_snapshot_provenance"].update(release_sha="d" * 40),
    lambda lane: lane["queue_snapshot_provenance"].update(stopped_runtime_fingerprint="invalid"),
    lambda lane: lane.update(pending_inference_jobs=1),
])
def test_stopped_gsales_proof_rejects_invalid_state_or_provenance(mutation):
    status = _stopped_gsales_status()
    mutation(status["lanes"]["GSALES"])

    with pytest.raises(RuntimeError, match="stopped_gsales_proof_invalid"):
        migration._stopped_gsales_proof(status)


def test_stopped_gsales_proof_rejects_stale_snapshot():
    stale = datetime.now(UTC) - timedelta(seconds=migration.QUEUE_SNAPSHOT_MAX_AGE_SECONDS + 1)

    with pytest.raises(RuntimeError, match="stopped_gsales_proof_invalid"):
        migration._stopped_gsales_proof(_stopped_gsales_status(stale))


def test_full_apply_requires_explicit_stopped_gsales_optin_before_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "TRANSITION_LOCK", tmp_path / "transition.lock")
    preflight = {
        "manifest": {"source_sha": "a" * 40},
        "queue_evidence": {},
        "status": {"lanes": {"GSALES": {
            "queue_snapshot_authority": "stopped_runtime_ephemeral_readonly_v1",
        }}},
    }
    monkeypatch.setattr(migration, "_run_safety_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setattr(migration, "_pause_idle_launchagent",
                        lambda: pytest.fail("opt-in must be checked before pausing"))

    with pytest.raises(RuntimeError, match="preserve_stopped_gsales_opt_in_required"):
        migration.apply()


def test_stage_runtime_uses_running_python312_when_missing_from_path(tmp_path, monkeypatch):
    worker_dir = tmp_path / "source"
    worker_dir.mkdir()
    assets = {}
    for name in migration.ASSETS:
        payload = f"asset:{name}".encode()
        source = worker_dir / name
        source.write_bytes(payload)
        source.chmod(0o644)
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
    assert all((release / name).stat().st_mode & 0o777 ==
               (0o700 if name == "run_codex_worker.sh" else 0o600)
               for name in migration.ASSETS)
    assert (release / "lab_promptfoo_compat_manifest.json").is_file()


def _make_staged_runtime_fixture(tmp_path):
    runtime = tmp_path / "runtime" / ("a" * 40)
    runtime.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    assets = {}
    for name in migration.ASSETS:
        payload = f"verified:{name}".encode()
        path = runtime / name
        path.write_bytes(payload)
        path.chmod(0o700 if name == "run_codex_worker.sh" else 0o600)
        assets[name] = hashlib.sha256(payload).hexdigest()
    manifest = {"source_sha": "a" * 40, "assets": assets, "bundle_sha256": "b" * 64}
    manifest_path = runtime / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)
    python = runtime / ".venv/bin/python3.12"
    python.parent.mkdir(parents=True, mode=0o700)
    python.write_text("test interpreter")
    python.chmod(0o700)
    return runtime, manifest


def test_verify_staged_runtime_rejects_world_readable_worker_asset(tmp_path, monkeypatch):
    runtime, manifest = _make_staged_runtime_fixture(tmp_path)
    (runtime / "lab_queue.py").chmod(0o644)
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0))

    with pytest.raises(RuntimeError, match="worker_runtime_asset_permissions_invalid"):
        migration._verify_staged_runtime(runtime, manifest)


def test_verify_staged_runtime_rejects_symlink_worker_asset(tmp_path, monkeypatch):
    runtime, manifest = _make_staged_runtime_fixture(tmp_path)
    asset = runtime / "lab_queue.py"
    payload = tmp_path / "replacement.py"
    payload.write_bytes(asset.read_bytes())
    payload.chmod(0o600)
    asset.unlink()
    asset.symlink_to(payload)
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0))

    with pytest.raises(RuntimeError, match="worker_runtime_asset_symlink_invalid"):
        migration._verify_staged_runtime(runtime, manifest)


def test_verify_staged_runtime_rejects_unowned_worker_asset(tmp_path, monkeypatch):
    runtime, manifest = _make_staged_runtime_fixture(tmp_path)
    real_uid = os.getuid()
    real_lstat = Path.lstat
    asset = runtime / "lab_queue.py"

    def fake_lstat(path):
        result = real_lstat(path)
        if path == asset:
            return type(result)(tuple(result[:4]) + (real_uid + 1,) + tuple(result[5:]))
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(migration.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0))

    with pytest.raises(RuntimeError, match="worker_runtime_owner_invalid"):
        migration._verify_staged_runtime(runtime, manifest)


def test_stage_runtime_rejects_symlink_source_asset(tmp_path, monkeypatch):
    worker_dir = tmp_path / "source"
    worker_dir.mkdir()
    assets = {}
    for name in migration.ASSETS:
        payload = f"asset:{name}".encode()
        path = worker_dir / name
        path.write_bytes(payload)
        path.chmod(0o644)
        assets[name] = hashlib.sha256(payload).hexdigest()
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes((worker_dir / "lab_queue.py").read_bytes())
    (worker_dir / "lab_queue.py").unlink()
    (worker_dir / "lab_queue.py").symlink_to(replacement)
    manifest = {"source_sha": "c" * 40, "assets": assets, "bundle_sha256": "d" * 64}
    monkeypatch.setattr(migration, "WORKER_DIR", worker_dir)
    monkeypatch.setattr(migration, "ROOT", tmp_path / "new-runtime")

    def run(command, **kwargs):
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin/python3.12"
            python.parent.mkdir(parents=True)
            python.write_text("staged Python 3.12")
            python.chmod(0o700)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(migration.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="worker_runtime_asset_source_symlink_invalid"):
        migration._stage_runtime(manifest)


def test_stage_runtime_reuse_rejects_unsafe_existing_asset_mode(tmp_path, monkeypatch):
    runtime, manifest = _make_staged_runtime_fixture(tmp_path)
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    (runtime / "lab_queue.py").chmod(0o644)

    with pytest.raises(RuntimeError, match="worker_runtime_asset_permissions_invalid"):
        migration._stage_runtime(manifest)


def test_stage_runtime_reuse_rejects_modified_existing_asset_bytes(tmp_path, monkeypatch):
    runtime, manifest = _make_staged_runtime_fixture(tmp_path)
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    asset = runtime / "lab_queue.py"
    asset.write_text("changed content")
    asset.chmod(0o600)

    with pytest.raises(RuntimeError, match="worker_runtime_asset_content_mismatch"):
        migration._stage_runtime(manifest)


@pytest.mark.parametrize("target_kind", ["release", "staged_release"])
def test_stage_runtime_rejects_dangling_release_symlink(tmp_path, monkeypatch, target_kind):
    runtime_root = tmp_path / "runtime"
    stage_root = tmp_path / "staging"
    runtime_root.mkdir(mode=0o700)
    stage_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    stage_root.chmod(0o700)
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    monkeypatch.setattr(migration, "_runtime_staging_base", lambda: stage_root)
    target = runtime_root / ("a" * 40) if target_kind == "release" else stage_root / ("a" * 40)
    target.symlink_to(tmp_path / "missing-target")
    manifest = {"source_sha": "a" * 40, "assets": {name: "0" * 64 for name in migration.ASSETS},
                "bundle_sha256": "b" * 64}

    with pytest.raises(RuntimeError, match="worker_runtime_release_path_symlink_invalid"):
        migration._stage_runtime(manifest)

    assert target.is_symlink()


@pytest.mark.parametrize("target_kind", ["release", "staged_release"])
def test_install_staged_runtime_rejects_dangling_release_symlink(tmp_path, monkeypatch, target_kind):
    runtime_root = tmp_path / "runtime"
    stage_root = tmp_path / "staging"
    runtime_root.mkdir(mode=0o700)
    stage_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    stage_root.chmod(0o700)
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    release = runtime_root / ("a" * 40)
    staged_release = stage_root / ("a" * 40)
    target = release if target_kind == "release" else staged_release
    target.symlink_to(tmp_path / "missing-target")
    manifest = {"source_sha": "a" * 40, "assets": {name: "0" * 64 for name in migration.ASSETS},
                "bundle_sha256": "b" * 64}

    with pytest.raises(RuntimeError, match="worker_runtime_release_path_symlink_invalid"):
        migration._install_staged_runtime(staged_release, manifest)

    assert target.is_symlink()
