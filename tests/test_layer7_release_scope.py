from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from layer7_release_scope import API, REQUIRED_ROLES, plan, verify_runtime
from native_release_intent import ARTIFACT_PREPARE_PATHS, LAYER7_CORE_CONFIG_TOOLING_PATHS, classify_push


def test_core_config_release_tooling_uses_one_shared_target_allowlist():
    source = (ROOT / "scripts/layer7_release_scope.py").read_text()
    assert "LAYER7_CORE_CONFIG_TOOLING_PATHS" in source
    assert "LAYER7_CORE_CONFIG_RELEASE_PATHS =" not in source
    assert "CORE_CONFIG_REQUIRED_PATHS =" not in source
    assert "LAYER7_API_PREPARE_TOOLING_PATHS =" not in (ROOT / "scripts/native_release_intent.py").read_text()
    assert {
        ".github/actions/layer7-deploy/action.yml",
        ".github/workflows/gpoint-layer7-staging.yml",
        ".github/workflows/gpoint-native-release.yml",
        "docs/release/automation.md",
        "scripts/layer7_release_scope.py",
        "scripts/native_change_surface.py",
        "scripts/native_release_intent.py",
        "scripts/verify_native_prepared_run.py",
        "tests/test_kamal_native_automatic_release.py",
        "tests/test_kamal_release_config.py",
        "tests/test_layer7_release_scope.py",
        "tests/test_layer7_staging_workflow.py",
        "tests/test_native_build_boundary.py",
        "tests/test_native_change_surface.py",
        "tests/test_native_prepared_run.py",
        "tests/test_native_prisma_apply_boundary.py",
        "tests/test_native_release_intent.py",
    } <= LAYER7_CORE_CONFIG_TOOLING_PATHS
    assert classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=set(LAYER7_CORE_CONFIG_TOOLING_PATHS),
    ) == "LAYER7_API"
    assert classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=set(LAYER7_CORE_CONFIG_TOOLING_PATHS) | {"apps/worker/src/main.ts"},
    ) == "BLOCK"


def commit(repo, message, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "add", "--", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def inventory(sha):
    return {
        "roles": {role: {"source": sha, "container": "a" * 64, "image": "sha256:" + "b" * 64,
                         "digest": "sha256:" + "c" * 64, "version": sha + "-1-1"}
                  for role in REQUIRED_ROLES},
        "prisma": ["20261105100000_sample=" + "d" * 64],
        "sqlx": ["1=" + "e" * 96],
        "database": {"postgres": "f" * 64, "redis": "1" * 64},
    }


def test_owner_main_marker_does_not_select_production():
    args = dict(event_name="push", ref="refs/heads/main", changed_paths={"apps/api/src/eros-response-prompt.js"})
    assert classify_push(**args, commit_message="GPOINT-LAYER7-API") == "LAYER7_API"
    assert classify_push(**args, commit_message="GPOINT-LAYER7-API\nGPOINT-ARTIFACT-PREPARE") == "BLOCK"


def test_me36_core_and_eros_config_changes_are_prepare_only():
    paths = {
        "apps/api/src/eros-response-prompt.js",
        "apps/api/src/eros-worker-contract.ts",
        "apps/api/test/eros-worker-contract.test.ts",
        "services/eros-engine/eros-language-contract.test.mjs",
        "services/eros-engine/eros-layer7-staging-model-config.toml",
        "services/eros-engine/eros-pilot-model-config.toml",
        "services/eros-engine/eros-production-model-config.toml",
        "services/eros-engine/local-e2e.test.mjs",
        "services/eros-engine/test-model-config.toml",
        ".github/workflows/gpoint-layer7-staging.yml",
        ".github/actions/layer7-deploy/action.yml",
        "scripts/layer7_release_scope.py",
        "scripts/native_release_intent.py",
        "tests/test_layer7_release_scope.py",
        "tests/test_native_release_intent.py",
        "tests/test_layer7_staging_workflow.py",
    }
    args = dict(event_name="push", ref="refs/heads/main", changed_paths=paths)
    assert classify_push(**args, commit_message="GPOINT-ARTIFACT-PREPARE") == "ARTIFACT_PREPARE"
    assert classify_push(**args, commit_message="GPOINT-LAYER7-API") == "BLOCK"


def test_layer7_core_api_config_rollout_requires_exact_marker_and_allowlisted_scope():
    config_path = "config/deploy.layer7-staging.eros-api.yml"
    scoped_paths = {
        config_path,
        ".github/actions/layer7-deploy/action.yml",
        "tests/test_layer7_staging_workflow.py",
        "scripts/native_release_intent.py",
        "scripts/layer7_release_scope.py",
        "tests/test_layer7_release_scope.py",
    }
    assert classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=scoped_paths,
        commit_message="ME-36 exact Core API scope\nGPOINT-LAYER7-API",
    ) == "LAYER7_API"
    assert classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={config_path},
    ) == "BLOCK"
    assert classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=scoped_paths | {"apps/worker/src/main.ts"},
        commit_message="GPOINT-LAYER7-API",
    ) == "BLOCK"
    assert classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={config_path},
        commit_message="GPOINT-LAYER7-API\nGPOINT-ARTIFACT-PREPARE",
    ) == "BLOCK"


def test_native_selector_bootstrap_is_build_only_and_not_layer7_deploy():
    tooling_only = {
        ".github/actions/layer7-deploy/action.yml",
        "scripts/native_release_intent.py",
        "tests/test_native_release_intent.py",
        "scripts/layer7_release_scope.py",
        "tests/test_layer7_release_scope.py",
        "tests/test_layer7_staging_workflow.py",
    }
    assert tooling_only <= ARTIFACT_PREPARE_PATHS
    assert classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=tooling_only,
    ) == "ARTIFACT_PREPARE"
    assert classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=tooling_only,
        commit_message="GPOINT-LAYER7-API",
    ) == "BLOCK"


def test_scope_uses_full_diff_from_every_running_role(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    base = commit(tmp_path, "base", "apps/api/src/eros-response-prompt.js", "a")
    prompt = commit(tmp_path, "prompt", "apps/api/src/eros-response-prompt.js", "b")
    assert plan(tmp_path, prompt, inventory(base), previous=base) == ["CORE"]
    adapter = commit(tmp_path, "Layer7 API Eros adapter", "apps/api/src/eros-adapter.ts", "adapter")
    assert plan(tmp_path, adapter, inventory(prompt), previous=prompt) == ["CORE"]
    config = commit(tmp_path, "Layer7 API network config", "config/deploy.layer7-staging.eros-api.yml",
                   "network: [shared_internal, gpoint_eros_api, gpoint_eros_embeddings]\n")
    assert plan(tmp_path, config, inventory(adapter), previous=adapter) == ["CORE"]
    tooling = commit(tmp_path, "tooling", ".github/workflows/gpoint-native-release.yml", "updated")
    assert plan(tmp_path, tooling, inventory(base), previous=base) == ["CORE"]
    prompt = commit(tmp_path, "prompt", "apps/api/src/eros-response-prompt.js", "b2")
    assert plan(tmp_path, prompt, inventory(base), previous=tooling) == ["CORE"]
    assert plan(tmp_path, prompt, inventory(prompt)) == []
    worker = commit(tmp_path, "worker", "apps/worker/src/main.ts", "new behavior")
    latest = commit(tmp_path, "prompt", "apps/api/src/eros-response-prompt.js", "c")
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, latest, inventory(base))
    mixed = inventory(worker)
    mixed["roles"]["gpoint-core-worker"] = inventory(prompt)["roles"]["gpoint-core-worker"]
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, latest, mixed)


def test_scope_allows_only_known_release_router_paths_alongside_core_config(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    base = commit(tmp_path, "base", "apps/api/src/eros-response-prompt.js", "old prompt")
    staging = commit(tmp_path, "Layer7 staging readiness",
                     ".github/workflows/gpoint-layer7-staging.yml", "readiness workflow")
    action = commit(tmp_path, "Layer7 action", ".github/actions/layer7-deploy/action.yml", "action")
    router = commit(tmp_path, "release router", "scripts/native_release_intent.py", "router")
    verifier = commit(tmp_path, "prepared verifier", "scripts/verify_native_prepared_run.py", "verifier")
    staging_test = commit(tmp_path, "staging workflow test",
                          "tests/test_layer7_staging_workflow.py", "staging test")
    prepared_test = commit(tmp_path, "prepared run test", "tests/test_native_prepared_run.py", "prepared test")
    router_test = commit(tmp_path, "release router test", "tests/test_native_release_intent.py", "test")
    config = commit(tmp_path, "active Eros config",
                    "services/eros-engine/eros-layer7-staging-model-config.toml", "model = 'qwen'\n")
    candidate = commit(tmp_path, "Core prompt", "apps/api/src/eros-response-prompt.js", "new prompt")
    assert plan(tmp_path, candidate, inventory(base), previous=base) == ["CORE"]

    worker = commit(tmp_path, "out-of-scope worker", "apps/worker/src/main.ts", "worker change")
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, worker, inventory(base), previous=base)


def test_scope_reconciles_only_the_verified_historical_layer7_range(tmp_path, monkeypatch):
    import layer7_release_scope as scope

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    api_path = "config/deploy.layer7-staging.eros-api.yml"
    eros_path = "services/eros-engine/upstream.sha"
    base = commit(tmp_path, "baseline", api_path, "before")
    installed = commit(tmp_path, "Eros release", eros_path, "verified upstream")
    candidate = commit(tmp_path, "Core API rollout", api_path, "after")
    snapshot = inventory(base)
    snapshot["roles"][scope.ENGINE] = inventory(installed)["roles"][scope.ENGINE]
    digest = snapshot["roles"][scope.ENGINE]["digest"]
    monkeypatch.setattr(scope, "RECONCILED_LAYER7_RANGE", (base, installed, digest), raising=False)

    assert plan(tmp_path, candidate, snapshot, previous=installed) == ["CORE"]
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, candidate, inventory(base), previous=installed)
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, candidate, snapshot)
    snapshot["roles"][scope.ENGINE]["digest"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, candidate, snapshot, previous=installed)
    snapshot["roles"][scope.ENGINE]["digest"] = digest

    drift = commit(tmp_path, "Eros changed again", eros_path, "unverified upstream")
    next_api = commit(tmp_path, "API rollout after drift", api_path, "next")
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, next_api, snapshot, previous=drift)
    with pytest.raises(ValueError, match="push changed paths outside"):
        plan(tmp_path, next_api, snapshot, previous=candidate)


def test_verification_refuses_changes_to_other_roles_and_database():
    candidate = "2" * 40
    before = inventory("1" * 40)
    after = inventory("1" * 40)
    after["roles"][API] = {**after["roles"][API], "source": candidate, "container": "3" * 64,
                            "version": candidate + "-4-1"}
    verify_runtime(before, after, candidate, candidate + "-4-1", "sha256:" + "c" * 64)
    after["roles"]["gpoint-core-worker"]["container"] = "4" * 64
    with pytest.raises(ValueError, match="untouched"):
        verify_runtime(before, after, candidate, candidate + "-4-1", "sha256:" + "c" * 64)
    after["roles"]["gpoint-core-worker"] = before["roles"]["gpoint-core-worker"]
    after["sqlx"] = []
    with pytest.raises(ValueError, match="database"):
        verify_runtime(before, after, candidate, candidate + "-4-1", "sha256:" + "c" * 64)


def test_verification_allows_only_an_eros_container_restart_with_the_same_image():
    candidate = "2" * 40
    before = inventory("1" * 40)
    after = inventory("1" * 40)
    after["roles"][API] = {**after["roles"][API], "source": candidate,
                            "container": "3" * 64, "version": candidate + "-4-1"}
    after["roles"]["gpoint-eros-pilot-engine"] = {
        **after["roles"]["gpoint-eros-pilot-engine"], "container": "4" * 64,
        "restarts": "1",
    }
    verify_runtime(before, after, candidate, candidate + "-4-1", "sha256:" + "c" * 64,
                   eros_config_update=True)
    after["roles"]["gpoint-eros-pilot-engine"]["digest"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="image changed"):
        verify_runtime(before, after, candidate, candidate + "-4-1", "sha256:" + "c" * 64,
                       eros_config_update=True)


def test_layer7_core_api_apply_uses_the_existing_protected_environment_and_stock_kamal():
    import yaml

    native = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())["jobs"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-layer7-staging.yml").read_text())
    layer7 = workflow["jobs"]["layer7_staging"]
    deploy = native["deploy"]
    assert native["build"]["needs"] == ["tests"]
    assert layer7["environment"]["name"] == "layer7-staging"
    assert "workflow_call" not in workflow[True]
    assert "STAGING_ENV_BUNDLE" not in layer7["env"]
    assert layer7["concurrency"]["group"] == "gpoint-layer7-staging"
    assert "github.event_name == 'push'" in deploy["if"]
    assert "needs.tests.outputs.mode == 'LAYER7_API'" in deploy["if"]
    assert "github.actor == github.repository_owner" in deploy["if"]
    assert "github.triggering_actor == github.repository_owner" in deploy["if"]
    assert deploy["environment"]["name"] == "${{ needs.tests.outputs.mode == 'LAYER7_API' && 'layer7-api-auto' || 'production' }}"
    assert deploy["concurrency"]["group"] == "${{ needs.tests.outputs.mode == 'LAYER7_API' && 'gpoint-layer7-staging' || 'gpoint-production-release' }}"
    steps = yaml.safe_load((ROOT / ".github/actions/layer7-deploy/action.yml").read_text())["runs"]["steps"]
    assert not any(step.get("id") == "scoped" for step in steps)
    scope = next(step for step in steps if step.get("id") == "protected_scope")
    assert "github.event_name == 'push'" in scope["if"]
    assert "repos/$GITHUB_REPOSITORY/environments/layer7-api-auto" in scope["run"]
    assert '"$environment/deployment-branch-policies"' in scope["run"]
    assert "layer7_release_scope.py paths" in scope["run"]
    assert "PREVIOUS_SHA: ${{ github.event.before }}" not in str(scope.get("env", {}))
    for title in ("Apply isolated Prisma and SQLx migrations with recovery gates",
                  "Install staging secrets and provision only the empty GPoint staging database",
                  "Layer7 staging preflight and isolated GPoint schema bootstrap"):
        assert "steps.protected_scope.outputs.narrow != 'true'" in next(
            step for step in steps if step.get("name") == title)["if"]
    pull = next(step for step in steps if step.get("name", "").startswith("Pull and verify exact private"))
    assert pull["run"].index('if [[ "$SCOPED" != true ]]') < pull["run"].index('images+=(')
    deploy = next(step["run"] for step in steps if step.get("name", "").startswith("Deploy exact immutable"))
    assert deploy.index('if [[ "$SCOPED" != true ]]') < deploy.index("kamal accessory boot")
    assert "LAYER7_API_PREVIOUS_RESTORED" in deploy
    assert "LAYER7_API_RECOVERY_REQUIRED" in deploy
    assert "{{.Id}}" in deploy and '"$previous_api" "$previous_id"' in deploy
    assert deploy.rindex('if [[ "$SCOPED" != true ]]') < deploy.index('for role in worker bot')
    verify = next(step["run"] for step in steps if step.get("name", "").startswith("Verify runtime roles"))
    assert '"${MINIAPP_DIGEST:--}" "${EROS_DIGEST:--}"' in verify
    assert 'layer7_release_scope.py verify' in verify


@pytest.mark.parametrize("verified_old,expected", [(True, "PREVIOUS_RESTORED"), (False, "RECOVERY_REQUIRED")])
def test_failed_kamal_switch_restores_only_verified_previous_api(tmp_path, verified_old, expected):
    import json
    import os
    import yaml

    action = yaml.safe_load((ROOT / ".github/actions/layer7-deploy/action.yml").read_text())
    step = next(step for step in action["runs"]["steps"]
                if step.get("name", "").startswith("Deploy exact immutable"))
    body = step["run"]
    recovery_start = body.index('if ! GPOINT_KAMAL_EXPECTED_DIGEST="$CORE_DIGEST" GPOINT_KAMAL_SERVICE=gpoint-core')
    recovery = body[recovery_start:body.rindex('if [[ "$SCOPED" != true ]]')]
    snapshot = tmp_path / "layer7-verified-baseline.json"
    snapshot.write_text(json.dumps(inventory("1" * 40)))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    ssh = scripts / "layer7-transport.sh"
    ssh.write_text('#!/bin/bash\nif [[ "$3" == bash ]]; then cat >/dev/null; test "$OLD_VERIFIED" = true; fi\n')
    ssh.chmod(0o755)
    script = '''
set -euo pipefail
kamal() {
  if [[ "$1" == redeploy ]]; then return 1; fi
  printf '%s\\n' "$*" >> "$RUNNER_TEMP/kamal.log"
}
check_queue_empty() { return 0; }
''' + recovery
    env = {
        **os.environ, "GITHUB_WORKSPACE": str(tmp_path), "RUNNER_TEMP": str(tmp_path), "NARROW_SCOPE": "false",
        "GPOINT_LAYER7_STAGING_HOST": "example", "SCOPED": "true",
        "CORE_DIGEST": "sha256:" + "c" * 64, "IMAGE_VERSION": "2" * 40 + "-4-1",
        "previous_api": "gpoint-core-api-" + "1" * 40 + "-1-1",
        "OLD_VERIFIED": str(verified_old).lower(),
    }
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert f"LAYER7_API_{expected}" in result.stderr
    calls = tmp_path / "kamal.log"
    if verified_old:
        assert f"app start --version {'1' * 40}-1-1 --roles api" in calls.read_text()
    else:
        assert not calls.exists()
