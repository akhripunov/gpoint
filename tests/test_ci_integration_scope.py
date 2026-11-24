"""Keep expensive CI integration scope narrower without weakening release gates."""

from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_release_intent as intent

WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"
REVIEWED_CI_EXEMPTIONS = {
    ".github/actionlint.yaml",
    ".github/workflows/sync-agents-to-linear.yml",
    "scripts/classify-release-prepush.py",
    "scripts/prepush.py",
    "scripts/verify_native_eros_image.py",
    "scripts/check-kamal-configs.sh",
    "scripts/verify-eros-runtime-labels.py",
    "scripts/verify_native_rollback_evidence.py",
}


def _selected(paths):
    return (
        intent.paths_match_filter(paths, "eros_engine"),
        intent.paths_match_filter(paths, "miniapp")
        or intent.paths_match_filter(paths, "database")
        or intent.paths_match_filter(paths, "integration_infrastructure"),
        intent.paths_match_filter(paths, "database")
        or intent.paths_match_filter(paths, "integration_infrastructure"),
    )


def test_integration_filter_is_infrastructure_minus_only_reviewed_safe_paths():
    canonical = intent.NATIVE_RELEASE_FILTERS["infrastructure"]
    selected = intent.NATIVE_RELEASE_FILTERS["integration_infrastructure"]
    assert [p for p in selected if not p.startswith("!")] == canonical
    assert {p[1:] for p in selected if p.startswith("!")} == REVIEWED_CI_EXEMPTIONS
    assert all(intent._known_non_runtime_path(p) for p in REVIEWED_CI_EXEMPTIONS)
    assert all(intent.paths_match_filter({p}, "infrastructure")
               for p in REVIEWED_CI_EXEMPTIONS)
    assert all(not intent.paths_match_filter({p}, "integration_infrastructure")
               for p in REVIEWED_CI_EXEMPTIONS)


@pytest.mark.parametrize("paths,expected", [
    ({".github/actionlint.yaml"}, (False, False, False)),
    ({".github/workflows/sync-agents-to-linear.yml"}, (False, False, False)),
    ({"scripts/classify-release-prepush.py"}, (False, False, False)),
    ({"scripts/prepush.py"}, (False, False, False)),
    ({"scripts/verify_native_rollback_evidence.py"}, (False, False, False)),
    ({"scripts/verify_native_eros_image.py"}, (False, False, False)),
    ({"scripts/verify-eros-runtime-labels.py"}, (False, False, False)),
    ({"scripts/check-kamal-configs.sh"}, (False, False, False)),
    ({"README.md"}, (False, False, False)),
    ({"apps/miniapp/src/main.tsx"}, (False, True, False)),
    ({"services/eros-engine/upstream.sha"}, (True, False, False)),
    ({"packages/db/prisma/schema.prisma"}, (False, True, True)),
    ({"config/deploy.eros.yml"}, (True, True, True)),
    ({".github/filters/native-release.json"}, (False, True, True)),
    ({".github/workflows/gpoint-operations.yml"}, (False, True, True)),
    ({"scripts/check-kamal-configs.sh", "apps/miniapp/src/page.tsx"}, (False, True, False)),
    ({".github/workflows/gpoint-release.yml"}, (False, True, True)),
    ({"scripts/unreviewed-new-helper.py"}, (False, True, True)),
    ({".github/actionlint.yaml", "packages/db/prisma/schema.prisma"}, (False, True, True)),
    ({"scripts/prepush.py", "scripts/unreviewed-new-helper.py"}, (False, True, True)),
])
def test_ci_integration_matrix(paths, expected):
    assert _selected(paths) == expected


def test_expensive_steps_share_reviewed_scope_and_fail_closed_guards():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    tests = workflow["jobs"]["tests"]
    steps = {step.get("name"): step for step in tests["steps"]}
    assert tests["strategy"]["matrix"]["suite"] == ["workspace", "python"]
    filter_step = next(step for step in tests["steps"]
                       if step.get("id") == "filter")
    assert filter_step["with"]["predicate-quantifier"] == "some-with-excludes"
    assert filter_step["with"]["filters"] == ".github/filters/native-release.json"
    for name in (
        "Install Playwright Chromium",
        "Run Mini App end-to-end tests",
        "Detect pending schema dependencies before Build",
        "Run isolated PostgreSQL 16 migration and integration suites",
    ):
        condition = steps[name]["if"]
        assert "steps.filter.outputs.integration_infrastructure == 'true'" in condition
        assert "steps.filter.outputs.infrastructure" not in condition
        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "github.event.before == '0000000000000000000000000000000000000000'" in condition
    assert steps["Validate pinned Kamal configs and shared proxy routing"]["if"] == ("matrix.suite == 'workspace'")
    assert not any(step.get("id") in {"eros_test_image", "eros_e2e", "eros_scope"} for step in tests["steps"])
    e2e = next(step for step in workflow["jobs"]["deploy"]["steps"] if step.get("id") == "eros_e2e")
    assert "steps.component_plan.outputs.deploy_eros == 'true'" in e2e["if"]
    assert steps["Run workflow, Native First and Kamal boundary tests"]["if"] == (
        "matrix.suite == 'python'"
    )
