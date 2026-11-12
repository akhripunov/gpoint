"""Queue scoping: independent events must not serialise, same environment must."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_queue_is_scoped_per_operation_not_per_branch():
    group = load()["concurrency"]["group"]
    # The ordinary release queues per event and ref; special operations live in
    # their own workflows with their own queues.
    assert group == "gpoint-${{ github.event_name }}-release-${{ github.ref }}"
    assert load()["concurrency"]["cancel-in-progress"] is False
    staging = yaml.safe_load((ROOT / ".github/workflows/gpoint-layer7-staging.yml").read_text())
    assert staging["jobs"]["layer7_staging"]["concurrency"] == {
        "group": "gpoint-layer7-staging", "cancel-in-progress": False}


def test_mutating_jobs_keep_one_lock_per_environment():
    jobs = load()["jobs"]
    assert jobs["deploy"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["deploy"]["concurrency"]["cancel-in-progress"] is False
    staging = yaml.safe_load((ROOT / ".github/workflows/gpoint-layer7-staging.yml").read_text())
    assert staging["jobs"]["layer7_staging"]["concurrency"]["group"] == "gpoint-layer7-staging"
    operations = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())
    assert operations["jobs"]["migration_rehearsal"]["concurrency"]["group"] == "gpoint-production-release"
    assert "backup_maintenance" not in operations["jobs"]
    assert "rollback" not in operations["jobs"]
