import importlib.util
import json
from pathlib import Path
import subprocess
import sys


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


def test_any_schema_or_migration_change_blocks_ordinary_release():
    for path in (
        "packages/db/prisma/schema.prisma",
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


def test_approved_prisma_migration_source_paths_enter_the_existing_protected_deploy():
    migration = "packages/db/prisma/migrations/20261103090000_add_index/migration.sql"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={migration}
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={migration, "packages/db/prisma/schema.prisma"},
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        changed_paths={"packages/db/prisma/schema.prisma"},
    ) == "BLOCK"


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


def test_blocked_main_path_set_fails_the_classifier_job(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {
        "apps/api/src/main.ts", "packages/db/prisma/schema.prisma",
    })
    assert MODULE.main() == 2
    assert "mode=BLOCK" in capsys.readouterr().out


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


def test_component_scope_is_exactly_single_component_and_uses_full_fallback(tmp_path):
    cases = (
        ({"apps/miniapp/src/main.tsx": "export default 1;\n"}, "MINIAPP"),
        ({"services/agata/app/responses/main.py": "VALUE = 1\n"}, "AGATA"),
        ({"apps/api/src/main.ts": "export const version = 2;\n"}, "CORE"),
        ({"apps/api/src/main.ts": "export const version = 2;\n",
          "apps/miniapp/src/main.tsx": "export default 1;\n"}, "FULL"),
        ({"docs/release/automation.md": "release\n"}, "FULL"),
    )
    for index, (changes, expected) in enumerate(cases):
        repo, _baseline, candidate, inventory = _component_repo(tmp_path / str(index), changes)
        result = MODULE.classify_component_scope(repo, candidate, inventory)
        assert result["scope"] == expected
        assert result["deploy_core"] is (expected in ("CORE", "FULL"))
        assert result["deploy_agata"] is (expected in ("AGATA", "FULL"))
        assert result["deploy_miniapp"] is (expected in ("MINIAPP", "FULL"))


def test_mixed_runtime_baseline_falls_back_to_full_component_deploy(tmp_path):
    repo, _baseline, candidate, inventory = _component_repo(
        tmp_path, {"apps/miniapp/src/main.tsx": "export default 1;\n"}
    )
    inventory["roles"]["gpoint-agata-api"]["source_sha"] = "c" * 40
    inventory["roles"]["gpoint-agata-memory-worker"]["source_sha"] = "c" * 40
    result = MODULE.classify_component_scope(repo, candidate, inventory)
    assert result["scope"] == "FULL"
    assert result["reason"] == "mixed_component_baseline"


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
