from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from layer7_release_scope import API, REQUIRED_ROLES, plan, verify_runtime
from native_release_intent import classify_push


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


def test_scope_uses_full_diff_from_every_running_role(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    base = commit(tmp_path, "base", "apps/api/src/eros-response-prompt.js", "a")
    prompt = commit(tmp_path, "prompt", "apps/api/src/eros-response-prompt.js", "b")
    assert plan(tmp_path, prompt, inventory(base), previous=base) == ["CORE"]
    tooling = commit(tmp_path, "tooling", ".github/workflows/gpoint-native-release.yml", "updated")
    with pytest.raises(ValueError, match="outside"):
        plan(tmp_path, tooling, inventory(base), previous=base)
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


def test_auto_workflow_is_branch_restricted_and_only_mutates_api():
    import yaml

    native = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())["jobs"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-layer7-staging.yml").read_text())
    layer7 = workflow["jobs"]["layer7_staging"]
    caller = native["layer7_deploy"]
    assert caller["needs"] == ["tests", "layer7_scope", "build"]
    assert caller["with"]["prepared_app_digest"] == "${{ needs.build.outputs.app_digest }}"
    assert layer7["environment"]["name"] == "${{ github.event_name == 'push' && 'layer7-api-auto' || 'layer7-staging' }}"
    scope = next(step["run"] for step in native["layer7_scope"]["steps"] if step.get("id") == "scope")
    assert "/environments/layer7-api-auto" in scope
    assert '"$environment/deployment-branch-policies"' in scope
    assert '[("main", "branch")]' in scope
    assert next(step for step in layer7["steps"] if step.get("id") == "staging_env")["if"] == "github.event_name == 'workflow_dispatch'"
    assert "STAGING_ENV_BUNDLE" not in layer7["env"]
    assert native["layer7_scope"]["permissions"] == {"contents": "read", "actions": "read"}
    assert 'custom_branch_policies' in scope
    assert 'rules != ["branch_policy"]' in scope
    assert scope.index('gh api "$environment"') < scope.index('scripts/layer7-transport.sh ssh')
    assert 'github.triggering_actor == github.repository_owner' in native["layer7_scope"]["if"]
    assert all("secrets" not in str(step) for step in native["layer7_scope"]["steps"])
    assert layer7["concurrency"]["group"] == "gpoint-layer7-staging"
    assert "github.actor == github.repository_owner" in native["layer7_scope"]["if"]
    assert "github.event_name == 'push'" in native["layer7_scope"]["if"]
    steps = layer7["steps"]
    preflight = next(step for step in steps if step.get("id") == "scoped")
    assert 'cmp "$before" "$before.live"' in preflight["run"]
    for title in ("Apply isolated Prisma and SQLx migrations with recovery gates",
                  "Install staging secrets and provision only the empty GPoint staging database"):
        assert next(step for step in steps if step.get("name") == title)["if"] == "github.event_name == 'workflow_dispatch'"
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

    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-layer7-staging.yml").read_text())
    step = next(step for step in workflow["jobs"]["layer7_staging"]["steps"]
                if step.get("name", "").startswith("Deploy exact immutable"))
    body = step["run"]
    recovery = body[body.index('if ! GPOINT_KAMAL_EXPECTED_DIGEST='):body.rindex('if [[ "$SCOPED" != true ]]')]
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
        **os.environ, "GITHUB_WORKSPACE": str(tmp_path), "RUNNER_TEMP": str(tmp_path),
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
