"""Queue scoping: independent events must not serialise, same environment must."""

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_queue_is_scoped_per_operation_not_per_branch():
    group = load()["concurrency"]["group"]
    # Keying by ref alone made staging wait for an unrelated production push.
    assert group.startswith("gpoint-${{ github.event_name }}-")
    # Every mutating dispatch operation gets its own queue key, so a staging
    # deploy never waits for a rollback and vice versa.
    for marker in ("inputs.rollback == true && 'rollback'",
                   "inputs.migration_rehearsal == true && 'rehearsal'",
                   "inputs.backup_maintenance == true && 'backup'",
                   "inputs.deploy_layer7_staging == true && 'staging'"):
        assert marker in group
    assert "'release'" in group
    assert load()["concurrency"]["cancel-in-progress"] is False


def test_mutating_jobs_keep_one_lock_per_environment():
    jobs = load()["jobs"]
    assert jobs["deploy"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["deploy"]["concurrency"]["cancel-in-progress"] is False
    assert jobs["layer7_staging"]["concurrency"]["group"] == "gpoint-layer7-staging"
    assert jobs["rollback"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["backup_maintenance"]["concurrency"]["group"] == "gpoint-production-release"
    assert jobs["migration_rehearsal"]["concurrency"]["group"] == "gpoint-production-release"
