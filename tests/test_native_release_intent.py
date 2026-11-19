import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('path', ['scripts/transition-agata-worker-v3.py', 'services/gsales/tests/test_shared_worker_http.py'])
def test_retired_host_transition_and_shared_worker_fixture_are_known_non_runtime(path):
    assert MODULE._known_non_runtime_path(path)
    assert not MODULE._known_non_runtime_path(path + '.unreviewed')


@pytest.mark.parametrize('path,allowed', [
    ('docs/release/RELEASE_PERFORMANCE.md', True),
    ('scripts/release-readiness.mjs', True),
    ('scripts/resolve_native_environment_deployment.py', True),
    ('tests/test_native_environment_deployment.py', True),
    ('tests/test_release_component_manifest_v4.py', True),
    ('tests/test_release_static_exchange_v4.py', True),
    ('tests/test_release_static_executor_v4.py', True),
    ('tests/test_release_ui_artifact_v4.py', True),
    ('tests/test_release_ui_switch_v4.py', True),
    ('services/agata/worker/install_launch_agent.py', True),
    ('services/gsales/tests/test_shared_worker_http.py', True),
    ('services/agata/worker/unreviewed_installer.py', False),
])
def test_only_exact_owner_approved_legacy_deletions_enter_build_plan(tmp_path, path, allowed):
    repo, _, baseline, _ = _component_repo(tmp_path, {path: 'retired legacy entrypoint\n'})
    (repo / path).unlink()
    (repo / 'apps/api/src/main.ts').write_text('export const version = 2;\n')
    _git(repo, 'add', '--', path, 'apps/api/src/main.ts')
    _git(repo, 'commit', '-qm', 'remove approved legacy entrypoint')
    candidate = _git(repo, 'rev-parse', 'HEAD')
    sources = {component: baseline for component in MODULE.COMPONENT_ROLES}
    if allowed:
        plan = MODULE.classify_component_build_plan(repo, candidate, sources)
        assert 'CORE' in plan['build_components']
    else:
        with pytest.raises(ValueError, match='deleted or renamed'):
            MODULE.classify_component_build_plan(repo, candidate, sources)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_release_intent", ROOT / "scripts/native_release_intent.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_app_only_main_push_deploys_including_its_regression_tests():
    assert MODULE.classify_push(
        event_name="push",
        ref="refs/heads/main",
        changed_paths={"apps/api/src/main.ts", "apps/api/test/main.test.ts"},
    ) == "DEPLOY"


def test_me36_active_eros_model_config_does_not_build_or_update_eros_image(tmp_path):
    repo, baseline, _, _ = _component_repo(tmp_path, {
        "services/eros-engine/eros-layer7-staging-model-config.toml": "old config\n",
        "services/eros-engine/eros-production-model-config.toml": "old config\n",
        "apps/api/src/eros-response-prompt.js": "old prompt\n",
    })
    (repo / "services/eros-engine/eros-layer7-staging-model-config.toml").write_text("new config\n")
    (repo / "services/eros-engine/eros-production-model-config.toml").write_text("new config\n")
    (repo / "apps/api/src/eros-response-prompt.js").write_text("new prompt\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "ME-36 memory prompt and model config")
    candidate = _git(repo, "rev-parse", "HEAD")
    plan = MODULE.classify_component_build_plan(
        repo, candidate, dict.fromkeys(MODULE.ALL_COMPONENT_ROLES, baseline)
    )
    assert plan["deploy_scope"] == "CORE"
    assert plan["build_components"] == ["CORE"]


def test_normal_app_paths_cover_miniapp_bot_worker_agata_and_runtime_packages():
    for path in (
        "apps/miniapp/src/main.tsx",
        "apps/bot/src/index.ts",
        "apps/worker/src/index.ts",
        "services/agata/app/main.py",
        "packages/contracts/src/index.ts",
        "packages/agata-client/src/client.ts",
        "packages/db/src/index.ts",
        "pnpm-lock.yaml",
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths={path}
        ) == "DEPLOY", path


def test_known_operational_or_test_only_main_push_uses_the_same_release():
    for changed_paths in (
        {"scripts/verify.py", "scripts/new-helper.py"},
        {".kamal/hooks/pre-app-boot", ".kamal/hooks/post-app-boot"},
        {".kamal/secrets"},
        {".github/workflows/gpoint-operations.yml", "tests/test_native_release_intent.py"},
        {"docs/release/automation.md"},
        {"systemd/gpoint-backup.timer"},
        {"tests/test_release.py", "apps/api/test/main.test.ts"},
        {"services/gsales/app/main.py"},
        {"packages/gpoint-codex-mcp/src/server.ts"},
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=changed_paths
        ) == "DEPLOY"


def test_component_identity_infrastructure_pr_is_test_only_until_main_push():
    paths = {
        ".kamal/hooks/pre-app-boot", ".kamal/hooks/post-app-boot",
        ".github/actions/layer7-deploy/action.yml",
        "scripts/native_release_intent.py",
        "tests/test_kamal_release_config.py", "tests/test_kamal_post_app_boot_hook.py",
        "tests/test_layer7_staging_workflow.py",
    }
    assert MODULE.classify_push(
        event_name="pull_request", ref="refs/pull/999/merge", changed_paths=paths,
    ) == "NO_DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
    ) == "NO_DEPLOY"
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1].split("  backup_maintenance:\n", 1)[0]
    assert "github.event_name == 'push'" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "github.event_name == 'pull_request'" not in deploy


def test_app_and_release_files_can_ship_together_in_one_authorized_main_push():
    for changed_paths in (
        {"apps/api/src/main.ts", ".github/workflows/gpoint-native-release.yml"},
        {"apps/miniapp/src/main.tsx", "scripts/native_release_intent.py"},
        {"apps/bot/src/index.ts", "docs/release/automation.md"},
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=changed_paths
        ) == "DEPLOY"


def test_non_prisma_or_unclassified_migration_paths_block_ordinary_release():
    for path in (
        "services/agata/migrations/versions/0013_new.py",
        "packages/db/prisma/migrations/README.md",
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main",
            changed_paths={"apps/api/src/main.ts", path},
        ) == "BLOCK"
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths={path}
        ) == "BLOCK"


def test_prisma_schema_change_reaches_required_hosted_prisma_gates():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={"packages/db/prisma/schema.prisma"},
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={"apps/api/src/main.ts", "packages/db/prisma/schema.prisma"},
    ) == "DEPLOY"


def test_approved_prisma_migration_source_paths_enter_the_existing_protected_deploy():
    migration = "packages/db/prisma/migrations/20261103090000_add_index/migration.sql"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={migration}
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={migration, "packages/db/prisma/schema.prisma"},
    ) == "DEPLOY"


def test_exact_main_push_with_artifact_prepare_marker_builds_without_authorizing_deploy():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={"scripts/native_release_intent.py", "tests/test_native_release_intent.py"},
        commit_message="ME-36 candidate\n\nGPOINT-ARTIFACT-PREPARE",
    ) == "ARTIFACT_PREPARE"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={"apps/api/src/main.ts"},
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"new-root/unclassified.py"},
        commit_message="GPOINT-ARTIFACT-PREPARE",
    ) == "BLOCK"
    for paths in ({"apps/api/src/main.ts"},
                  {"packages/db/prisma/schema.prisma"},
                  {"packages/db/prisma/migrations/20261105100000_global_policy/migration.sql"}):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=paths,
            commit_message="GPOINT-ARTIFACT-PREPARE",
        ) == "BLOCK"


def test_layer7_api_route_change_cannot_fall_through_to_production_deploy():
    paths = {
        ".github/actions/layer7-deploy/action.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "scripts/layer7_release_scope.py",
        "scripts/native_release_intent.py",
        "tests/test_native_release_intent.py",
        "tests/test_layer7_release_scope.py",
        "tests/test_layer7_staging_workflow.py",
    }
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
    ) == "BLOCK"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
        commit_message="ME-36 Layer7 Core API embeddings fix\n\nGPOINT-LAYER7-API",
    ) == "LAYER7_API"
    assert not MODULE.push_deploy_authorized(
        mode="LAYER7_API", changed_paths={"apps/api/src/eros-response-prompt.js"},
    )
    selector_only_paths = paths - {"config/deploy.layer7-staging.eros-api.yml"}
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=selector_only_paths,
    ) == "ARTIFACT_PREPARE"


def test_layer7_release_tooling_builds_core_but_does_not_auto_apply():
    paths = {
        ".github/actions/layer7-deploy/action.yml",
        ".github/workflows/gpoint-native-release.yml",
        "docs/release/automation.md",
        "scripts/layer7_release_scope.py",
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
    }
    mode = MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
    )
    assert mode == "LAYER7_API"
    assert not MODULE.push_deploy_authorized(mode=mode, changed_paths=paths)
    assert not MODULE.push_deploy_authorized(
        mode="LAYER7_API",
        changed_paths={"apps/api/src/eros-response-prompt.js",
                       "services/eros-engine/eros-layer7-staging-model-config.toml"},
    )
    assert MODULE.push_deploy_authorized(
        mode="DEPLOY", changed_paths={"apps/api/src/main.ts"},
    )


def test_api_commit_cannot_bypass_approval_for_accumulated_infrastructure(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    def commit(path, contents):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        subprocess.run(["git", "-C", str(repo), "add", path], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True)
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    production_baseline = commit("README.md", "baseline\n")
    infra_commit = commit("config/deploy.yml", "infra change\n")
    api_commit = commit("apps/api/src/messages.ts", "api change\n")
    original_changed_paths = MODULE.changed_paths
    monkeypatch.setattr(
        MODULE, "changed_paths",
        lambda before, sha: original_changed_paths(before, sha, repo=repo),
    )
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", api_commit, "--before", infra_commit,
        "--infrastructure-baseline-sha", production_baseline,
        "--actor", "akhripunov", "--triggering-actor", "akhripunov",
        "--repository-owner", "akhripunov",
    ])

    assert MODULE.main() == 0
    output = capsys.readouterr().out
    assert "mode=DEPLOY" in output
    assert "deploy=false" in output


def test_main_push_without_verified_production_baseline_is_not_auto_applied():
    assert not MODULE.push_deploy_authorized(
        mode="DEPLOY", changed_paths={"apps/api/src/messages.ts"},
        cumulative_infrastructure_change=True,
    )


def test_marked_layer7_scope_fix_builds_only_core_without_authorizing_apply():
    paths = {
        "scripts/native_release_intent.py",
        "tests/test_native_release_intent.py",
        "tests/test_native_prepared_run.py",
    }
    kwargs = {"event_name": "push", "ref": "refs/heads/main",
              "commit_message": "ME-36 Layer7 tooling\n\nGPOINT-LAYER7-API"}
    mode = MODULE.classify_push(**kwargs, changed_paths=paths)
    assert mode == "LAYER7_API"
    assert not MODULE.push_deploy_authorized(mode=mode, changed_paths=paths)
    assert MODULE.classify_push(
        **kwargs, changed_paths=paths | {"apps/worker/src/main.ts"},
    ) == "BLOCK"


def test_layer7_eros_adapter_source_uses_only_the_explicit_api_route():
    paths = {
        "apps/api/src/eros-adapter.ts",
        "apps/api/test/eros-adapter.test.ts",
        "scripts/native_release_intent.py",
        "scripts/layer7_release_scope.py",
        "tests/test_native_release_intent.py",
        "tests/test_layer7_release_scope.py",
    }
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
        commit_message="ME-36 Layer7 adapter follow-up\n\nGPOINT-LAYER7-API",
    ) == "LAYER7_API"
    assert not MODULE.push_deploy_authorized(mode="LAYER7_API", changed_paths=paths)
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
    ) == "BLOCK"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=paths | {"apps/api/src/messages.ts"},
        commit_message="ME-36 Layer7 adapter follow-up\n\nGPOINT-LAYER7-API",
    ) == "BLOCK"


def test_artifact_prepare_marker_routes_main_push_to_build_only(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
        "--infrastructure-baseline-sha", "c" * 40,
        "--actor", "akhripunov", "--triggering-actor", "akhripunov",
        "--repository-owner", "akhripunov", "--commit-message", "ME-36\nGPOINT-ARTIFACT-PREPARE",
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {"scripts/native_release_intent.py"})
    assert MODULE.main() == 0
    output = capsys.readouterr().out
    assert "mode=ARTIFACT_PREPARE" in output
    assert "deploy=false" in output


def test_artifact_prepare_marker_in_subject_is_not_a_mode_marker():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"scripts/release_helper.py"},
        commit_message="GPOINT-ARTIFACT-PREPARE: release helper",
    ) == "DEPLOY"


def test_exact_release_tooling_candidate_routes_to_artifact_prepare_without_marker():
    me36_layer7_release_tooling = {
        ".github/actions/layer7-deploy/action.yml",
        ".github/workflows/gpoint-layer7-staging.yml",
        "scripts/verify_native_prepared_run.py",
        "tests/test_layer7_staging_workflow.py",
        "tests/test_native_prepared_run.py",
    }
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=me36_layer7_release_tooling,
    ) == "ARTIFACT_PREPARE"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths=set(MODULE.ARTIFACT_PREPARE_PATHS),
    ) == "ARTIFACT_PREPARE"


def test_agata_migration_and_unknown_schema_paths_remain_blocked_from_prisma_apply():
    for path in (
        "services/agata/migrations/versions/0013_new.py",
        "packages/another-db/migrations/001_new.sql",
        "apps/api/prisma/migrations/001_new.sql",
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths={path}
        ) == "BLOCK"


def test_unknown_paths_block_instead_of_becoming_a_deploy_or_silent_prepare():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"new-root/file.py"}
    ) == "BLOCK"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"packages/unclassified/src/index.ts"}
    ) == "BLOCK"


def test_prisma_schema_change_is_deployed_only_after_classifier_passes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
        "--actor", "akhripunov", "--triggering-actor", "akhripunov",
        "--repository-owner", "akhripunov",
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {
        "apps/api/src/main.ts", "packages/db/prisma/schema.prisma",
    })
    assert MODULE.main() == 0
    assert "mode=DEPLOY" in capsys.readouterr().out


@pytest.mark.parametrize(("actor", "triggering_actor"), [
    ("collaborator", "collaborator"),
    ("akhripunov", "collaborator"),
    ("", "akhripunov"),
    ("akhripunov", ""),
])
def test_main_push_requires_repository_owner_as_actor_and_triggering_actor(
    monkeypatch, capsys, actor, triggering_actor
):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
        "--actor", actor, "--triggering-actor", triggering_actor,
        "--repository-owner", "akhripunov",
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {"apps/api/src/main.ts"})

    assert MODULE.main() == 2
    output = capsys.readouterr()
    assert "NATIVE_RELEASE_INTENT=BLOCK" in output.err
    assert "repository owner" in output.err


def test_owner_main_push_remains_automatic_after_the_existing_tests_and_build(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
        "--infrastructure-baseline-sha", "c" * 40,
        "--actor", "akhripunov", "--triggering-actor", "akhripunov",
        "--repository-owner", "akhripunov",
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {"apps/api/src/main.ts"})
    assert MODULE.main() == 0
    assert "deploy=true" in capsys.readouterr().out


def test_dispatch_rerun_requires_repository_owner_as_triggering_actor(monkeypatch, capsys):
    sha = "a" * 40
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "workflow_dispatch", "--ref", "refs/heads/main",
        "--sha", sha, "--candidate-sha", sha,
        "--actor", "akhripunov", "--triggering-actor", "collaborator",
        "--repository-owner", "akhripunov", "--approve-production",
    ])

    assert MODULE.main() == 2
    assert "NATIVE_RELEASE_INTENT=BLOCK" in capsys.readouterr().err


def test_non_main_and_non_push_events_cannot_deploy():
    assert MODULE.classify_push(
        event_name="pull_request", ref="refs/pull/12/merge",
        changed_paths={"apps/api/src/main.ts"},
    ) == "NO_DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/feature", changed_paths={"apps/api/src/main.ts"}
    ) == "NO_DEPLOY"


def test_rehearsal_dispatch_is_main_exact_sha_owner_and_never_deploys():
    assert MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=True, approve_production=False,
    ) == ("REHEARSAL", False)


def test_invalid_rehearsal_dispatch_fails_closed():
    invalid = (
        {"ref": "refs/heads/feature", "candidate_sha": "a" * 40, "github_sha": "a" * 40,
         "actor": "akhripunov", "repository_owner": "akhripunov"},
        {"ref": "refs/heads/main", "candidate_sha": "b" * 40, "github_sha": "a" * 40,
         "actor": "akhripunov", "repository_owner": "akhripunov"},
        {"ref": "refs/heads/main", "candidate_sha": "a" * 40, "github_sha": "a" * 40,
         "actor": "other", "repository_owner": "akhripunov"},
        {"ref": "refs/heads/main", "candidate_sha": "a" * 40, "github_sha": "a" * 40,
         "actor": "akhripunov", "repository_owner": "akhripunov", "approve_production": True},
    )
    for event in invalid:
        event.setdefault("migration_rehearsal", True)
        event.setdefault("approve_production", False)
        assert MODULE.classify_dispatch(**event) == ("INVALID_REHEARSAL", False)


def test_ordinary_main_push_and_exact_sha_dispatch_remain_enabled():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"apps/api/src/main.ts"}
    ) == "DEPLOY"
    assert MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False, approve_production=True,
    ) == ("DEPLOY", True)


def test_manual_production_dispatch_requires_exact_owner_approval_before_build():
    for actor, approved in (("other", True), ("akhripunov", False)):
        assert MODULE.classify_dispatch(
            ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
            actor=actor, repository_owner="akhripunov",
            migration_rehearsal=False, approve_production=approved,
        ) == ("INVALID_DEPLOY", False)


def test_eros_image_rollback_is_opt_in_only_with_the_protected_rollback_operation():
    valid = MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov", migration_rehearsal=False,
        approve_production=True, rollback=True, rollback_eros=True,
    )
    assert valid == ("ROLLBACK", True)
    invalid = MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov", migration_rehearsal=False,
        approve_production=True, rollback=False, rollback_eros=True,
    )
    assert invalid == ("INVALID_ROLLBACK", False)


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _component_repo(tmp_path, changes):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    base_file = repo / "apps/api/src/main.ts"
    base_file.parent.mkdir(parents=True)
    base_file.write_text("export const version = 1;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    for path, contents in changes.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    repos = {
        "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
        "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
        "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
    }
    image_ids = {repo: "sha256:" + str(index) * 64 for index, repo in enumerate(set(repos.values()), 1)}
    inventory = {"roles": {}}
    for role, image_repo in repos.items():
        version = f"{baseline}-100-1"
        inventory["roles"][role] = {
            "name": f"{role}-{version}", "version": version, "source_sha": baseline,
            "image_id": image_ids[image_repo],
            "repo_digest": f"{image_repo}@sha256:" + str(list(set(repos.values())).index(image_repo) + 1) * 64,
        }
    return repo, baseline, candidate, inventory


def _add_eros_roles(inventory, *, baseline=None):
    roles = inventory["roles"]
    source = baseline or roles["gpoint-core-api"]["source_sha"]
    version = roles["gpoint-core-api"]["version"]
    runtime_image = roles["gpoint-core-api"]["image_id"]
    runtime_digest = roles["gpoint-core-api"]["repo_digest"]
    engine_role, relay_role = MODULE.EROS_ROLES
    engine_repo = MODULE.ROLE_REPOSITORIES[engine_role]
    relay_repo = MODULE.ROLE_REPOSITORIES[relay_role]
    roles[engine_role] = {
        "name": f"{engine_role}-{version}", "version": version, "source_sha": source,
        "image_id": "sha256:" + "4" * 64,
        "repo_digest": engine_repo + "@sha256:" + "5" * 64,
    }
    roles[relay_role] = {
        "name": f"{relay_role}-{version}", "version": version, "source_sha": source,
        "image_id": runtime_image,
        "repo_digest": runtime_digest if relay_repo == MODULE.ROLE_REPOSITORIES["gpoint-core-api"]
                       else relay_repo + "@sha256:" + "7" * 64,
    }
    return inventory


def test_component_scope_updates_only_exactly_touched_known_components(tmp_path):
    cases = (
        ({"apps/miniapp/src/main.tsx": "export default 1;\n"}, "MINIAPP"),
        ({"services/agata/app/responses/main.py": "VALUE = 1\n"}, "AGATA"),
        ({"apps/api/src/main.ts": "export const version = 2;\n"}, "CORE"),
        ({"apps/api/src/main.ts": "export const version = 2;\n",
          "apps/miniapp/src/main.tsx": "export default 1;\n"}, "CORE+MINIAPP"),
    )
    for index, (changes, expected) in enumerate(cases):
        repo, _baseline, candidate, inventory = _component_repo(tmp_path / str(index), changes)
        result = MODULE.classify_component_scope(repo, candidate, inventory)
        assert result["scope"] == expected
        components = set(expected.split("+"))
        assert result["deploy_core"] is ("CORE" in components)
        assert result["deploy_agata"] is ("AGATA" in components)
        assert result["deploy_miniapp"] is ("MINIAPP" in components)
        assert result["deploy_eros"] is False
        assert result["eros_bootstrap"] is False


def test_eros_engine_source_is_an_independent_runtime_component(tmp_path):
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}
    )
    _add_eros_roles(inventory, baseline=baseline)
    repository = MODULE.ROLE_REPOSITORIES[MODULE.EROS_ROLES[0]]

    result = MODULE.classify_component_scope(repo, candidate, inventory)

    assert result == {
        "scope": "EROS", "reason": "exact_component_diff",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": True, "deploy_eros_relay": False, "eros_bootstrap": False,
    }
    build_plan = MODULE.classify_component_build_plan(
        repo, candidate, dict.fromkeys(MODULE.ALL_COMPONENT_ROLES, baseline)
    )
    assert build_plan == {
        "deploy_scope": "EROS", "build_components": ["EROS"],
        "reason": "exact_component_diff",
    }
    expected = MODULE.expected_runtime_inventory(
        "EROS", inventory, candidate, candidate + "-101-1", "sha256:" + "8" * 64, "", "",
        eros_digest="sha256:" + "6" * 64,
    )
    assert expected["updatedComponents"] == ["EROS"]
    assert expected["roles"][MODULE.EROS_ROLES[0]]["sourceSha"] == candidate
    assert expected["roles"][MODULE.EROS_ROLES[0]]["repositoryDigest"] == repository + "@sha256:" + "6" * 64
    assert expected["roles"]["gpoint-core-api"]["sourceSha"] == baseline
    assert expected["roles"]["gpoint-core-api"]["version"] == inventory["roles"]["gpoint-core-api"]["version"]
    assert expected["roles"][MODULE.EROS_ROLES[1]]["sourceSha"] == baseline
    assert expected["roles"][MODULE.EROS_ROLES[1]]["baselineImageId"] == inventory["roles"][MODULE.EROS_ROLES[1]]["image_id"]


def test_eros_relay_source_updates_only_relay_with_shared_runtime_image(tmp_path):
    relay_path = "services/eros-engine/eros-pilot-embedding-relay.mjs"
    repo, baseline, candidate, inventory = _component_repo(tmp_path, {relay_path: "export const relay = true;\n"})
    _add_eros_roles(inventory, baseline=baseline)

    scope = MODULE.classify_component_scope(repo, candidate, inventory)
    build = MODULE.classify_component_build_plan(
        repo, candidate, dict.fromkeys(MODULE.ALL_COMPONENT_ROLES, baseline)
    )
    expected = MODULE.expected_runtime_inventory(
        "EROS_RELAY", inventory, candidate, candidate + "-101-1", "", "", "",
        eros_relay_digest="sha256:" + "8" * 64,
    )

    assert MODULE._component_paths(relay_path) == {"EROS_RELAY"}
    assert scope["scope"] == "EROS_RELAY"
    assert scope["deploy_core"] is False
    assert scope["deploy_eros"] is False
    assert expected["roles"]["gpoint-eros-pilot-engine"]["sourceSha"] == baseline
    assert build["deploy_scope"] == "EROS_RELAY"
    assert build["build_components"] == ["CORE"]
    assert expected["roles"]["gpoint-core-api"]["sourceSha"] == baseline
    assert expected["roles"]["gpoint-core-worker"]["sourceSha"] == baseline
    assert expected["roles"]["gpoint-eros-relay-relay"]["sourceSha"] == candidate


def test_first_eros_bootstrap_adds_engine_and_relay_with_exact_artifact_digests(tmp_path):
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}
    )
    result = MODULE.classify_component_scope(repo, candidate, inventory)
    assert result == {
        "scope": "EROS+EROS_RELAY", "reason": "exact_component_diff",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": True, "deploy_eros_relay": True, "eros_bootstrap": True,
    }

    expected = MODULE.expected_runtime_inventory(
        "EROS+EROS_RELAY", inventory, candidate, candidate + "-101-1", "sha256:" + "8" * 64, "", "",
        eros_digest="sha256:" + "6" * 64, eros_relay_digest="sha256:" + "8" * 64,
    )

    assert expected["updatedComponents"] == ["EROS", "EROS_RELAY"]
    assert set(expected["roles"]) == set(MODULE.COMPONENT_ROLES["CORE"] + MODULE.COMPONENT_ROLES["AGATA"] +
                                           MODULE.COMPONENT_ROLES["MINIAPP"] + MODULE.EROS_ROLES)
    assert expected["roles"][MODULE.EROS_ROLES[0]]["sourceSha"] == candidate
    assert expected["roles"][MODULE.EROS_ROLES[1]]["sourceSha"] == candidate
    assert expected["roles"][MODULE.EROS_ROLES[1]]["repositoryDigest"] == MODULE.ROLE_REPOSITORIES[MODULE.EROS_ROLES[1]] + "@sha256:" + "8" * 64


def test_eros_scope_fails_closed_without_stock_engine_digest(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}
    )
    with pytest.raises(ValueError, match="updated candidate runtime image digest is invalid"):
        MODULE.expected_runtime_inventory(
            "EROS", inventory, candidate, candidate + "-101-1", "sha256:" + "8" * 64, "", "",
        )


def test_eros_scope_fails_closed_without_core_runtime_support_digest(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}
    )
    with pytest.raises(ValueError, match="updated candidate runtime image digest is invalid"):
        MODULE.expected_runtime_inventory(
            "EROS+EROS_RELAY", inventory, candidate, candidate + "-101-1", "", "", "",
            eros_digest="sha256:" + "6" * 64,
        )


def test_installed_eros_inventory_requires_both_real_kamal_roles(tmp_path):
    _repo, baseline, _candidate, inventory = _component_repo(
        tmp_path, {"docs/release/automation.md": "synthetic fixture\n"}
    )
    _add_eros_roles(inventory, baseline=baseline)
    del inventory["roles"][MODULE.EROS_ROLES[1]]
    with pytest.raises(ValueError, match="unexpected role set"):
        MODULE._validate_role_inventory(inventory)


def test_component_scope_regressions_keep_eros_independent(tmp_path):
    cases = (
        ({"apps/api/src/main.ts": "export const version = 2;\n"}, "CORE"),
        ({"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}, "EROS"),
        ({"apps/miniapp/src/main.tsx": "export default 2;\n"}, "MINIAPP"),
        ({"docs/release/automation.md": "tooling-only\n"}, "NONE"),
    )
    for index, (changes, expected_scope) in enumerate(cases):
        repo, baseline, candidate, inventory = _component_repo(tmp_path / str(index), changes)
        _add_eros_roles(inventory, baseline=baseline)
        result = MODULE.classify_component_scope(repo, candidate, inventory)
        assert result["scope"] == expected_scope
        assert result["deploy_eros"] is (expected_scope == "EROS")
        assert result["eros_bootstrap"] is False
        assert result["deploy_core"] is (expected_scope == "CORE")
        assert result["deploy_miniapp"] is (expected_scope == "MINIAPP")
        expected = MODULE.expected_runtime_inventory(
            expected_scope, inventory, candidate, candidate + "-102-1",
            "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
            eros_digest="sha256:" + "6" * 64 if expected_scope == "EROS" else "",
        )
        if expected_scope != "EROS":
            for role in MODULE.EROS_ROLES:
                assert expected["roles"][role] == {
                    "version": inventory["roles"][role]["version"],
                    "sourceSha": inventory["roles"][role]["source_sha"],
                    "repositoryDigest": inventory["roles"][role]["repo_digest"],
                    "baselineImageId": inventory["roles"][role]["image_id"],
                }


def test_tooling_only_candidate_is_noop_with_staggered_component_baselines(tmp_path):
    repo, baseline, core_release, inventory = _component_repo(
        tmp_path, {"apps/api/src/main.ts": "export const version = 2;\n"}
    )
    core_version = f"{core_release}-101-1"
    for role in MODULE.COMPONENT_ROLES["CORE"]:
        inventory["roles"][role]["source_sha"] = core_release
        inventory["roles"][role]["version"] = core_version
        inventory["roles"][role]["name"] = f"{role}-{core_version}"

    workflow = repo / ".github/workflows/gpoint-native-release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: release workflow\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "release tooling only")
    candidate = _git(repo, "rev-parse", "HEAD")

    result = MODULE.classify_component_scope(repo, candidate, inventory)

    assert result == {
        "scope": "NONE", "reason": "no_runtime_changes",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": False, "deploy_eros_relay": False, "eros_bootstrap": False,
    }
    assert baseline != core_release
    expected = MODULE.expected_runtime_inventory(
        result["scope"], inventory, candidate, candidate + "-102-1",
        "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
    )
    assert expected["updatedComponents"] == []
    assert {
        role: (row["sourceSha"], row["version"])
        for role, row in expected["roles"].items()
    } == {
        role: (row["source_sha"], row["version"])
        for role, row in inventory["roles"].items()
    }


def test_build_plan_uses_per_component_sources_and_builds_core_for_prisma_migrations(tmp_path):
    repo, baseline, candidate, _inventory = _component_repo(
        tmp_path,
        {
            "apps/miniapp/src/main.tsx": "export default 1;\n",
            "packages/db/prisma/migrations/20261103120000_add_flag/migration.sql": "ALTER TABLE users ADD COLUMN flag boolean;\n",
        },
    )

    plan = MODULE.classify_component_build_plan(
        repo, candidate, {"CORE": baseline, "AGATA": baseline, "MINIAPP": baseline}
    )

    assert plan == {
        "deploy_scope": "MINIAPP",
        "build_components": ["CORE", "MINIAPP"],
        "reason": "exact_component_diff",
    }


def test_build_plan_skips_images_for_documentation_only_diff(tmp_path):
    repo, baseline, candidate, _inventory = _component_repo(
        tmp_path, {"docs/release/automation.md": "updated release guide\n"}
    )

    plan = MODULE.classify_component_build_plan(
        repo, candidate, {"CORE": baseline, "AGATA": baseline, "MINIAPP": baseline}
    )

    assert plan == {
        "deploy_scope": "NONE",
        "build_components": [],
        "reason": "no_runtime_changes",
    }


def test_shared_dependency_and_agata_migration_paths_select_their_real_images(tmp_path):
    repo, baseline, candidate, _inventory = _component_repo(
        tmp_path,
        {
            "packages/contracts/src/index.ts": "export type Id = string;\n",
            "services/agata/migrations/versions/0013_next.py": "revision = 'agata0013'\n",
        },
    )

    plan = MODULE.classify_component_build_plan(
        repo, candidate, {"CORE": baseline, "AGATA": baseline, "MINIAPP": baseline}
    )

    assert plan["deploy_scope"] == "AGATA+CORE+MINIAPP"
    assert plan["build_components"] == ["AGATA", "CORE", "MINIAPP"]


def test_staggered_component_baselines_do_not_redeploy_already_updated_components(tmp_path):
    repo, baseline, core_release, inventory = _component_repo(
        tmp_path, {"apps/api/src/main.ts": "export const version = 2;\n"}
    )
    version = f"{core_release}-101-1"
    for role in MODULE.COMPONENT_ROLES["CORE"]:
        inventory["roles"][role]["source_sha"] = core_release
        inventory["roles"][role]["version"] = version
        inventory["roles"][role]["name"] = f"{role}-{version}"
    target = repo / "apps/miniapp/src/main.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export default 1;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "miniapp candidate")
    candidate = _git(repo, "rev-parse", "HEAD")

    result = MODULE.classify_component_scope(repo, candidate, inventory)

    assert result["scope"] == "MINIAPP"
    assert result["reason"] == "exact_component_diff"
    assert result["deploy_core"] is False
    assert result["deploy_agata"] is False
    assert result["deploy_miniapp"] is True
    assert baseline != core_release


def test_unknown_paths_block_before_deploy_planning(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path, {"secrets/unmapped.txt": "do not deploy\n"}
    )
    with pytest.raises(ValueError, match="unmapped or unsafe candidate path"):
        MODULE.classify_component_scope(repo, candidate, inventory)


def test_documentation_only_candidate_verifies_runtime_without_restarting_components(tmp_path):
    for path in ("README.md", "docs/release/automation.md"):
        repo, _baseline, candidate, inventory = _component_repo(
            tmp_path / path.replace("/", "_"), {path: "updated documentation\n"}
        )

        scope = MODULE.classify_component_scope(repo, candidate, inventory)
        expected = MODULE.expected_runtime_inventory(
            scope["scope"], inventory, candidate, candidate + "-123-1",
            "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
        )

        assert scope == {"scope": "NONE", "reason": "no_runtime_changes",
                         "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
                         "deploy_eros": False, "deploy_eros_relay": False, "eros_bootstrap": False}
        assert expected["updatedComponents"] == []
        assert {row["sourceSha"] for row in expected["roles"].values()} == {_baseline}


def test_me36_release_helpers_are_exact_non_runtime_paths(tmp_path):
    helper_paths = (
        ".kamal/hooks/pre-app-boot",
        "scripts/eros-native-database.py",
        "scripts/eros-production-secrets.py",
        "scripts/verify-eros-runtime-labels.py",
        "scripts/verify-kamal-role.sh",
        "scripts/verify-layer7-ollama.py",
        "scripts/verify_native_eros_image.py",
        "scripts/verify_native_prepared_run.py",
    )
    changes = {path: "verified ME-36 release helper\n" for path in helper_paths}
    repo, _baseline, candidate, inventory = _component_repo(tmp_path, changes)

    scope = MODULE.classify_component_scope(repo, candidate, inventory)

    assert scope == {
        "scope": "NONE", "reason": "no_runtime_changes",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": False, "deploy_eros_relay": False, "eros_bootstrap": False,
    }


def test_known_release_infrastructure_candidate_has_no_application_component_scope(tmp_path):
    changes = {
        ".codex/roles/devops.md": "updated role policy\n",
        ".github/workflows/gpoint-native-release.yml": "updated workflow\n",
        "AGENTS.md": "updated instructions\n",
        "docs/operations/gpoint-vps-backups.md": "updated backup guide\n",
        "docs/release/automation.md": "updated release guide\n",
        "packages/db/prisma/production-approved-migrations.json": "[]\n",
        "packages/db/prisma/migrations/20261103120000_release_guard/migration.sql": "SELECT 1;\n",
        "scripts/gpoint-backup-policy.py": "updated backup policy\n",
        "scripts/classify-release-prepush.py": "updated pre-push classifier\n",
        "scripts/check-kamal-configs.sh": "updated Kamal config validation helper\n",
        "scripts/gpoint-scheduled-backup.py": "updated backup runner\n",
        "scripts/native_release_intent.py": "updated scope classifier\n",
        "scripts/prisma_migration_release_plan.py": "updated migration planner\n",
        "scripts/resolve_native_build_scope.py": "updated build resolver\n",
        "scripts/verify-gpoint-scheduled-backup-restore.sh": "updated restore verifier\n",
        "scripts/verify-kamal-migration-parity.py": "updated migration verifier\n",
        "scripts/verify-kamal-production-baseline.py": "updated runtime verifier\n",
        "scripts/verify_native_rollback_evidence.py": "updated rollback verifier\n",
        "scripts/write-kamal-production-manifest.py": "updated manifest writer\n",
        "systemd/gpoint-backup.service": "updated backup service\n",
        "tests/test_native_release_intent.py": "updated release tests\n",
    }
    repo, _baseline, candidate, inventory = _component_repo(tmp_path, changes)

    scope = MODULE.classify_component_scope(repo, candidate, inventory)

    assert scope == {
        "scope": "NONE", "reason": "no_runtime_changes",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": False, "deploy_eros_relay": False, "eros_bootstrap": False,
    }


def test_known_release_infrastructure_does_not_expand_an_application_scope(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path,
        {
            "apps/api/src/main.ts": "export const version = 2;\n",
            "scripts/native_release_intent.py": "updated scope classifier\n",
            ".github/workflows/gpoint-native-release.yml": "updated workflow\n",
        },
    )

    scope = MODULE.classify_component_scope(repo, candidate, inventory)

    assert scope["scope"] == "CORE"
    assert scope["deploy_core"] is True
    assert scope["deploy_agata"] is False
    assert scope["deploy_miniapp"] is False


def test_unlisted_release_script_still_requires_explicit_component_scope(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path, {"scripts/unreviewed-runtime-helper.py": "unmapped behavior\n"}
    )

    with pytest.raises(ValueError, match="unmapped or unsafe candidate path"):
        MODULE.classify_component_scope(repo, candidate, inventory)


def test_known_non_runtime_symlink_still_requires_explicit_component_scope(tmp_path):
    repo, _baseline, _candidate, inventory = _component_repo(
        tmp_path, {"docs/release-link.md": "regular file\n"}
    )
    path = repo / "docs/release-link.md"
    path.unlink()
    path.symlink_to("../apps/api/src/main.ts")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "replace release guide with symlink")
    candidate = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="candidate path is not a regular file"):
        MODULE.classify_component_scope(repo, candidate, inventory)


def test_candidate_equal_to_baseline_blocks_without_restarting_components(tmp_path):
    repo, baseline, _candidate, inventory = _component_repo(
        tmp_path, {"apps/miniapp/src/main.tsx": "export default 1;\n"}
    )
    with pytest.raises(ValueError, match="candidate equals production baseline"):
        MODULE.classify_component_scope(repo, baseline, inventory)


def test_component_runtime_inventory_pins_new_roles_and_preserves_untouched_identity(tmp_path):
    _repo, baseline, _candidate, inventory = _component_repo(
        tmp_path, {"apps/miniapp/src/main.tsx": "export default 1;\n"}
    )
    candidate = "d" * 40
    version = candidate + "-123-2"
    result = MODULE.expected_runtime_inventory(
        "MINIAPP", inventory, candidate, version,
        "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
    )
    assert result["roles"]["gpoint-miniapp-web"] == {
        "version": version, "sourceSha": candidate,
        "repositoryDigest": "ghcr.io/akhripunov/gpoint-miniapp@sha256:" + "3" * 64,
        "baselineImageId": None,
    }
    assert result["roles"]["gpoint-core-api"]["sourceSha"] == baseline
    assert result["roles"]["gpoint-core-api"]["baselineImageId"].startswith("sha256:")
    encoded = MODULE.expected_runtime_inventory_b64(
        "MINIAPP", inventory, candidate, version,
        "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
    )
    assert json.loads(__import__("base64").b64decode(encoded)) == result
    multi = MODULE.expected_runtime_inventory(
        "CORE+MINIAPP", inventory, candidate, version,
        "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
    )
    assert multi["roles"]["gpoint-core-api"]["sourceSha"] == candidate
    assert multi["roles"]["gpoint-miniapp-web"]["sourceSha"] == candidate
    assert multi["roles"]["gpoint-agata-api"]["sourceSha"] == baseline


@pytest.mark.parametrize("mode,builds", [("BOOTSTRAP_FULL", ["CORE", "AGATA", "MINIAPP", "EROS"]),
                                        ("VERIFIED", ["CORE", "MINIAPP"])])
def test_workflow_plan_keeps_extra_builds_separate_from_runtime_scope(tmp_path, monkeypatch, capsys, mode, builds):
    import yaml
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"apps/miniapp/public/release-smoke.json": '{"attempt":2}\n'})
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    step = next(s for s in workflow["jobs"]["deploy"]["steps"] if s.get("id") == "component_plan")
    verify_images = next(s for s in workflow["jobs"]["deploy"]["steps"]
                         if s.get("name") == "Verify immutable candidate images selected for the live scope")
    verify_runtime = next(s for s in workflow["jobs"]["deploy"]["steps"] if s.get("id") == "verify")
    for key in ("APP_DIGEST", "AGATA_DIGEST", "MINIAPP_DIGEST"):
        assert verify_images["env"][key] == "${{ needs.build.outputs." + key.lower() + " }}"
        assert verify_runtime["env"][key] == "${{ steps.component_plan.outputs." + key.lower() + " }}"
    script = step["run"].split('python3 - <<\'PY\' >> "$GITHUB_OUTPUT"\n', 1)[1].rsplit('\nPY', 1)[0]
    monkeypatch.chdir(repo)
    monkeypatch.setitem(sys.modules, "native_release_intent", MODULE)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for key, value in {
        "CANDIDATE_SHA": candidate, "IMAGE_VERSION": candidate + "-200-1",
        "BASELINE_ROLE_INVENTORY": json.dumps(inventory), "BASELINE_MODE": mode,
        "BUILD_COMPONENTS": json.dumps(builds), "BUILD_DEPLOY_SCOPE": "MINIAPP",
        "BUILD_BASELINE_SOURCES": json.dumps(dict.fromkeys(MODULE.COMPONENT_ROLES, baseline)),
        "APP_DIGEST": "sha256:" + "4" * 64, "AGATA_DIGEST": "sha256:" + "5" * 64,
        "MINIAPP_DIGEST": "sha256:" + "6" * 64,
    }.items():
        monkeypatch.setenv(key, value)
    exec(compile(script, "component_plan", "exec"), {})
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert outputs["scope"] == "MINIAPP"
    assert outputs["app_digest"] == inventory["roles"]["gpoint-core-api"]["repo_digest"].split("@", 1)[1]
    assert outputs["agata_digest"] == inventory["roles"]["gpoint-agata-api"]["repo_digest"].split("@", 1)[1]
    assert outputs["miniapp_digest"] == "sha256:" + "6" * 64
    assert outputs["eros_digest"] == ""
    assert outputs["migration_app_digest"] == "sha256:" + "4" * 64
    assert outputs["core_api_version"] == inventory["roles"]["gpoint-core-api"]["version"]
    assert outputs["core_api_source_sha"] == baseline


def test_workflow_engine_only_preserves_core_and_relay_without_core_build(tmp_path, monkeypatch, capsys):
    import yaml
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"})
    _add_eros_roles(inventory, baseline=baseline)
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    step = next(s for s in workflow["jobs"]["deploy"]["steps"] if s.get("id") == "component_plan")
    script = step["run"].split('python3 - <<\'PY\' >> "$GITHUB_OUTPUT"\n', 1)[1].rsplit('\nPY', 1)[0]
    monkeypatch.chdir(repo)
    monkeypatch.setitem(sys.modules, "native_release_intent", MODULE)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for key, value in {
        "CANDIDATE_SHA": candidate, "IMAGE_VERSION": candidate + "-200-1",
        "BASELINE_ROLE_INVENTORY": json.dumps(inventory), "BASELINE_MODE": "VERIFIED",
        "BUILD_COMPONENTS": '["EROS"]', "BUILD_DEPLOY_SCOPE": "EROS",
        "BUILD_BASELINE_SOURCES": json.dumps(dict.fromkeys(MODULE.ALL_COMPONENT_ROLES, baseline)),
        "APP_DIGEST": "sha256:" + "8" * 64, "EROS_DIGEST": "sha256:" + "9" * 64,
        "AGATA_DIGEST": "", "MINIAPP_DIGEST": "",
    }.items():
        monkeypatch.setenv(key, value)

    exec(compile(script, "component_plan_eros", "exec"), {})

    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    plan = json.loads(base64.b64decode(outputs["expected_role_identities_b64"]))
    assert outputs["scope"] == "EROS"
    assert outputs["deploy_core"] == "false"
    assert outputs["deploy_eros"] == "true"
    assert outputs["deploy_eros_relay"] == "false"
    assert outputs["app_digest"] == inventory["roles"]["gpoint-core-api"]["repo_digest"].split("@", 1)[1]
    assert plan["roles"]["gpoint-core-api"]["sourceSha"] == baseline
    assert plan["roles"]["gpoint-eros-relay-relay"]["repositoryDigest"] == (
        inventory["roles"]["gpoint-eros-relay-relay"]["repo_digest"])


def test_rollback_dispatch_requires_main_exact_owner_explicit_approval_and_no_other_maintenance():
    args = dict(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False,
        rollback=True, approve_production=True,
    )
    assert MODULE.classify_dispatch(**args) == ("ROLLBACK", True)
    for changes in (
        {"ref": "refs/heads/feature"},
        {"candidate_sha": "b" * 40},
        {"actor": "other"},
        {"approve_production": False},
        {"migration_rehearsal": True},
    ):
        invalid = dict(args)
        invalid.update(changes)
        assert MODULE.classify_dispatch(**invalid)[1] is False
        assert MODULE.classify_dispatch(**invalid)[0] != "ROLLBACK"


def test_layer7_eros_push_requires_explicit_isolated_artifact_route():
    paths = {"services/eros-engine/upstream.sha", "scripts/eros-native-database.py"}
    context = {"event_name": "push", "ref": "refs/heads/main"}
    marker = "Stock Eros update\n\nGPOINT-LAYER7-EROS"
    assert MODULE.classify_push(**context, changed_paths=paths, commit_message=marker) == "LAYER7_EROS"
    assert MODULE.classify_push(**context, changed_paths=paths) == "BLOCK"
    assert MODULE.classify_push(
        **context, changed_paths=paths | {"apps/api/src/main.ts"}, commit_message=marker
    ) == "BLOCK"
    assert MODULE.classify_push(
        **context, changed_paths=paths, commit_message=marker + "\nGPOINT-ARTIFACT-PREPARE"
    ) == "BLOCK"


def test_layer7_eros_migration_gate_correction_cannot_route_to_old_production():
    paths = {"scripts/eros-native-database.py", "tests/test_eros_native_database.py"}
    context = {"event_name": "push", "ref": "refs/heads/main"}
    assert MODULE.classify_push(**context, changed_paths=paths,
                                commit_message="GPOINT-LAYER7-EROS") == "LAYER7_EROS"
    assert MODULE.classify_push(**context, changed_paths=paths) == "BLOCK"


def test_layer7_runner_compatibility_fix_keeps_explicit_eros_route():
    paths = {".github/workflows/gpoint-layer7-staging.yml",
             "tests/test_layer7_staging_workflow.py"}
    context = {"event_name": "push", "ref": "refs/heads/main"}
    assert MODULE.classify_push(**context, changed_paths=paths,
                                commit_message="GPOINT-LAYER7-EROS") == "LAYER7_EROS"
    assert MODULE.classify_push(**context, changed_paths=paths) == "BLOCK"


def test_relay_recovery_uses_its_own_baseline_after_engine_has_advanced(tmp_path):
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/eros-pilot-embedding-relay.mjs": "export {};\n"},
    )
    _add_eros_roles(inventory, baseline=baseline)
    engine, relay = MODULE.EROS_ROLES
    version = candidate + "-200-1"
    inventory["roles"][engine].update(
        name=f"{engine}-{version}", version=version, source_sha=candidate,
    )

    plan = MODULE.classify_component_scope(repo, candidate, inventory)

    assert plan["scope"] == "EROS_RELAY"
    assert plan["deploy_eros"] is False
    built = MODULE.classify_component_build_plan(
        repo, candidate, {**dict.fromkeys(MODULE.COMPONENT_ROLES, baseline),
                          "EROS": candidate, "EROS_RELAY": baseline},
    )
    assert built["build_components"] == ["CORE"]
    expected = MODULE.expected_runtime_inventory(
        plan["scope"], inventory, candidate, version, "", "", "",
        eros_relay_digest="sha256:" + "8" * 64,
    )
    assert expected["roles"][engine]["baselineImageId"] == inventory["roles"][engine]["image_id"]
    assert expected["roles"][relay]["sourceSha"] == candidate


def test_shared_runtime_dockerfile_builds_core_once_for_core_and_installed_relay(tmp_path):
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"infra/production/Gpoint.Dockerfile": "FROM scratch\n"},
    )
    assert MODULE.classify_component_scope(repo, candidate, inventory)["scope"] == "CORE+MINIAPP"

    _add_eros_roles(inventory, baseline=baseline)
    plan = MODULE.classify_component_build_plan(repo, candidate, MODULE.installed_component_sources(inventory))

    assert plan["deploy_scope"] == "CORE+EROS_RELAY+MINIAPP"
    assert plan["build_components"] == ["CORE", "MINIAPP"]
