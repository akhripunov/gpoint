import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts/verify-kamal-production-baseline.py"
SPEC = importlib.util.spec_from_file_location("kamal_baseline", PATH)
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)


def rows(include_bot=True, include_eros=True):
    source = "a" * 40
    version = source + "-10-1"
    repositories = {**BASELINE.ROLES, **BASELINE.EROS_ROLES}
    records = []
    for i, (role, repository) in enumerate(repositories.items(), 1):
        if role == "gpoint-core-bot" and not include_bot:
            continue
        if role == "gpoint-eros-pilot-engine" and not include_eros:
            continue
        group = "EROS" if role == "gpoint-eros-pilot-engine" else "CORE" if role.startswith("gpoint-core-") else "MINIAPP"
        digest = "sha256:" + ("1" if group == "CORE" else "2" if group == "MINIAPP" else "3") * 64
        records.append({
            "role": role, "name": role + "-" + version, "version": version,
            "revision": source, "image_id": "sha256:" + ("4" if group == "CORE" else "5" if group == "MINIAPP" else "6") * 64,
            "repo_digests": [repository + "@" + digest],
        })
    return records


def test_accepts_exact_five_role_baseline_with_three_images():
    inventory = BASELINE.validate(rows())
    assert set(inventory["roles"]) == set(BASELINE.ROLES) | set(BASELINE.EROS_ROLES)
    assert inventory["missing_roles"] == []


def test_allows_missing_core_bot_and_uninstalled_eros_only_as_bootstrap_roles():
    inventory = BASELINE.validate(rows(include_bot=False, include_eros=False))
    assert set(inventory["missing_roles"]) == {"gpoint-core-bot", "gpoint-eros-pilot-engine"}


@pytest.mark.parametrize("removed", ["gpoint-core-api", "gpoint-core-worker", "gpoint-miniapp-web"])
def test_requires_live_core_and_miniapp_baselines(removed):
    records = [row for row in rows() if row["role"] != removed]
    with pytest.raises(ValueError, match="incomplete"):
        BASELINE.validate(records)


def test_rejects_duplicate_roles_and_shared_core_digest_mismatch():
    records = rows()
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        BASELINE.validate(records + [records[0]])
    records = rows()
    records[1]["repo_digests"] = ["ghcr.io/akhripunov/gpoint-runtime@sha256:" + "f" * 64]
    records[1]["image_id"] = records[0]["image_id"]
    with pytest.raises(ValueError, match="different image digests"):
        BASELINE.validate(records)


def test_rejects_any_retired_role_in_the_active_live_inventory():
    records = rows()
    records.append({
        "role": "gpoint-eros-relay-relay", "name": "gpoint-eros-relay-relay-" + "a" * 40 + "-10-1",
        "version": "a" * 40 + "-10-1", "revision": "a" * 40,
        "image_id": "sha256:" + "a" * 64,
        "repo_digests": ["ghcr.io/akhripunov/gpoint-runtime@sha256:" + "a" * 64],
    })
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        BASELINE.validate(records)
