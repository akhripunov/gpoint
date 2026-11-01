import importlib.util
from pathlib import Path
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


def test_operational_or_test_only_main_push_never_builds_or_deploys():
    for changed_paths in (
        {"scripts/verify.py", "scripts/new-helper.py"},
        {".github/workflows/gpoint-native-release.yml", "tests/test_native_release_intent.py"},
        {"docs/release/automation.md"},
        {"systemd/gpoint-backup.timer"},
        {"tests/test_release.py", "apps/api/test/main.test.ts"},
        {"services/gsales/app/main.py"},
        {"packages/gpoint-codex-mcp/src/server.ts"},
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=changed_paths
        ) == "PREPARE_ONLY"


def test_app_mixed_with_workflow_release_or_unrelated_operations_blocks():
    for changed_paths in (
        {"apps/api/src/main.ts", ".github/workflows/gpoint-native-release.yml"},
        {"apps/miniapp/src/main.tsx", "scripts/native_release_intent.py"},
        {"apps/bot/src/index.ts", "docs/release/automation.md"},
        {"apps/api/src/main.ts", "unknown/new-root-scope.txt"},
    ):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths=changed_paths
        ) == "BLOCK"


def test_any_schema_or_migration_change_blocks_ordinary_release():
    for path in (
        "packages/db/prisma/schema.prisma",
        "packages/db/prisma/migrations/20261101000000_new/migration.sql",
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
