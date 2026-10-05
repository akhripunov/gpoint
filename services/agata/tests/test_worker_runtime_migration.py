"""Migration safety contracts; no LaunchAgent or installed files are changed."""
from __future__ import annotations

import importlib.util
import json
import plistlib
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
