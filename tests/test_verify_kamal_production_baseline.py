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
            "started_at": ("2026-11-16T18:49:32Z" if role == "gpoint-core-api" else
                           "2026-11-13T20:00:19Z" if role == "gpoint-core-worker" else None),
        })
    return records


def test_accepts_exact_five_role_baseline_with_three_images():
    inventory = BASELINE.validate(rows())
    assert set(inventory["roles"]) == set(BASELINE.ROLES) | set(BASELINE.EROS_ROLES)
    assert inventory["missing_roles"] == []


def test_allows_missing_core_bot_and_uninstalled_eros_only_as_bootstrap_roles():
    inventory = BASELINE.validate(rows(include_bot=False, include_eros=False))
    assert set(inventory["missing_roles"]) == {"gpoint-core-bot", "gpoint-eros-pilot-engine"}


def test_reads_the_verified_layer7_mixed_core_baseline_without_losing_role_identity():
    records = rows(include_bot=False, include_eros=False)
    api = next(row for row in records if row["role"] == "gpoint-core-api")
    worker = next(row for row in records if row["role"] == "gpoint-core-worker")
    api.update({
        "name": "gpoint-core-api-3d680fa153fd6aebf786455d6f36d3b1a67cc78f-43930964514-1",
        "version": "3d680fa153fd6aebf786455d6f36d3b1a67cc78f-43930964514-1",
        "revision": "3d680fa153fd6aebf786455d6f36d3b1a67cc78f",
        "image_id": "sha256:8de41d23ec5f5e9592d8e7a4c377a72347d01266f97822486d4e40d0fbbb0a82",
        "repo_digests": ["ghcr.io/akhripunov/gpoint-runtime@sha256:8de41d23ec5f5e9592d8e7a4c377a72347d01266f97822486d4e40d0fbbb0a82"],
        "started_at": "2026-11-16T18:49:32Z",
    })
    worker.update({
        "name": "gpoint-core-worker-145111f17b8f2cb94bfa5d1c266fabc5eb0e0952-43494016382-1",
        "version": "145111f17b8f2cb94bfa5d1c266fabc5eb0e0952-43494016382-1",
        "revision": "145111f17b8f2cb94bfa5d1c266fabc5eb0e0952",
        "image_id": "sha256:107c041c8e9022c5998bbf4f749e6a62428bb30de358f67c7ab0032cbb974340",
        "repo_digests": ["ghcr.io/akhripunov/gpoint-runtime@sha256:107c041c8e9022c5998bbf4f749e6a62428bb30de358f67c7ab0032cbb974340"],
        "started_at": "2026-11-13T20:00:19Z",
    })

    inventory = BASELINE.validate(records)

    assert inventory["roles"]["gpoint-core-api"]["version"] == api["version"]
    assert inventory["roles"]["gpoint-core-worker"]["version"] == worker["version"]
    assert inventory["roles"]["gpoint-core-api"]["image_id"] != inventory["roles"]["gpoint-core-worker"]["image_id"]
    assert inventory["roles"]["gpoint-core-api"]["started_at"] == "2026-11-16T18:49:32Z"
    assert inventory["roles"]["gpoint-core-worker"]["started_at"] == "2026-11-13T20:00:19Z"
    assert inventory["mixed_components"] == ["CORE"]
    assert "gpoint-core-bot" in inventory["missing_roles"]


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
    with pytest.raises(ValueError, match="different image identities"):
        BASELINE.validate(records)


def test_rejects_role_version_that_does_not_match_its_source_sha():
    records = rows(include_bot=False)
    records[0]["revision"] = "f" * 40
    with pytest.raises(ValueError, match="invalid OCI source SHA"):
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
