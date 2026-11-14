"""Release routing: Tests -> Build -> Deploy and the rollback mode.

Run 43148553035 taught one lesson; this file teaches the other one. When Build is
skipped, a job that needs it is skipped too unless its condition uses always().
Rollback intentionally skips Build (it redeploys already published images), so the
Deploy admission must be evaluated, not eyeballed.
"""

import itertools
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
DEPLOY = WORKFLOW["jobs"]["deploy"]
BUILD = WORKFLOW["jobs"]["build"]

REPLACEMENTS = (
    (r"always\(\)", "True"),
    (r"cancelled\(\)", "False"),
    (r"success\(\)", "True"),
    (r"github\.event_name", "event"),
    (r"github\.ref", "ref"),
    (r"github\.actor", "actor"),
    (r"github\.repository_owner", "owner"),
    (r"needs\.tests\.result", "tests_result"),
    (r"needs\.build\.result", "build_result"),
    (r"needs\.tests\.outputs\.mode", "tests_mode"),
    (r"needs\.tests\.outputs\.deploy", "tests_deploy"),
    (r"needs\.build\.outputs\.deploy_scope", "deploy_scope"),
    (r"inputs\.mode", "inputs_mode"),
    (r"steps\.[A-Za-z0-9_]+\.outputs\.[A-Za-z0-9_]+ == '[^']*'", "True"),
    (r"steps\.[A-Za-z0-9_]+\.outcome == 'failure'", "False"),
    (r"steps\.[A-Za-z0-9_]+\.outcome != 'success'", "False"),
    (r"steps\.[A-Za-z0-9_]+\.outputs\.[A-Za-z0-9_]+", "'true'"),
    (r"steps\.[A-Za-z0-9_]+\.outcome", "'success'"),
)


def evaluate(expression: str, **values) -> bool:
    defaults = {
        "tests_mode": "DEPLOY", "tests_deploy": "true", "tests_result": "success",
        "build_result": "success", "deploy_scope": "CORE", "event": "push",
        "ref": "refs/heads/main", "actor": "akhripunov", "owner": "akhripunov",
        "inputs_mode": "deploy",
    }
    defaults.update(values)
    values = defaults
    source = expression
    for pattern, replacement in REPLACEMENTS:
        source = re.sub(pattern, replacement, source)
    source = source.replace("&&", " and ").replace("||", " or ")
    source = re.sub(r"!(?!=)", " not ", source)
    return bool(eval(source, {"__builtins__": {}}, values))  # noqa: S307 - fixed test input


def should_deploy(*, event, ref, actor, owner, mode, tests_result, build_result,
                  tests_mode, deploy_scope, tests_deploy) -> bool:
    """Independent reading of the release rules, used to check the workflow."""
    if tests_result != "success" or tests_deploy != "true":
        return False
    if event == "push":
        return (ref == "refs/heads/main" and build_result == "success"
                and deploy_scope != "NONE")
    if event != "workflow_dispatch":
        return False
    if ref != "refs/heads/main" or actor != owner:
        return False
    if mode == "deploy":
        return (tests_mode == "DEPLOY" and build_result == "success"
                and deploy_scope != "NONE")
    if mode == "rollback":
        return tests_mode == "ROLLBACK" and build_result in {"success", "skipped"}
    return False


CASES = list(itertools.product(
    ("push", "workflow_dispatch", "pull_request"),
    ("refs/heads/main", "refs/heads/feature"),
    (True, False),
    ("deploy", "rollback", ""),
    ("success", "failure", "cancelled", "skipped"),
    ("success", "failure", "skipped", "cancelled"),
    ("DEPLOY", "ROLLBACK", "BLOCK"),
    ("NONE", "CORE+MINIAPP"),
    ("true", "false"),
))


@pytest.mark.parametrize("event,ref,owner_actor,mode,tests_result,build_result,"
                         "tests_mode,deploy_scope,tests_deploy", CASES)
def test_deploy_admission_matches_the_release_rules(event, ref, owner_actor, mode,
                                                    tests_result, build_result,
                                                    tests_mode, deploy_scope, tests_deploy):
    actual = evaluate(
        DEPLOY["if"], event=event, ref=ref, actor="akhripunov" if owner_actor else "collaborator",
        owner="akhripunov", inputs_mode=mode, tests_result=tests_result,
        build_result=build_result, tests_mode=tests_mode, tests_deploy=tests_deploy,
        deploy_scope=deploy_scope)
    expected = should_deploy(
        event=event, ref=ref, actor="akhripunov" if owner_actor else "collaborator",
        owner="akhripunov", mode=mode, tests_result=tests_result,
        build_result=build_result, tests_mode=tests_mode, deploy_scope=deploy_scope,
        tests_deploy=tests_deploy)
    assert actual is expected, (
        f"deploy admission differs for event={event} ref={ref} owner={owner_actor} "
        f"mode={mode} tests={tests_result} build={build_result} "
        f"tests_mode={tests_mode} scope={deploy_scope}")


def test_rollback_skips_build_but_still_reaches_deploy():
    assert "inputsmode" not in BUILD["if"]
    assert "inputs.mode == 'DEPLOY'" in BUILD["if"] or "mode == 'DEPLOY'" in BUILD["if"]
    assert "skipped" in DEPLOY["if"]
    assert "always()" in DEPLOY["if"]
    assert "cancelled()" in DEPLOY["if"]


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
