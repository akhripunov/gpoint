from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_layer7_is_not_configured_as_a_staging_release_target():
    retired_paths = (
        ".github/workflows/gpoint-layer7-staging.yml",
        ".github/actions/layer7-deploy/action.yml",
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
        "services/eros-engine/eros-layer7-staging-model-config.toml",
    )
    assert all(not (ROOT / path).exists() for path in retired_paths)


def test_pr_candidate_runs_build_without_production_deploy():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    )
    build = workflow["jobs"]["build"]
    deploy = workflow["jobs"]["deploy"]

    assert "github.event_name == 'pull_request'" in build["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in build["if"]
    assert "github.event.pull_request.author_association" in build["if"]
    assert 'fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\')' in build["if"]
    assert build["permissions"]["packages"] == "write"
    build_scope = next(step for step in build["steps"] if step.get("id") == "build_scope")
    assert build_scope["env"]["FULL_BUILD_VALIDATION"] == "${{ github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'me-62-full-build') }}"
    assert 'value["build_components"] = ["AGATA", "CORE", "EROS", "MINIAPP"]' in build_scope["run"]
    tests = workflow["jobs"]["tests"]
    eros_e2e = next(step for step in tests["steps"] if step.get("id") == "eros_e2e")
    assert "github.event_name == 'pull_request'" in eros_e2e["if"]
    assert "contains(github.event.pull_request.labels.*.name, 'me-62-full-build')" in eros_e2e["if"]
    assert "eros_e2e" not in tests["outputs"]
    eros_gate = next(step for step in build["steps"] if step.get("name") == "Require exact-candidate Eros test evidence")
    assert "/attempts/$GITHUB_RUN_ATTEMPT/jobs" in eros_gate["run"]
    assert ".head_sha" in eros_gate["run"]
    assert "runs/$GITHUB_RUN_ID" in eros_gate["run"]
    assert "github.event_name == 'pull_request'" not in deploy["if"]
    assert "needs.tests.outputs.mode == 'DEPLOY'" in deploy["if"]
