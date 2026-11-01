import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("baseline", ROOT / "scripts/verify-kamal-production-baseline.py")
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)
SHA, SOURCE = "a" * 40, "b" * 40


def records():
    rows = []
    for i, (role, repo) in enumerate(BASELINE.ROLES.items(), 1):
        version = f"{SHA}-41223019715-1"
        rows.append({"role": role, "name": f"{role}-{version}", "version": version,
                     "revision": SOURCE, "image_id": f"sha256:{i:064x}",
                     "repo_digests": [f"{repo}@sha256:{i:064x}"]})
    return rows


def test_accepts_historical_release_version_sha_separate_from_common_app_source_sha():
    result = BASELINE.validate(records())
    assert result["version"] == f"{SHA}-41223019715-1"
    assert result["source_sha"] == SOURCE
    assert len(result["roles"]) == 6


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.pop(), "incomplete or duplicated"),
    (lambda r: r[0].update(repo_digests=[]), "immutable GHCR RepoDigest"),
    (lambda r: r[0].update(repo_digests=["ghcr.io/akhripunov/evil@sha256:" + "f" * 64]), "immutable GHCR RepoDigest"),
    (lambda r: r[0].update(revision="not-a-sha"), "invalid OCI source SHA"),
    (lambda r: r[0].update(image_id="sha256:bad"), "invalid OCI source SHA"),
    (lambda r: r[1].update(version="c" * 40 + "-1-1", name="gpoint-core-worker-" + "c" * 40 + "-1-1"), "mixed release versions"),
    (lambda r: r[1].update(revision="c" * 40), "mixed source revisions"),
])
def test_rejects_tampered_or_inconsistent_baseline(mutate, message):
    rows = records()
    mutate(rows)
    with pytest.raises(ValueError, match=message):
        BASELINE.validate(rows)


def test_workflow_uses_read_only_remote_baseline_and_its_source_sha_for_migration_gate():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    preflight = deploy.split("name: Verify installed Kamal topology before ordinary release", 1)[1].split(
        "name: Verify candidate compatibility with the live database baseline", 1)[0]
    assert "verify-kamal-production-baseline.py" in preflight
    assert "GPOINT_BASELINE_ROLE_INVENTORY" in preflight
    assert '"$version" == "$source_sha"-*' not in preflight
    compatibility = deploy.split("name: Verify candidate compatibility with the live database baseline", 1)[1].split(
        "name: Create exact-SHA in-progress GitHub Deployment", 1)[0]
    assert "BASELINE_SOURCE_SHA: ${{ steps.topology.outputs.GPOINT_BASELINE_ROLE_SOURCE_SHA }}" in compatibility
