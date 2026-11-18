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
    assert "actionlint" in commands
    assert "scripts/check-kamal-configs.sh" in commands
    assert "ghcr.io/basecamp/kamal:v2.12.0@sha256:" in str(document["jobs"]["test"]["steps"])
    assert document["jobs"]["test"]["services"]["postgres"]["image"] == "pgvector/pgvector:0.8.6-pg16"
    assert "Test migrations, backups, and release contracts" in [
        step.get("name") for step in steps
    ]


def test_core_dispatch_is_gated_and_migrates_exact_new_image_before_kamal():
    document = workflow("gpoint-core-deploy.yml")
    assert set(document["on"]) == {"push", "workflow_dispatch"}
    assert document["on"]["push"]["branches"] == ["dev"]
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
    assert shell.index("kamal app stop --roles bot") < shell.index(
        "kamal deploy --skip-push"
    )
    assert "rollback" not in shell.lower()


def test_component_workflows_are_manual_and_use_existing_configs():
    miniapp = workflow("gpoint-miniapp-deploy.yml")
    eros = workflow("gpoint-eros-deploy.yml")
    relay = workflow("gpoint-eros-relay-deploy.yml")
    for document in (miniapp, eros, relay):
        assert set(document["on"]) == {"push", "workflow_dispatch"}
        assert document["on"]["push"]["branches"] == ["dev"]
        assert document["jobs"]["build"]["if"] == "github.ref == 'refs/heads/main' && github.actor == github.repository_owner"
        assert document["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main' && github.actor == github.repository_owner"
        assert document["jobs"]["deploy"]["environment"] == "production"
        assert document["jobs"]["deploy"]["concurrency"]["group"] == "gpoint-production-release"
        assert document["jobs"]["deploy"]["steps"][-1]["if"] == "always() && env.GPOINT_PRODUCTION_HOST != ''"

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


def test_component_dev_paths_are_scoped_and_core_relay_runtime_builds_match():
    core = workflow("gpoint-core-deploy.yml")
    miniapp = workflow("gpoint-miniapp-deploy.yml")
    eros = workflow("gpoint-eros-deploy.yml")
    relay = workflow("gpoint-eros-relay-deploy.yml")
    core_paths = set(core["on"]["push"]["paths"])
    miniapp_paths = set(miniapp["on"]["push"]["paths"])
    eros_paths = set(eros["on"]["push"]["paths"])
    relay_paths = set(relay["on"]["push"]["paths"])

    assert "apps/api/**" in core_paths and "apps/miniapp/**" not in core_paths
    assert "apps/miniapp/**" in miniapp_paths and "apps/api/**" not in miniapp_paths
    assert "services/eros-engine/upstream.sha" in eros_paths
    assert "services/eros-engine/eros-pilot-embedding-relay.mjs" in relay_paths
    for paths in (core_paths, miniapp_paths, eros_paths, relay_paths):
        assert "docs/**" not in paths

    core_build = next(step for step in steps_for(core, "build") if step.get("name") == "Build immutable Core image")["with"]
    relay_build = next(step for step in steps_for(relay, "build") if step.get("name") == "Build immutable Eros Relay image")["with"]
    for key in ("context", "file", "target", "build-contexts", "platforms", "build-args", "labels", "cache-from", "cache-to", "sbom", "provenance"):
        assert core_build[key] == relay_build[key], f"CORE and RELAY runtime image build differs at {key}"

    for document in (core, miniapp, eros, relay):
        deploy_steps = steps_for(document, "deploy")
        deploy_shell = run_text(deploy_steps)
        deploy_step = next(
            step for step in deploy_steps if "docker run --rm --pull=never" in step.get("run", "")
        )
        assert "ghcr.io/basecamp/kamal:v2.12.0@sha256:" in deploy_step["env"]["KAMAL_IMAGE"]
        assert "gem install kamal" not in deploy_shell
        assert "docker run --rm --pull=never --platform linux/amd64" in deploy_shell
