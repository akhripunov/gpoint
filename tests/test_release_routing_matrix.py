"""Integrated release event routing and Deploy-only mode gates."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())
DEPLOY = WORKFLOW["jobs"]["deploy"]
BUILD = WORKFLOW["jobs"]["build"]

REPLACEMENTS = (
    (r"always\(\)", "True"),
    (r"cancelled\(\)", "False"),
    (r"success\(\)", "True"),
    (r"inputs\.mode", "inputs_mode"),
    (r"steps\.[A-Za-z0-9_]+\.outputs\.[A-Za-z0-9_]+ == '[^']*'", "True"),
    (r"steps\.[A-Za-z0-9_]+\.outcome == 'failure'", "False"),
    (r"steps\.[A-Za-z0-9_]+\.outcome != 'success'", "False"),
    (r"steps\.[A-Za-z0-9_]+\.outputs\.[A-Za-z0-9_]+", "'true'"),
    (r"steps\.[A-Za-z0-9_]+\.outcome", "'success'"),
)


def evaluate(expression: str, *, inputs_mode: str) -> bool:
    source = expression
    for pattern, replacement in REPLACEMENTS:
        source = re.sub(pattern, replacement, source)
    source = source.replace("&&", " and ").replace("||", " or ")
    source = re.sub(r"!(?!=)", " not ", source)
    return bool(eval(source, {"__builtins__": {}}, {"inputs_mode": inputs_mode}))  # noqa: S307

def test_pr_push_and_deploy_dispatch_form_one_connected_release_flow():
    events = WORKFLOW.get("on", WORKFLOW.get(True))
    tests = WORKFLOW["jobs"]["tests"]
    build = BUILD
    assert events["pull_request"]["branches"] == ["main"]
    assert events["push"]["branches"] == ["main"]
    assert tests["if"] == "github.event_name != 'workflow_dispatch'"
    assert set(tests["outputs"]) == {"mode"}
    assert "github.event_name == 'push'" in build["if"]
    assert "github.ref == 'refs/heads/main'" in build["if"]
    assert "needs.tests.result == 'success'" in build["if"]
    assert "needs.tests.outputs.mode == 'DEPLOY'" in build["if"]
    assert "pull_request" not in build["if"]
    assert "needs.tests.outputs.deploy" not in build["if"]
    assert "needs" not in DEPLOY
    assert "workflow_dispatch" in DEPLOY["if"]
    assert "github.ref == 'refs/heads/main'" in DEPLOY["if"]
    assert "github.actor == github.repository_owner" in DEPLOY["if"]
    assert "inputs.approve_production == true" in DEPLOY["if"]
    assert DEPLOY["environment"]["name"] == "production"
    assert DEPLOY["concurrency"]["group"] == "gpoint-production-release"


def test_deploy_uses_prior_attempt_manifest_and_has_no_current_run_ci_dependency():
    deploy_text = yaml.safe_dump(DEPLOY)
    assert "needs.tests" not in deploy_text
    assert "needs.build" not in deploy_text
    assert "GITHUB_RUN_ID/jobs" not in deploy_text
    assert "github.event_name == 'push'" not in deploy_text
    assert "infrastructure_baseline" not in deploy_text
    find = next(step for step in DEPLOY["steps"] if step.get("id") == "prepared_run")
    assert "/attempts/$run_attempt/jobs" in find["run"]
    assert "Tests (workspace)" in find["run"]
    assert "Tests (python)" in find["run"]
    assert "Build and publish immutable images" in find["run"]
    assert "conclusion == \"success\"" in find["run"]
    evidence = next(step for step in DEPLOY["steps"] if step.get("id") == "eros_e2e_evidence")
    assert evidence["env"]["SOURCE_RUN_ID"] == "${{ steps.prepared_run.outputs.run_id }}"
    assert evidence["env"]["SOURCE_RUN_ATTEMPT"] == "${{ steps.prepared_run.outputs.run_attempt }}"
    assert "/attempts/$SOURCE_RUN_ATTEMPT/jobs" in evidence["run"]
    assert not any(step.get("uses", "").startswith("docker/build-push-action@")
                   for step in DEPLOY["steps"])
    download = next(step for step in DEPLOY["steps"]
                    if step.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["artifact-ids"] == "${{ steps.prepared_run.outputs.artifact_id }}"
    assert download["with"]["run-id"] == "${{ steps.prepared_run.outputs.run_id }}"
    assert download["with"]["digest-mismatch"] == "error"
    manifest = next(step for step in DEPLOY["steps"] if step.get("id") == "prepared")
    assert manifest["id"] == "prepared"
    assert "IMAGE_VERSION={version}" in manifest["run"]


ORDINARY_MUTATIONS = {
    "component_plan", "migration_baseline", "migration_plan", "deployment",
    "migration_apply", "eros_database", "kamal_apply", "verify",
}
ROLLBACK_STEPS = {"manifest", "evidence", "baseline", "rollback_apply", "rollback_verify"}


def test_no_phase_can_run_in_the_wrong_mode():
    steps = DEPLOY["steps"]
    ordinary = [s for s in steps if s.get("id") in ORDINARY_MUTATIONS]
    rollback = [s for s in steps if s.get("id") in ROLLBACK_STEPS]
    assert ordinary and rollback
    for step in ordinary:
        assert step.get("if"), f"{step['id']} has no mode gate"
        assert evaluate(step["if"], inputs_mode="rollback") is False, step["id"]
        assert evaluate(step["if"], inputs_mode="deploy") is True, step["id"]
    for step in rollback:
        assert step.get("if"), f"{step['id']} has no mode gate"
        assert evaluate(step["if"], inputs_mode="deploy") is False, step["id"]
        assert evaluate(step["if"], inputs_mode="rollback") is True, step["id"]
