import yaml
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_layer7_staging_is_owner_dispatched_on_trusted_runner_with_read_only_packages():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["layer7_staging"]

    assert job["runs-on"] == ["self-hosted", "gpoint-owner-mac"]
    assert job["environment"]["name"] == "layer7-staging"
    assert job["permissions"] == {
        "contents": "read",
        "packages": "read",
        "actions": "read",
        "deployments": "write",
    }
    assert "GPOINT_KAMAL_HOST" not in str(job)
    assert "production" not in str(job["environment"]).lower()


def test_layer7_staging_requires_exact_successful_prepare_and_explicit_staging_intent():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["deploy_layer7_staging"]["type"] == "boolean"
    assert inputs["deploy_layer7_staging"]["default"] == "false"
    assert inputs["approve_layer7_staging"]["default"] == "false"

    job = workflow["jobs"]["layer7_staging"]
    assert job["needs"] == ["tests"]
    assert "inputs.deploy_layer7_staging == true" in job["if"]
    step_text = yaml.safe_dump(job["steps"])
    assert "verify_native_prepared_run.py" in step_text
    assert "prepared_run_id" in step_text
    assert "prepared_eros_digest" in step_text
    assert "StrictHostKeyChecking=yes" in step_text
    assert "staging_preflight.outcome == 'success'" in step_text
    assert "Remove temporary Kamal GHCR authorization" in step_text
    assert "docker logout ghcr.io" in step_text
    assert "no_existing_credentials=true" in step_text
    assert "CORE_API_EROS_ROUTE=PASS EROS_DATABASE_NETWORK_ISOLATION=PASS" in (
        ROOT / ".github/workflows/gpoint-native-release.yml"
    ).read_text()


def test_staging_kamal_configs_are_separate_from_public_production_routes():
    config_paths = [
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
    ]
    for relative in config_paths:
        path = ROOT / relative
        assert path.is_file(), relative
        text = path.read_text()
        assert "GPOINT_KAMAL_HOST" not in text
        assert "GPOINT_PRODUCTION_HOST" not in text
        assert "gpoint.one" not in text
        assert "GPOINT_LAYER7_STAGING_HOST" in text
    api_config = (ROOT / "config/deploy.layer7-staging.yml").read_text()
    eros_config = (ROOT / "config/deploy.layer7-staging.eros.yml").read_text()
    assert "- gpoint_eros_api" in api_config
    assert "- gpoint_eros_data" not in api_config
    assert "- gpoint_eros_data" in eros_config and "- gpoint_eros_api" in eros_config


def test_layer7_staging_never_uses_production_environment_or_production_host():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    staging = workflow["jobs"]["layer7_staging"]
    serialized = yaml.safe_dump(staging)
    assert "secrets.GPOINT_KAMAL_HOST" not in serialized
    assert "environment: production" not in serialized
    assert "GPOINT_LAYER7_STAGING_HOST" in serialized
    assert "GITHUB_TOKEN" in serialized


def test_layer7_staging_is_separate_from_build_and_production_deploy_jobs():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    staging = workflow["jobs"]["layer7_staging"]
    assert "LAYER7_STAGING" not in workflow["jobs"]["build"]["if"]
    assert "LAYER7_STAGING" not in workflow["jobs"]["deploy"]["if"]
    assert "needs.tests.outputs.deploy == 'true'" in workflow["jobs"]["deploy"]["if"]
    assert staging["env"]["KAMAL_REGISTRY_PASSWORD"] == "${{ secrets.GITHUB_TOKEN }}"
    assert staging["permissions"]["packages"] == "read"
    assert staging["permissions"].get("contents") == "read"
    assert staging["permissions"].get("deployments") == "write"
    assert "packages: write" not in yaml.safe_dump(staging)
    steps = yaml.safe_dump(staging["steps"])
    assert "steps.prepared.outputs.image_version" in steps
    for config in (
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
    ):
        assert config in steps
    assert "--mode prepare" in steps
    assert "--mode apply" in steps
    assert "docker inspect --format" in steps
    assert "com.gpoint.source.revision" in steps
    assert "StrictHostKeyChecking=yes" in steps
