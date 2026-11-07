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
    group_values = {
        "ghcr.io/akhripunov/gpoint-runtime": ("sha256:" + "1" * 64, "ghcr.io/akhripunov/gpoint-runtime@sha256:" + "a" * 64),
        "ghcr.io/akhripunov/gpoint-agata-runtime": ("sha256:" + "2" * 64, "ghcr.io/akhripunov/gpoint-agata-runtime@sha256:" + "b" * 64),
        "ghcr.io/akhripunov/gpoint-miniapp": ("sha256:" + "3" * 64, "ghcr.io/akhripunov/gpoint-miniapp@sha256:" + "c" * 64),
        "ghcr.io/akhripunov/gpoint-eros": ("sha256:" + "4" * 64, "ghcr.io/akhripunov/gpoint-eros@sha256:" + "d" * 64),
    }
    for role, repo in BASELINE.ROLES.items():
        version = f"{SHA}-41223019715-1"
        rows.append({"role": role, "name": f"{role}-{version}", "version": version,
                     "revision": SOURCE, "image_id": group_values[repo][0],
                     "repo_digests": [group_values[repo][1]]})
    return rows


def eros_records():
    rows = records()
    version = f"{SHA}-41223019715-1"
    engine_role, relay_role = BASELINE.EROS_ROLES
    engine_repo = BASELINE.EROS_ROLES[engine_role]
    runtime_repo = BASELINE.EROS_ROLES[relay_role]
    rows.extend([
        {"role": engine_role, "name": f"{engine_role}-{version}", "version": version,
         "revision": SOURCE, "image_id": "sha256:" + "4" * 64,
         "repo_digests": [engine_repo + "@sha256:" + "d" * 64]},
        {"role": relay_role, "name": f"{relay_role}-{version}", "version": version,
         "revision": SOURCE, "image_id": "sha256:" + "1" * 64,
         "repo_digests": [runtime_repo + "@sha256:" + "a" * 64]},
    ])
    return rows


def test_accepts_historical_release_version_sha_separate_from_common_app_source_sha():
    result = BASELINE.validate(records())
    assert result["version"] == f"{SHA}-41223019715-1"
    assert result["source_sha"] == SOURCE
    assert len(result["roles"]) == 6


def test_accepts_complete_eros_engine_and_relay_baseline():
    result = BASELINE.validate(eros_records())
    assert len(result["roles"]) == 8
    engine_role, relay_role = BASELINE.EROS_ROLES
    assert result["roles"][engine_role]["repo_digest"].startswith(
        "ghcr.io/akhripunov/gpoint-eros@sha256:")
    assert result["roles"][relay_role]["repo_digest"].startswith(
        "ghcr.io/akhripunov/gpoint-runtime@sha256:")


@pytest.mark.parametrize("missing_role", BASELINE.EROS_ROLES)
def test_rejects_partial_eros_baseline(missing_role):
    rows = [row for row in eros_records() if row["role"] != missing_role]
    with pytest.raises(ValueError, match="incomplete or duplicated"):
        BASELINE.validate(rows)


def test_accepts_component_scoped_baselines_but_keeps_each_shared_image_group_consistent():
    rows = records()
    groups = {
        "gpoint-agata-api": ("c" * 40, "c" * 40 + "-41223019900-1"),
        "gpoint-agata-memory-worker": ("c" * 40, "c" * 40 + "-41223019900-1"),
        "gpoint-miniapp-web": ("d" * 40, "d" * 40 + "-41223020000-1"),
    }
    for row in rows:
        if row["role"] in groups:
            source, version = groups[row["role"]]
            row["revision"] = source
            row["version"] = version
            row["name"] = f"{row['role']}-{version}"

    result = BASELINE.validate(rows)

    assert result["version"] is None
    assert result["source_sha"] == SOURCE
    assert result["roles"]["gpoint-agata-api"]["source_sha"] == "c" * 40
    assert result["roles"]["gpoint-miniapp-web"]["source_sha"] == "d" * 40


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r.pop(), "incomplete or duplicated"),
    (lambda r: r[0].update(repo_digests=[]), "immutable GHCR RepoDigest"),
    (lambda r: r[0].update(repo_digests=["ghcr.io/akhripunov/evil@sha256:" + "f" * 64]), "immutable GHCR RepoDigest"),
    (lambda r: r[0].update(revision="not-a-sha"), "invalid OCI source SHA"),
    (lambda r: r[0].update(image_id="sha256:bad"), "invalid OCI source SHA"),
    (lambda r: r[1].update(version="c" * 40 + "-1-1", name="gpoint-core-worker-" + "c" * 40 + "-1-1"), "mixed release versions"),
    (lambda r: r[1].update(revision="c" * 40), "mixed source revisions"),
    (lambda r: r[1].update(repo_digests=["ghcr.io/akhripunov/gpoint-runtime@sha256:" + "d" * 64]), "roles for ghcr.io/akhripunov/gpoint-runtime have different image digests"),
    (lambda r: r[1].update(image_id="sha256:" + "d" * 64), "roles for ghcr.io/akhripunov/gpoint-runtime have different Docker image IDs"),
    (lambda r: r[0].update(version=None), "Kamal version is missing or malformed"),
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
    assert 'json.loads(sys.argv[1])' in preflight
    assert 'bash -s -- "$baseline_version" "$baseline_source_sha"' in preflight
    assert 'bash -s -- "$role_inventory"' not in preflight
    assert 'printf \'GPOINT_BASELINE_ROLE_INVENTORY=%s\\n\' "$role_inventory" >> "$GITHUB_OUTPUT"' in preflight
    assert preflight.index("baseline_values=") < preflight.index('bash -s -- "$baseline_version"')
    assert '"$version" == "$source_sha"-*' not in preflight
    compatibility = deploy.split("name: Verify candidate compatibility with the live database baseline", 1)[1].split(
        "name: Create exact-SHA in-progress GitHub Deployment", 1)[0]
    assert "BASELINE_SOURCE_SHA: ${{ steps.topology.outputs.GPOINT_BASELINE_ROLE_SOURCE_SHA }}" in compatibility
