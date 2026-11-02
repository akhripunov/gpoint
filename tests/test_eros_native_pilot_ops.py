import importlib.util
from pathlib import Path
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eros_native_ops", ROOT / "scripts/eros_native_pilot_ops.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


USER_ID = "10000000-0000-4000-8000-000000000001"
CREATOR_ID = "20000000-0000-4000-8000-000000000002"


def pilot_values():
    return {
        "EROS_ENABLED": "true",
        "EROS_PRODUCTION_TEST_MODE": "true",
        "EROS_DATA_MODE": "synthetic",
        "EROS_PRODUCTION_TEST_USER_IDS": USER_ID,
        "EROS_PRODUCTION_TEST_CREATOR_IDS": CREATOR_ID,
        "EROS_TEST_USER_IDS": USER_ID,
        "EROS_TEST_CREATOR_IDS": CREATOR_ID,
        "EROS_RUNTIME_DATABASE_URL": "postgresql://eros_runtime:secret@gpoint-eros-db:5432/eros",
        "EROS_MIGRATOR_DATABASE_URL": "postgresql://eros_migrator:secret@gpoint-eros-db:5432/eros",
        "DATABASE_URL": "postgresql://eros_runtime:secret@gpoint-eros-db:5432/eros",
        "EROS_IMAGE": "ghcr.io/akhripunov/gpoint-eros-pilot@sha256:" + "b" * 64,
        "EROS_SOURCE_SHA": "a" * 40,
        "EROS_DATABASE_IDENTITY": "secret-identity",
    }


def test_runtime_gate_requires_exact_single_uuid_pair_and_all_synthetic_flags():
    values = pilot_values()
    assert MODULE.validate_pilot_runtime(values, USER_ID, CREATOR_ID, "a" * 40)
    for key, value in (
        ("EROS_PRODUCTION_TEST_USER_IDS", ""),
        ("EROS_PRODUCTION_TEST_USER_IDS", USER_ID + "," + USER_ID),
        ("EROS_PRODUCTION_TEST_CREATOR_IDS", "not-a-uuid"),
        ("EROS_ENABLED", "false"),
        ("EROS_PRODUCTION_TEST_MODE", "false"),
        ("EROS_DATA_MODE", "production"),
    ):
        mismatch = dict(values, **{key: value})
        with pytest.raises(MODULE.PilotGateError):
            MODULE.validate_pilot_runtime(mismatch, USER_ID, CREATOR_ID, "a" * 40)
    with pytest.raises(MODULE.PilotGateError):
        MODULE.validate_pilot_runtime(values, CREATOR_ID, USER_ID, "a" * 40)


def test_migrator_env_contains_only_sqlx_dsn_and_is_private(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EROS_NATIVE_OPS_TEST", "1")
        path = Path(tmp) / "eros-migrator.env"
        MODULE.write_migrator_env(path, pilot_values())
        assert path.read_text() == "DATABASE_URL=postgresql://eros_migrator:secret@gpoint-eros-db:5432/eros\n"
        assert path.stat().st_mode & 0o777 == 0o600
        assert "EROS_RUNTIME_PASSWORD" not in path.read_text()


def test_stock_migration_command_is_exact_image_internal_network_and_one_shot():
    image = "ghcr.io/akhripunov/gpoint-eros-pilot@sha256:" + "b" * 64
    env_file = "/run/eros-migrator-" + "c" * 40 + ".env"
    command = MODULE.stock_migrate_command(image, "c" * 40, env_file)
    assert command == ["docker", "run", "--rm", "--network", "gpoint_eros_data",
                       "--env-file", env_file, image, "migrate"]
    with pytest.raises(MODULE.PilotGateError):
        MODULE.stock_migrate_command("ghcr.io/akhripunov/gpoint-eros-pilot:latest", "c" * 40, "/tmp/e")
    with pytest.raises(MODULE.PilotGateError):
        MODULE.stock_migrate_command(image, "c" * 40, "/tmp/eros.env")


def test_backup_manifest_binds_file_sha_and_refuses_paths_outside_pilot_backup_root():
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "eros-before-abc.dump"
        backup.write_bytes(b"verified PostgreSQL custom archive")
        backup.chmod(0o600)
        manifest = MODULE.write_backup_manifest(backup, "a" * 40, "b" * 64, backup_root=Path(tmp))
        assert manifest["sha256"] == MODULE.sha256_file(backup)
        assert manifest["sourceSha"] == "a" * 40
        assert manifest["databaseIdentitySha256"] == __import__("hashlib").sha256(b"b" * 64).hexdigest()
        with pytest.raises(MODULE.PilotGateError):
            MODULE.verify_backup(backup, Path(tmp) / "outside")


def test_restore_probe_database_name_is_strictly_scoped():
    assert MODULE.validate_restore_probe_name("eros_restore_probe_0123456789abcdef")
    for name in ("eros", "gpoint", "postgres", "eros_restore_probe_../../gpoint"):
        with pytest.raises(MODULE.PilotGateError):
            MODULE.validate_restore_probe_name(name)
