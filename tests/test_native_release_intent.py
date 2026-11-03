import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_release_intent", ROOT / "scripts/native_release_intent.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_allowed_main_push_selects_automatic_exact_sha_deploy():
    assert MODULE.classify_push(
        event_name="push",
        ref="refs/heads/main",
        changed_paths={"apps/api/src/main.ts", "apps/api/test/main.test.ts"},
    ) == "DEPLOY"


def test_initial_eros_bootstrap_exception_is_bound_to_exact_base_and_runtime_tree(monkeypatch):
    paths = {"apps/api/src/main.ts", "services/eros-engine/local-e2e.test.mjs",
             "config/deploy.eros-api.yml"}
    monkeypatch.setattr(MODULE, "eros_runtime_tree_digest", lambda _sha, _repo: MODULE.EROS_BOOTSTRAP_TREE_SHA256)
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
        before=MODULE.EROS_BOOTSTRAP_BASE_SHA, sha="a" * 40,
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
        before="b" * 40, sha="a" * 40,
    ) == "BLOCK"
    monkeypatch.setattr(MODULE, "eros_runtime_tree_digest", lambda _sha, _repo: "0" * 64)
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths=paths,
        before=MODULE.EROS_BOOTSTRAP_BASE_SHA, sha="a" * 40,
    ) == "BLOCK"


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


def test_known_operational_or_test_only_main_push_uses_native_release_flow():
    for changed_paths in (
        {"scripts/verify.py", "scripts/new-helper.py"},
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


def test_eros_runtime_hooks_and_config_changes_block_until_explicit_pilot_reconciliation():
    for path in (".kamal/hooks/pre-app-boot", ".kamal/hooks/post-app-boot", "config/eros-pilot.yml",
                 "scripts/eros-new-operation.py", "scripts/collect_kamal_eros_state.py",
                 "scripts/kamal_eros_state.py", "scripts/automated-eros-new-operation.py",
                 "scripts/kamal-eros-new-operation.py", "scripts/check-eros-pilot.py",
                 "scripts/deploy-eros-pilot.py"):
        assert MODULE.classify_push(
            event_name="push", ref="refs/heads/main", changed_paths={path}
        ) == "BLOCK", path


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


def test_ordinary_main_push_and_exact_sha_dispatch_are_supported():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={"apps/api/src/main.ts"}
    ) == "DEPLOY"
    assert MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False, approve_production=True,
    ) == ("DEPLOY", True)


def test_main_push_classifier_output_enables_only_the_automatic_native_deploy_job(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "native_release_intent.py", "--event", "push", "--ref", "refs/heads/main",
        "--sha", "a" * 40, "--before", "b" * 40,
    ])
    monkeypatch.setattr(MODULE, "changed_paths", lambda *_args, **_kwargs: {
        "apps/api/src/main.ts", "scripts/native_release_intent.py",
    })
    assert MODULE.main() == 0
    output = capsys.readouterr().out
    assert "mode=DEPLOY" in output
    assert "deploy=true" in output


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


def test_synthetic_eros_pilot_is_a_separate_exact_sha_owner_opt_in():
    result = MODULE.classify_eros_pilot_dispatch(
        event_name="workflow_dispatch", ref="refs/heads/main",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=False, environment_approved=False,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
    )
    assert result == ("SYNTHETIC_PILOT_READY", False)


def test_synthetic_eros_apply_requires_its_own_approval_and_protected_environment():
    args = dict(
        event_name="workflow_dispatch", ref="refs/heads/main",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=True, environment_approved=True,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
    )
    assert MODULE.classify_eros_pilot_dispatch(**args) == ("SYNTHETIC_PILOT_APPLY", False)
    for changes in (
        {"opt_in": False}, {"approve_apply": False}, {"environment_approved": False},
        {"approve_production": True}, {"migration_rehearsal": True},
        {"candidate_sha": "b" * 40}, {"actor": "other"},
    ):
        invalid = dict(args)
        invalid.update(changes)
        assert MODULE.classify_eros_pilot_dispatch(**invalid)[1] is False


def test_pilot_apply_intent_never_sets_the_ordinary_deploy_output():
    args = dict(
        event_name="workflow_dispatch", ref="refs/heads/main",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=True, environment_approved=True,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
    )
    mode, deploy = MODULE.classify_eros_pilot_dispatch(**args)
    assert mode == "SYNTHETIC_PILOT_APPLY"
    assert deploy is False


def test_synthetic_pilot_cleanup_requires_a_separate_exact_sha_environment_gate():
    base = dict(
        event_name="workflow_dispatch", ref="refs/heads/main",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=False, environment_approved=False,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
        cleanup_pilot=True, approve_cleanup=False,
    )
    assert MODULE.classify_eros_pilot_dispatch(**base) == ("SYNTHETIC_PILOT_CLEANUP_REQUESTED", False)
    approved = dict(base, approve_cleanup=True, environment_approved=True)
    assert MODULE.classify_eros_pilot_dispatch(**approved) == ("SYNTHETIC_PILOT_CLEANUP", False)
    assert MODULE.classify_eros_pilot_dispatch(**(approved | {"test_user_ids": ""}))[0] == "INVALID_SYNTHETIC_PILOT"
    assert MODULE.classify_eros_pilot_dispatch(**(approved | {"approve_production": True}))[0] == "INVALID_SYNTHETIC_PILOT"
    for changes in ({"approve_apply": True}, {"approve_cleanup": True, "environment_approved": False}):
        assert MODULE.classify_eros_pilot_dispatch(**(approved | changes))[0] == "INVALID_SYNTHETIC_PILOT_CLEANUP"


def test_non_main_exact_sha_synthetic_dispatch_is_verify_only_and_never_opens_apply():
    args = dict(
        event_name="workflow_dispatch", ref="refs/heads/codex/me36-eros-engine-v1",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=False, environment_approved=False,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
    )
    assert MODULE.classify_eros_pilot_dispatch(**args) == ("SYNTHETIC_PILOT_VERIFY_ONLY", False)
    for changes in ({"approve_apply": True}, {"environment_approved": True}, {"cleanup_pilot": True}):
        assert MODULE.classify_eros_pilot_dispatch(**(args | changes))[0] == "INVALID_SYNTHETIC_PILOT"


def test_synthetic_eros_pilot_rejects_non_synthetic_or_multiple_allowlist_ids():
    args = dict(
        event_name="workflow_dispatch", ref="refs/heads/main",
        candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        opt_in=True, approve_apply=False, environment_approved=False,
        approve_production=False, migration_rehearsal=False,
        backup_maintenance=False, rollback=False,
        test_user_ids="10000000-0000-4000-8000-000000000001",
        test_creator_ids="20000000-0000-4000-8000-000000000002",
    )
    for changes in (
        {"test_user_ids": ""},
        {"test_user_ids": "10000000-0000-4000-8000-000000000001,30000000-0000-4000-8000-000000000003"},
        {"test_creator_ids": "not-a-uuid"},
        {"test_creator_ids": "20000000-0000-4000-8000-000000000002,30000000-0000-4000-8000-000000000003"},
    ):
        invalid = dict(args)
        invalid.update(changes)
        assert MODULE.classify_eros_pilot_dispatch(**invalid) == ("INVALID_SYNTHETIC_PILOT", False)
