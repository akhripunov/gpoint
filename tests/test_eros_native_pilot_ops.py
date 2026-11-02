import importlib.util
import json
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


def test_local_image_alias_is_confined_to_exact_disposable_docker_e2e(monkeypatch):
    image = "gpoint-eros-pilot:" + "a" * 40
    assert not MODULE._valid_stock_image(image, "a" * 40)
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST", "1")
    assert not MODULE._valid_stock_image(image, "a" * 40)
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER", "eros-provision-test-0123456789ab")
    assert MODULE._valid_stock_image(image, "a" * 40)
    assert not MODULE._valid_stock_image(image, "b" * 40)
    assert not MODULE._valid_stock_image("gpoint-eros-pilot:latest", "a" * 40)


def test_local_stock_image_readback_returns_docker_image_id(monkeypatch):
    sha = "a" * 40
    image = f"gpoint-eros-pilot:{sha}"
    image_id = "sha256:" + "c" * 64
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST", "1")
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST_POSTGRES_CONTAINER", "eros-provision-test-0123456789ab")
    row = {"Id": image_id, "Os": "linux", "Architecture": "amd64",
           "Config": {"Labels": {
               "org.opencontainers.image.revision": MODULE.UPSTREAM_SHA,
               "com.gpoint.eros.upstream-revision": MODULE.UPSTREAM_SHA,
               "com.gpoint.integration.revision": sha,
               "com.gpoint.eros.patch-set.sha256": "none",
           }}, "RepoDigests": []}
    monkeypatch.setattr(MODULE, "_run", lambda *a, **k: type("Result", (), {
        "stdout": json.dumps([row]).encode("utf-8")})())
    assert MODULE.verify_stock_image(image, sha) == {
        "imageId": image_id, "digest": image_id, "platform": "linux/amd64",
    }


def test_native_ops_run_captures_stdout_for_readbacks(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": b"verified"})()

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    result = MODULE._run(["docker", "image", "inspect", "gpoint-eros-pilot:test"])
    assert result.stdout == b"verified"
    assert calls[0][1]["stdout"] == MODULE.subprocess.PIPE


def test_native_ops_run_classifies_permission_failures_without_exposing_output(monkeypatch):
    def run(command, **kwargs):
        return type("Result", (), {
            "returncode": 1,
            "stdout": b"",
            "stderr": b"ERROR: permission denied for schema engine; postgresql://secret:password@example/eros",
        })()

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    with pytest.raises(MODULE.PilotGateError) as caught:
        MODULE._run(["docker", "run", "stock-eros", "migrate"])
    assert caught.value.diagnostic_code == "postgres_schema_permission"
    assert "secret" not in str(caught.value)
    assert "password" not in str(caught.value)


@pytest.mark.parametrize(("stdout", "stderr", "expected"), [
    (b"sqlx error: SQLSTATE 42501", b"", "postgres_sqlstate"),
    (b"sqlx error: SQLSTATE secre!", b"", "postgres_sqlstate"),
    (b"vendor password mismatch", b"", "stock_eros_migration_failure"),
])
def test_native_ops_run_classifies_stdout_only_migration_errors(monkeypatch, stdout, stderr, expected):
    def run(command, **kwargs):
        return type("Result", (), {"returncode": 1, "stdout": stdout, "stderr": stderr})()

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    with pytest.raises(MODULE.PilotGateError) as caught:
        MODULE._run(["docker", "run", "stock-eros", "migrate"])
    assert caught.value.diagnostic_code == expected
    assert "vendor password" not in str(caught.value)


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


def test_pilot_cleanup_requires_exact_eros_database_owner_and_deployment_identity():
    assert MODULE.validate_cleanup_identity(
        database="eros", owner="eros_ddl", observed_identity="pilot-identity",
        expected_identity="pilot-identity")
    for database, owner, observed in (
        ("gpoint", "gpoint", "pilot-identity"),
        ("eros", "postgres", "pilot-identity"),
        ("eros", "eros_ddl", "other-pilot"),
    ):
        with pytest.raises(MODULE.PilotGateError):
            MODULE.validate_cleanup_identity(database=database, owner=owner,
                observed_identity=observed, expected_identity="pilot-identity")


def test_pilot_cleanup_never_targets_existing_gpoint_services_or_shared_networks():
    assert MODULE.validate_cleanup_service("gpoint-eros-pilot")
    assert MODULE.validate_cleanup_service("gpoint-eros-relay")
    for service in ("gpoint-core", "gpoint-agata", "gpoint-miniapp", "shared_postgres_1"):
        with pytest.raises(MODULE.PilotGateError):
            MODULE.validate_cleanup_service(service)


def test_cleanup_drops_only_identity_owned_database_and_exact_roles(monkeypatch):
    calls = []
    replies = iter(("eros_ddl", "pilot-identity", json.dumps({
        "outsideOwners": 0, "memberships": 0, "connections": 0,
    })))

    def psql(sql, database="postgres"):
        calls.append((database, sql))
        try:
            return next(replies)
        except StopIteration:
            return ""

    monkeypatch.setattr(MODULE, "_psql", psql)
    MODULE.cleanup_database("pilot-identity")
    assert [sql for _, sql in calls if sql.startswith("DROP")] == [
        "DROP DATABASE eros", "DROP ROLE eros_runtime; DROP ROLE eros_migrator; DROP ROLE eros_ddl;"
    ]


def test_cleanup_refuses_foreign_eros_database_identity_before_drop(monkeypatch):
    calls = []

    def psql(sql, database="postgres"):
        calls.append(sql)
        return "eros_ddl" if len(calls) == 1 else "not-this-pilot"

    monkeypatch.setattr(MODULE, "_psql", psql)
    with pytest.raises(MODULE.PilotGateError):
        MODULE.cleanup_database("pilot-identity")
    assert not any(sql.startswith("DROP") for sql in calls)


def test_cleanup_refuses_network_with_foreign_container(monkeypatch):
    row = {"Name": "gpoint_eros_data", "Driver": "bridge", "Internal": True,
           "Labels": {"com.gpoint.eros.managed": "true"}, "IPAM": {"Config": []},
           "Containers": {"abc": {"Name": "gpoint-core-api-foreign"}}}
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *a, **k: MODULE.subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps([row]), stderr=""))
    with pytest.raises(MODULE.PilotGateError, match="attached containers"):
        MODULE.cleanup_managed_networks()


def test_pilot_api_network_detach_requires_exact_release_source_and_digest():
    with pytest.raises(MODULE.PilotGateError):
        MODULE.detach_pilot_api_network("gpoint-core-api-latest", "a" * 40,
            "ghcr.io/akhripunov/gpoint-runtime@sha256:" + "b" * 64)
    with pytest.raises(MODULE.PilotGateError):
        MODULE.detach_pilot_api_network("a" * 40 + "-1-1", "bad",
            "ghcr.io/akhripunov/gpoint-runtime@sha256:" + "b" * 64)


def test_stock_persona_seed_uses_exact_pinned_synthetic_fixture_and_private_database_network(monkeypatch):
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST", "1")
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST_SOURCE_ROOT", str(ROOT))
    root = str(ROOT)
    command = MODULE.stock_persona_seed_command(
        "ghcr.io/akhripunov/gpoint-eros-pilot@sha256:" + "b" * 64,
        "a" * 40, "/run/eros-migrator-" + "a" * 40 + ".env")
    assert command == [
        "docker", "run", "--rm", "--network", "gpoint_eros_data",
        "--env-file", "/run/eros-migrator-" + "a" * 40 + ".env",
        "--volume", root + "/services/eros-engine/test-personas:/etc/eros-engine/test-personas:ro",
        "ghcr.io/akhripunov/gpoint-eros-pilot@sha256:" + "b" * 64,
        "seed-personas", "/etc/eros-engine/test-personas",
    ]
    with pytest.raises(MODULE.PilotGateError):
        MODULE.stock_persona_seed_command("ghcr.io/akhripunov/gpoint-eros-pilot:latest",
            "a" * 40, "/run/eros-migrator-" + "a" * 40 + ".env")


def test_stock_persona_seed_requires_one_genome_identity_and_keeps_result_private(tmp_path, monkeypatch):
    monkeypatch.setenv("EROS_NATIVE_OPS_TEST", "1")
    genome = "30000000-0000-4000-8000-000000000003"
    assert MODULE.parse_seeded_genome_id(f"Synthetic Eros Persona: {genome}\n") == genome
    for output in ("no persona output", f"Synthetic Eros Persona {genome}\nSynthetic Eros Persona {genome}"):
        with pytest.raises(MODULE.PilotGateError):
            MODULE.parse_seeded_genome_id(output)
    result = tmp_path / "result.json"
    MODULE.write_persona_seed_result(result, "a" * 40, genome)
    assert result.stat().st_mode & 0o777 == 0o600
    assert MODULE.json.loads(result.read_text())["genomeId"] == genome
