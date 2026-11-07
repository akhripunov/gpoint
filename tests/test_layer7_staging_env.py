import importlib.util
import hashlib
import os
from pathlib import Path
import stat
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "layer7_staging_env", ROOT / "scripts/layer7_staging_env.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
sys.path.insert(0, str(ROOT / "scripts"))
WRITER_SPEC = importlib.util.spec_from_file_location(
    "write_layer7_staging_env", ROOT / "scripts/write_layer7_staging_env.py"
)
WRITER = importlib.util.module_from_spec(WRITER_SPEC)
WRITER_SPEC.loader.exec_module(WRITER)
DB_SPEC = importlib.util.spec_from_file_location(
    "layer7_staging_database", ROOT / "scripts/layer7_staging_database.py"
)
DB = importlib.util.module_from_spec(DB_SPEC)
DB_SPEC.loader.exec_module(DB)
STAGE_EROS_SPEC = importlib.util.spec_from_file_location(
    "write_layer7_staging_eros_api_env", ROOT / "scripts/write_layer7_staging_eros_api_env.py"
)
STAGE_EROS = importlib.util.module_from_spec(STAGE_EROS_SPEC)
STAGE_EROS_SPEC.loader.exec_module(STAGE_EROS)


def bundle(**overrides):
    values = {
        "DATABASE_URL": "postgresql://gpoint_staging_runtime:synthetic-db-secret@shared_postgres_1:5432/gpoint_staging",
        "REDIS_URL": "redis://default:synthetic-redis-secret@shared_redis_1:6379/15",
        "NODE_ENV": "production",
        "TELEGRAM_BOT_TOKEN": "synthetic-staging-bot-token",
        "TELEGRAM_WEBHOOK_SECRET": "synthetic-webhook-secret",
        "GPOINT_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        "GPOINT_CHAT_POLICY_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        "EROS_ENABLED": "true",
        "EROS_DATA_MODE": "gpoint-owned",
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items())


def bundle_without_bot_token():
    values = bundle().splitlines()
    return "\n".join(line for line in values if not line.startswith("TELEGRAM_BOT_TOKEN="))


def test_staging_env_accepts_internal_isolated_services_and_returns_only_hash():
    encoded, digest, bot_enabled = MODULE.parse_staging_env(bundle())
    assert b"synthetic-staging-bot-token" in encoded
    assert bot_enabled is True
    assert b"GPOINT_INGEST_URL=https://layer7-staging.invalid/api/internal/telegram/update\n" in encoded
    assert b"PUBLIC_APP_URL=https://layer7-staging.invalid\n" in encoded
    assert digest == __import__("hashlib").sha256(encoded).hexdigest()


def test_staging_env_accepts_telegramless_core_and_eros_mode_without_bot_secret():
    encoded, digest, bot_enabled = MODULE.parse_staging_env(bundle_without_bot_token())
    assert bot_enabled is False
    assert b"TELEGRAM_BOT_TOKEN=" not in encoded
    assert b"TELEGRAM_WEBHOOK_SECRET=synthetic-webhook-secret\n" in encoded
    assert digest == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("override", [
    {"DATABASE_URL": "postgresql://gpoint_staging_runtime:secret@38.180.110.92:5432/gpoint_staging"},
    {"DATABASE_URL": "postgresql://gpoint_staging_runtime:secret@external-db.example:5432/gpoint_staging"},
    {"DATABASE_URL": "postgresql://gpoint_staging_runtime:secret@shared_postgres_1:5432/maxibot"},
    {"DATABASE_URL": "postgresql://gpoint_staging_runtime:secret@shared_postgres_1:5432/gpoint"},
    {"REDIS_URL": "redis://default:secret@redis.example:6379/0"},
    {"GPOINT_TENANT_ID": "11111111-1111-4111-8111-111111111111"},
    {"GPOINT_CHAT_POLICY_TENANT_ID": "not-a-uuid"},
    {"EROS_ENABLED": "false"},
    {"EROS_DATA_MODE": "synthetic"},
    {"TELEGRAM_WEBHOOK_SECRET": ""},
])
def test_staging_env_rejects_production_remote_and_incomplete_routes(override):
    with pytest.raises(ValueError):
        MODULE.parse_staging_env(bundle(**override))


def test_staging_env_rejects_duplicate_keys_and_shell_interpolation():
    with pytest.raises(ValueError):
        MODULE.parse_staging_env(bundle() + "\nDATABASE_URL=postgresql://other")
    with pytest.raises(ValueError):
        MODULE.parse_staging_env(bundle(DATABASE_URL="${PRODUCTION_DATABASE_URL}"))


def test_staging_env_size_is_bounded():
    with pytest.raises(ValueError):
        MODULE.parse_staging_env(bundle() + "\n#" + "x" * 49_000)


def test_staging_env_writer_installs_only_validated_payload_with_private_mode(tmp_path):
    root = tmp_path / "env"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    encoded, digest, _ = MODULE.parse_staging_env(bundle())
    assert WRITER.install_staging_env(
        encoded, root=root, run_id="42339937322-1", expected_sha256=digest,
        owner_uid=os.getuid(),
    ) == digest
    target = root / "staging.env"
    assert target.read_bytes() == encoded
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(root.glob("*.pending"))


def test_staging_env_writer_fails_closed_without_overwriting_on_digest_mismatch(tmp_path):
    root = tmp_path / "env"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    target = root / "staging.env"
    target.write_text("old staging marker\n")
    os.chmod(target, 0o600)
    payload, _, _ = MODULE.parse_staging_env(bundle())
    with pytest.raises(ValueError, match="digest mismatch"):
        WRITER.install_staging_env(payload, root=root, run_id="42339937322-2",
                                   expected_sha256="0" * 64, owner_uid=os.getuid())
    assert target.read_text() == "old staging marker\n"


def test_staging_database_bootstrap_is_restricted_to_empty_staging_identity():
    calls = []

    class Result:
        stdout = b'{"database":null,"publicConnect":false,"otherConnect":[],"role":null}'

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    url = "postgresql://gpoint_staging_runtime:synthetic%40secret@shared_postgres_1:5432/gpoint_staging"
    DB.bootstrap_database(url, runner=runner)
    assert len(calls) == 2
    assert calls[0][0][0:4] == ["docker", "exec", "-i", "shared_postgres_1"]
    assert all("maxibot" not in " ".join(call[0]) for call in calls)
    assert b"CREATE DATABASE \"gpoint_staging\" OWNER \"gpoint_staging_runtime\"" in calls[1][1]["input"]
    assert b"synthetic@secret" in calls[1][1]["input"]
    assert "synthetic@secret" not in " ".join(calls[1][0])


def test_staging_database_bootstrap_safely_reuses_only_a_least_privilege_staging_identity():
    class Result:
        stdout = b'{"database":"gpoint_staging_runtime","publicConnect":false,"otherConnect":[],"role":{"superuser":false,"createdb":false,"createrole":false,"bypassrls":false,"inherit":false,"replication":false,"memberships":0}}'
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    DB.bootstrap_database(
        "postgresql://gpoint_staging_runtime:synthetic@shared_postgres_1:5432/gpoint_staging",
        runner=runner,
    )
    assert len(calls) == 2
    assert b"ALTER ROLE \"gpoint_staging_runtime\"" in calls[1][1]["input"]
    assert b"CREATE DATABASE \"gpoint_staging\"" not in calls[1][1]["input"]


def test_staging_database_bootstrap_refuses_privileged_role_or_wrong_database_owner():
    class Result:
        stdout = b'{"database":"other_owner","publicConnect":false,"otherConnect":[],"role":{"superuser":true,"createdb":false,"createrole":false,"bypassrls":false,"inherit":false,"replication":false,"memberships":0}}'

    with pytest.raises(RuntimeError, match="unsafe existing staging identity"):
        DB.bootstrap_database(
            "postgresql://gpoint_staging_runtime:synthetic@shared_postgres_1:5432/gpoint_staging",
            runner=lambda *_args, **_kwargs: Result(),
        )


def test_staging_eros_api_env_reuses_canonical_writer_and_binds_synthetic_tenant(tmp_path):
    env_dir = tmp_path / "env"
    env_dir.mkdir(mode=0o700)
    os.chmod(env_dir, 0o700)
    tenant = "22222222-2222-4222-8222-222222222222"
    eros_env = env_dir / "eros.env"
    eros_env.write_text("\n".join([
        "EROS_JWT_SECRET=" + "j" * 48,
        "EROS_BRIDGE_API_KEY=" + "b" * 48,
        "EROS_EMBEDDING_API_KEY=" + "e" * 48,
        "EROS_WORKER_SIGNING_SECRET=" + "s" * 48,
        "EROS_WORKER_GATEWAY_TOKEN=" + "w" * 48,
        "EROS_TENANT_ID=" + tenant,
    ]) + "\n")
    os.chmod(eros_env, 0o600)
    source_env = env_dir / "staging.env"
    source_env.write_text("GPOINT_CHAT_POLICY_TENANT_ID=" + tenant + "\n")
    os.chmod(source_env, 0o600)
    output = env_dir / "layer7-staging-eros-api.env"

    STAGE_EROS.write(eros_env=eros_env, source_env=source_env, output=output)
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["EROS_DATA_MODE"] == "gpoint-owned"
    assert values["EROS_SERVICE_URL"] == "http://gpoint-eros:8080"
    assert values["EROS_WORKER_TENANT_ID"] == tenant
    assert values["GPOINT_CHAT_POLICY_TENANT_ID"] == tenant
    assert values["EROS_PRODUCTION_TEST_USER_IDS"] == ""
    assert values["EROS_PRODUCTION_TEST_CREATOR_IDS"] == ""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_staging_eros_api_env_refuses_tenant_mismatch(tmp_path):
    env_dir = tmp_path / "env"
    env_dir.mkdir(mode=0o700)
    os.chmod(env_dir, 0o700)
    eros_env = env_dir / "eros.env"
    eros_env.write_text("\n".join([
        "EROS_JWT_SECRET=" + "j" * 48,
        "EROS_BRIDGE_API_KEY=" + "b" * 48,
        "EROS_EMBEDDING_API_KEY=" + "e" * 48,
        "EROS_WORKER_SIGNING_SECRET=" + "s" * 48,
        "EROS_WORKER_GATEWAY_TOKEN=" + "w" * 48,
        "EROS_TENANT_ID=11111111-1111-4111-8111-111111111111",
    ]) + "\n")
    os.chmod(eros_env, 0o600)
    source_env = env_dir / "staging.env"
    source_env.write_text("GPOINT_CHAT_POLICY_TENANT_ID=22222222-2222-4222-8222-222222222222\n")
    with pytest.raises(RuntimeError, match="tenant identities disagree"):
        STAGE_EROS.write(eros_env=eros_env, source_env=source_env,
                         output=env_dir / "layer7-staging-eros-api.env")
