from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def steps_for(document, job):
    return document["jobs"][job]["steps"]


def run_text(steps):
    return "\n".join(step.get("run", "") for step in steps)


def step_index(steps, fragment):
    return next(
        index
        for index, step in enumerate(steps)
        if fragment in step.get("name", "") or fragment in step.get("run", "")
    )


def test_required_ci_runs_full_contract_without_path_filters():
    document = workflow("gpoint-ci.yml")

    assert "pull_request" in document["on"]
    assert "paths" not in document["on"]["pull_request"]
    assert "paths-ignore" not in document["on"]["pull_request"]
    assert "workflow_call" in document["on"]
    steps = steps_for(document, "test")
    commands = run_text(steps)
    assert "pnpm install --frozen-lockfile" in commands
    assert "pnpm -r build" in commands
    assert "pnpm -r typecheck" in commands
    assert "pnpm -r test" in commands
    assert "pnpm test:integration" in commands
    assert "pnpm test:release" in commands
    assert "python -m pytest -q" in commands
    assert document["jobs"]["test"]["services"]["postgres"]["image"] == "pgvector/pgvector:0.8.6-pg16"
    assert "Test migrations, backups, and release contracts" in [
        step.get("name") for step in steps
    ]


def test_core_dispatch_is_gated_and_migrates_exact_new_image_before_kamal():
    document = workflow("gpoint-core-deploy.yml")
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["jobs"]["deploy"]["environment"] == "production"
    assert document["jobs"]["deploy"]["concurrency"]["group"] == "gpoint-production-release"
    assert "inputs:" not in str(document["on"]["workflow_dispatch"])
    deploy = steps_for(document, "deploy")
    shell = run_text(deploy)
    assert "ssh-keygen -F" in shell
    assert "StrictHostKeyChecking" in shell
    assert 'ghcr.io/akhripunov/gpoint-runtime@$APP_DIGEST' in shell
    assert 'APP_DIGEST" =~ ^sha256:[0-9a-f]{64}$' in shell
    assert "pnpm exec prisma migrate deploy" in shell
    assert step_index(deploy, "migrate deploy") < step_index(deploy, "Stop Bot then deploy")
    assert shell.index("kamal app stop --roles bot") < shell.index("kamal deploy --skip-push")
    assert "rollback" not in shell.lower()


def test_component_workflows_are_manual_and_use_existing_configs():
    miniapp = workflow("gpoint-miniapp-deploy.yml")
    eros = workflow("gpoint-eros-deploy.yml")
    relay = workflow("gpoint-eros-relay-deploy.yml")
    for document in (miniapp, eros, relay):
        assert set(document["on"]) == {"workflow_dispatch"}
        assert document["jobs"]["deploy"]["environment"] == "production"
        assert document["jobs"]["deploy"]["concurrency"]["group"] == "gpoint-production-release"

    miniapp_shell = run_text(steps_for(miniapp, "deploy"))
    assert 'image="ghcr.io/akhripunov/gpoint-miniapp@$IMAGE_DIGEST"' in miniapp_shell
    assert 'IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$' in miniapp_shell
    assert "config/deploy.miniapp.yml" in miniapp_shell
    assert "ghcr.io/akhripunov/gpoint-runtime@$IMAGE_DIGEST" not in miniapp_shell

    eros_shell = run_text(steps_for(eros, "deploy"))
    assert 'image="ghcr.io/akhripunov/gpoint-eros@$EROS_DIGEST"' in eros_shell
    assert "config/deploy.eros.yml" in eros_shell
    assert "scripts/eros-native-database.py" in eros_shell
    assert "--mode prepare" in eros_shell
    assert "--mode apply" in eros_shell
    assert "--mode verify-applied" in eros_shell
    assert "eros-production-model-config.toml" in eros_shell
    assert "sha256sum" in eros_shell

    relay_shell = run_text(steps_for(relay, "deploy"))
    assert 'image="ghcr.io/akhripunov/gpoint-runtime@$IMAGE_DIGEST"' in relay_shell
    assert "config/deploy.eros-relay.yml" in relay_shell
    assert "ghcr.io/akhripunov/gpoint-eros@$IMAGE_DIGEST" not in relay_shell
    assert step_index(steps_for(relay, "deploy"), "Verify the pinned Ollama") < step_index(
        steps_for(relay, "deploy"), "Deploy only Eros Relay"
    )


def test_existing_native_and_layer7_release_paths_remain_present():
    native = workflow("gpoint-native-release.yml")
    layer7 = workflow("gpoint-layer7-staging.yml")
    assert "push" in native["on"]
    assert "workflow_dispatch" in layer7["on"]
    assert layer7["jobs"]["layer7_staging"]["environment"]["name"] == "layer7-staging"
    assert layer7["jobs"]["layer7_staging"]["needs"] == "test"
    assert layer7["jobs"]["test"]["uses"] == "./.github/workflows/gpoint-ci.yml"
    assert layer7["jobs"]["test"]["with"]["candidate_sha"] == "${{ inputs.candidate_sha }}"
    assert any(
        step.get("uses") == "./.github/actions/layer7-deploy"
        for step in steps_for(layer7, "layer7_staging")
    )
