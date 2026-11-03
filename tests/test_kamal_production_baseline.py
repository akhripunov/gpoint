import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_kamal_production_baseline", ROOT / "scripts/verify-kamal-production-baseline.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def baseline_records(roles):
    version = "a" * 40 + "-123-1"
    source = "a" * 40
    repo_by_role = MODULE.ROLES
    image_ids = {}
    return [
        {
            "role": role,
            "name": f"{role}-{version}",
            "version": version,
            "revision": source,
            "image_id": image_ids.setdefault(repo_by_role[role], "sha256:" + str(index) * 64),
            "repo_digests": [repo_by_role[role] + "@sha256:" + image_ids[repo_by_role[role]].removeprefix("sha256:")],
        }
        for index, role in enumerate(roles, 1)
    ]


def test_baseline_accepts_four_service_gpoint_topology_after_agata_decommission():
    roles = ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot", "gpoint-miniapp-web")

    result = MODULE.validate(baseline_records(roles))

    assert set(result["roles"]) == set(roles)


def test_baseline_preserves_legacy_six_service_read_compatibility():
    assert len(MODULE.validate(baseline_records(MODULE.ROLES))["roles"]) == 6


@pytest.mark.parametrize("roles", [
    ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot", "gpoint-miniapp-web", "gpoint-agata-api"),
    ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
])
def test_baseline_rejects_partial_service_topologies(roles):
    with pytest.raises(ValueError, match="role set is incomplete"):
        MODULE.validate(baseline_records(roles))
