"""Queue scoping: independent events must not serialise, same environment must."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_queue_is_scoped_by_event_not_only_ref():
    workflow = load()
    # A staging dispatch and a production push share refs/heads/main; keying the
    # workflow queue by ref alone made staging wait for an unrelated release.
    assert workflow["concurrency"]["group"] == "gpoint-${{ github.event_name }}-${{ github.ref }}"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_mutating_jobs_keep_one_lock_per_environment():
    jobs = load()["jobs"]
    assert jobs["deploy"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["deploy"]["concurrency"]["cancel-in-progress"] is False
    assert jobs["layer7_staging"]["concurrency"]["group"] == "gpoint-layer7-staging"
    assert jobs["rollback"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["backup_maintenance"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["migration_rehearsal"]["concurrency"]["group"] == "gpoint-production-release"
