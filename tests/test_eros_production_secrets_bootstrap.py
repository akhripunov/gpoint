"""First-bootstrap contract for the production Eros protected environment."""

import os
import stat
import sys
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "eros_production_secrets", ROOT / "scripts/eros-production-secrets.py")
secrets_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secrets_mod)

SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/akhripunov/gpoint-eros@sha256:" + "b" * 64


def _source_env(tmp_path, tenant: str) -> Path:
    path = tmp_path / "production.env"
    path.write_text(f"AGATA_TENANT_ID={tenant}\nOTHER=value\n")
    return path


def test_missing_env_is_generated_with_all_secrets_and_0600(tmp_path):
    tenant = str(uuid.uuid4())
    target_dir = tmp_path / "env"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "eros.env"
    source = _source_env(tmp_path, tenant)

    secrets_mod.provision(target, source_sha=SOURCE_SHA, image=IMAGE, source_env=source)

    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    values = secrets_mod.read_env(target)
    for key in secrets_mod.SECRET_KEYS:
        assert values.get(key), f"{key} must be generated"
    assert values["EROS_TENANT_ID"] == tenant
    assert values["EROS_SOURCE_SHA"] == SOURCE_SHA
    assert values["EROS_IMAGE"] == IMAGE
    assert values["EROS_RUNTIME_DATABASE_URL"].startswith("postgresql://eros_runtime:")
    assert values["EROS_MIGRATOR_DATABASE_URL"].startswith("postgresql://eros_migrator:")
    # Values never render in the process output.
    assert "token_urlsafe" not in target.read_text()


def test_invalid_tenant_source_fails_closed(tmp_path):
    target_dir = tmp_path / "env"
    target_dir.mkdir(mode=0o700)
    source = _source_env(tmp_path, "not-a-uuid")
    try:
        secrets_mod.provision(target_dir / "eros.env", source_sha=SOURCE_SHA,
                              image=IMAGE, source_env=source)
    except (RuntimeError, ValueError):
        return
    raise AssertionError("invalid tenant must fail closed")


def test_missing_tenant_source_fails_closed(tmp_path):
    target_dir = tmp_path / "env"
    target_dir.mkdir(mode=0o700)
    try:
        secrets_mod.provision(target_dir / "eros.env", source_sha=SOURCE_SHA,
                              image=IMAGE, source_env=tmp_path / "absent.env")
    except (RuntimeError, ValueError):
        return
    raise AssertionError("missing tenant source must fail closed")


def test_eros_prerequisites_run_before_the_prisma_apply():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name", "") for step in steps]
    prereq = next(i for i, name in enumerate(names) if "Eros production prerequisites" in name)
    prisma = next(i for i, name in enumerate(names) if "Apply approved Prisma migrations" in name)
    assert prereq < prisma, "Eros prerequisites must be verified before the Prisma Apply"
    # The prerequisite step reuses the existing provisioner, not a new mechanism.
    assert "eros-production-secrets.py" in steps[prereq]["run"]
    assert steps[prereq]["if"] == "steps.component_plan.outputs.deploy_eros == 'true'"
