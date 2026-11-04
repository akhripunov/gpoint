import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest


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
        {".github/workflows/gpoint-native-release.yml", "tests/test_native_release_intent.py"},
        {"docs/release/automation.md"},
        {"systemd/gpoint-backup.timer"},
        {"tests/test_release.py", "apps/api/test/main.test.ts"},
        {"services/gsales/app/main.py"},
        {"packages/gpoint-codex-mcp/src/server.ts"},
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=changed_paths
        ) == "DEPLOY"


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


def test_backup_maintenance_dispatch_is_separate_exact_and_explicitly_authorized():
    valid = MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False, backup_maintenance=True, approve_production=True,
    )
    assert valid == ("BACKUP_MAINTENANCE", False)
    for changes, expected in (
        ({"ref": "refs/heads/feature"}, "INVALID_BACKUP_MAINTENANCE"),
        ({"candidate_sha": "b" * 40}, "INVALID_BACKUP_MAINTENANCE"),
        ({"actor": "other"}, "INVALID_BACKUP_MAINTENANCE"),
        ({"approve_production": False}, "INVALID_BACKUP_MAINTENANCE"),
        ({"migration_rehearsal": True}, "INVALID_REHEARSAL"),
    ):
        args = dict(
            ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
            actor="akhripunov", repository_owner="akhripunov",
            migration_rehearsal=False, backup_maintenance=True, approve_production=True,
        )
        args.update(changes)
        assert MODULE.classify_dispatch(**args) == (expected, False)


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


def test_eros_engine_source_is_an_independent_runtime_component(tmp_path):
    repo, baseline, candidate, inventory = _component_repo(
        tmp_path, {"services/eros-engine/build-stock-image.sh": "#!/bin/sh\n"}
    )
    repository = "ghcr.io/akhripunov/gpoint-eros"
    inventory["roles"]["gpoint-eros-engine"] = {
        "name": f"gpoint-eros-engine-{baseline}-100-1",
        "version": f"{baseline}-100-1",
        "source_sha": baseline,
        "image_id": "sha256:" + "4" * 64,
        "repo_digest": repository + "@sha256:" + "5" * 64,
    }

    result = MODULE.classify_component_scope(repo, candidate, inventory)

    assert result == {
        "scope": "EROS", "reason": "exact_component_diff",
        "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False,
        "deploy_eros": True,
    }
    expected = MODULE.expected_runtime_inventory(
        "EROS", inventory, candidate, candidate + "-101-1", "", "", "",
        eros_digest="sha256:" + "6" * 64,
    )
    assert expected["updatedComponents"] == ["EROS"]
    assert expected["roles"]["gpoint-eros-engine"]["sourceSha"] == candidate
    assert expected["roles"]["gpoint-eros-engine"]["repositoryDigest"] == repository + "@sha256:" + "6" * 64
    assert expected["roles"]["gpoint-core-api"]["sourceSha"] == baseline


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
                         "deploy_core": False, "deploy_agata": False, "deploy_miniapp": False}
        assert expected["updatedComponents"] == []
        assert {row["sourceSha"] for row in expected["roles"].values()} == {_baseline}


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
        "scripts/resolve_native_environment_deployment.py": "updated deployment resolver\n",
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


@pytest.mark.parametrize("mode,builds", [("BOOTSTRAP_FULL", ["CORE", "AGATA", "MINIAPP"]),
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
    assert outputs["migration_app_digest"] == "sha256:" + "4" * 64
    assert outputs["core_api_version"] == inventory["roles"]["gpoint-core-api"]["version"]
    assert outputs["core_api_source_sha"] == baseline


def test_rollback_dispatch_requires_main_exact_owner_explicit_approval_and_no_other_maintenance():
    args = dict(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False, backup_maintenance=False,
        rollback=True, approve_production=True,
    )
    assert MODULE.classify_dispatch(**args) == ("ROLLBACK", False)
    for changes in (
        {"ref": "refs/heads/feature"},
        {"candidate_sha": "b" * 40},
        {"actor": "other"},
        {"approve_production": False},
        {"migration_rehearsal": True},
        {"backup_maintenance": True},
    ):
        invalid = dict(args)
        invalid.update(changes)
        assert MODULE.classify_dispatch(**invalid)[1] is False
        assert MODULE.classify_dispatch(**invalid)[0] != "ROLLBACK"
