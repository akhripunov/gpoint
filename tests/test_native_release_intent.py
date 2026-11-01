import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_release_intent", ROOT / "scripts/native_release_intent.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_exact_prep_only_push_is_classified_without_deploy():
    assert MODULE.classify_push(
        event_name="push",
        ref="refs/heads/main",
        commit_message="Add rehearsal path\n\nGPOINT_MIGRATION_REHEARSAL_PREP=1",
        changed_paths=MODULE.PREP_ONLY_PATHS,
    ) == "PREPARE_ONLY"


def test_exact_fail_closed_workflow_correction_push_remains_prepare_only():
    correction_paths = {
        ".github/workflows/gpoint-native-release.yml",
        "scripts/native_release_intent.py",
        "tests/test_native_release_intent.py",
        "tests/test_native_build_boundary.py",
    }
    assert MODULE.classify_push(
        event_name="push",
        ref="refs/heads/main",
        commit_message="Update actionlint schema support\n\nGPOINT_MIGRATION_REHEARSAL_PREP=1",
        changed_paths=correction_paths,
    ) == "PREPARE_ONLY"


def test_prep_marker_with_unapproved_or_application_path_blocks_deploy():
    assert MODULE.classify_push(
        event_name="push",
        ref="refs/heads/main",
        commit_message="GPOINT_MIGRATION_REHEARSAL_PREP=1",
        changed_paths=MODULE.PREP_ONLY_PATHS | {"apps/api/src/main.ts"},
    ) == "BLOCK"


def test_prep_only_push_requires_the_exact_marker_and_rehearsal_workflow():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", commit_message="setup only",
        changed_paths=MODULE.PREP_ONLY_PATHS,
    ) == "DEPLOY"
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main",
        commit_message="GPOINT_MIGRATION_REHEARSAL_PREP=1",
        changed_paths=MODULE.PREP_ONLY_PATHS - {".github/workflows/gpoint-native-release.yml"},
    ) == "BLOCK"


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


def test_normal_main_push_and_dispatch_remain_ordinary_releases():
    assert MODULE.classify_push(
        event_name="push", ref="refs/heads/main", commit_message="Product change",
        changed_paths={"apps/api/src/main.ts"},
    ) == "DEPLOY"
    assert MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="akhripunov", repository_owner="akhripunov",
        migration_rehearsal=False, approve_production=False,
    ) == ("DEPLOY", True)
