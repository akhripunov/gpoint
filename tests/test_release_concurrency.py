"""Queue scoping: independent events must not serialise, same environment must."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_queue_is_scoped_per_operation_not_per_branch():
    group = load()["concurrency"]["group"]
    # PR validation is isolated from the serialized production transaction.
    assert group == "gpoint-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || 'native-production-transaction' }}"
    assert load()["concurrency"]["cancel-in-progress"] is False


def test_mutating_jobs_keep_one_lock_per_environment():
    jobs = load()["jobs"]
    assert jobs["deploy"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["deploy"]["concurrency"]["cancel-in-progress"] is False
    operations = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())
    assert operations["jobs"]["migration_rehearsal"]["concurrency"]["group"] == "gpoint-production-release"
    assert "backup_maintenance" not in operations["jobs"]
    assert "rollback" not in operations["jobs"]
