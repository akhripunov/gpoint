from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_BASE = "3d680fa153fd6aebf786455d6f36d3b1a67cc78f"
FIXED_CONTROLLER_PATHS = (
    ".github/workflows/gpoint-native-release.yml",
    "scripts/native_change_surface.py",
    "scripts/native_release_intent.py",
    "scripts/resolve_native_build_scope.py",
    "scripts/verify_native_rollback_evidence.py",
    "scripts/write-kamal-production-manifest.py",
)
SHARED_RELEASE_HELPERS = (
    "scripts/native_release_intent.py",
    "scripts/resolve_native_environment_deployment.py",
    "scripts/verify_native_image_attestation.py",
)


def _source_lines(path: str, revision: str | None = None) -> int:
    if revision:
        result = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            return 0
        return len(result.stdout.splitlines())
    target = ROOT / path
    return len(target.read_text().splitlines()) if target.is_file() else 0


def _workflow_source(revision: str | None = None) -> str:
    path = ".github/workflows/gpoint-native-release.yml"
    if revision:
        return subprocess.check_output(
            ["git", "show", f"{revision}:{path}"], cwd=ROOT, text=True
        )
    return (ROOT / path).read_text()


def _workflow_helpers(source: str) -> set[str]:
    return set(
        re.findall(
            r"(?<![A-Za-z0-9_])scripts/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js)",
            source,
        )
    )


def _documented_metric(documentation: str, label: str) -> tuple[int, int]:
    row = next(line for line in documentation.splitlines() if line.startswith(f"| {label} |"))
    columns = [column.strip().replace(",", "") for column in row.strip("|").split("|")]
    return int(columns[1]), int(columns[2])


def _ordinary_core_deploy_steps(revision: str | None = None) -> int:
    workflow = yaml.safe_load(_workflow_source(revision))
    count = 0
    for step in workflow["jobs"]["deploy"]["steps"]:
        condition = step.get("if", "")
        if "inputs.mode == 'rollback'" in condition:
            continue
        if "deploy_eros == 'true'" in condition or "rollback_eros == true" in condition:
            continue
        if "steps.verify.outcome != 'success'" in condition:
            continue
        if "inputs.mode != 'rollback'" in condition:
            count += 1
            continue
        if "needs.tests.outputs.mode == 'DEPLOY'" in condition:
            count += 1
            continue
        if "needs.tests.outputs.mode != 'LAYER7_API'" in condition:
            count += 1
            continue
        if "always()" in condition:
            count += 1
            continue
        if not condition:
            count += 1
    return count


def _tracked_paths(revision: str | None = None) -> set[str]:
    command = ["git", "ls-files"] if revision is None else ["git", "ls-tree", "-r", "--name-only", revision]
    return set(subprocess.check_output(command, cwd=ROOT, text=True).splitlines())


def test_native_workflow_references_only_existing_release_scripts():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    paths = set(re.findall(r"(?<![A-Za-z0-9_])scripts/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js)", workflow))
    assert paths
    missing = sorted(path for path in paths if not (ROOT / path).is_file())
    assert not missing


def test_layer7_is_not_configured_as_a_staging_release_target():
    retired_paths = (
        ".github/workflows/gpoint-layer7-staging.yml",
        ".github/actions/layer7-deploy/action.yml",
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
        "services/eros-engine/eros-layer7-staging-model-config.toml",
    )
    assert all(not (ROOT / path).exists() for path in retired_paths)


def test_retired_eros_pilot_compose_deployer_is_not_a_release_tool():
    package = __import__("json").loads((ROOT / "package.json").read_text())
    assert not (ROOT / "scripts/eros-pilot-deployment.mjs").exists()
    assert not (ROOT / "scripts/eros-pilot-deployment.test.mjs").exists()
    assert "eros-pilot-deployment" not in package["scripts"]["test:release"]


def test_pr_candidate_runs_build_without_production_deploy():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    )
    build = workflow["jobs"]["build"]
    deploy = workflow["jobs"]["deploy"]

    assert "github.event_name == 'pull_request'" in build["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in build["if"]
    assert "github.event.pull_request.author_association" in build["if"]
    assert 'fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\')' in build["if"]
    assert build["permissions"]["packages"] == "write"
    build_scope = next(step for step in build["steps"] if step.get("id") == "build_scope")
    assert build_scope["env"]["FULL_BUILD_VALIDATION"] == "${{ github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'me-62-full-build') }}"
    assert 'value["build_components"] = ["AGATA", "CORE", "EROS", "MINIAPP"]' in build_scope["run"]
    tests = workflow["jobs"]["tests"]
    eros_e2e = next(step for step in tests["steps"] if step.get("id") == "eros_e2e")
    assert "github.event_name == 'pull_request'" in eros_e2e["if"]
    assert "contains(github.event.pull_request.labels.*.name, 'me-62-full-build')" in eros_e2e["if"]
    assert "eros_e2e" not in tests["outputs"]
    eros_gate = next(step for step in build["steps"] if step.get("name") == "Require exact-candidate Eros test evidence")
    assert "/attempts/$GITHUB_RUN_ATTEMPT/jobs" in eros_gate["run"]
    assert ".head_sha" in eros_gate["run"]
    assert "runs/$GITHUB_RUN_ID" in eros_gate["run"]
    assert eros_gate["env"]["EXPECTED_RUN_HEAD_SHA"].endswith("github.sha }}")
    assert "github.event_name == 'pull_request'" not in deploy["if"]
    assert "needs.tests.outputs.mode == 'DEPLOY'" in deploy["if"]


def test_every_workflow_helper_has_a_replacement_and_retention_audit():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    documentation = (ROOT / "docs/release/automation.md").read_text()
    start = "<!-- ME62_HELPER_AUDIT_START -->"
    end = "<!-- ME62_HELPER_AUDIT_END -->"
    assert start in documentation and end in documentation
    audit = documentation.split(start, 1)[1].split(end, 1)[0]
    referenced = set(
        re.findall(
            r"(?<![A-Za-z0-9_])scripts/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js)",
            workflow,
        )
    )
    mapped = set(re.findall(r"`(scripts/[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js))`", audit))
    assert referenced <= mapped, f"helpers missing architecture disposition: {sorted(referenced - mapped)}"
    for line in audit.splitlines():
        if line.startswith("|") and "scripts/" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert len(cells) >= 3
            assert cells[1] and cells[2], f"incomplete native replacement rationale: {line}"


def test_fixed_six_path_reduction_metrics_are_recomputed_from_source():
    documentation = (ROOT / "docs/release/automation.md").read_text()
    expected = {
        "Main controller + five control adapters (six paths)": (
            sum(_source_lines(path, RELEASE_BASE) for path in FIXED_CONTROLLER_PATHS),
            sum(_source_lines(path) for path in FIXED_CONTROLLER_PATHS),
        ),
    }
    for label, (baseline, current) in expected.items():
        row = next(line for line in documentation.splitlines() if line.startswith(f"| {label} |"))
        columns = [column.strip().replace(",", "") for column in row.strip("|").split("|")]
        assert int(columns[1]) == baseline
        assert int(columns[2]) == current


def test_workflow_and_all_direct_helper_metrics_are_recomputed_from_git():
    documentation = (ROOT / "docs/release/automation.md").read_text()
    metrics = {}
    for revision in (RELEASE_BASE, None):
        workflow = _workflow_source(revision)
        helpers = _workflow_helpers(workflow)
        total = len(workflow.splitlines()) + sum(
            _source_lines(path, revision) for path in helpers
        )
        metrics[revision or "current"] = (len(workflow.splitlines()), len(helpers), total)

    for label, index in (
        ("Workflow LOC", 0),
        ("Referenced helper files", 1),
        ("Combined workflow + referenced scripts LOC", 2),
    ):
        row = next(line for line in documentation.splitlines() if line.startswith(f"| {label} |"))
        columns = [column.strip().replace(",", "") for column in row.strip("|").split("|")]
        assert int(columns[1]) == metrics[RELEASE_BASE][index]
        assert int(columns[2]) == metrics["current"][index]


def test_production_release_controller_and_unique_image_build_counts_are_explicit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    build_steps = [
        step for step in workflow["jobs"]["build"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert len(build_steps) == 4
    assert len([job for job in workflow["jobs"].values() if job.get("environment", {}).get("name") == "production"]) == 1


def test_all_seven_complexity_measures_keep_the_fixed_source_inventory():
    documentation = (ROOT / "docs/release/automation.md").read_text()
    baseline_workflow = _workflow_source(RELEASE_BASE)
    current_workflow = _workflow_source()
    baseline_helpers = _workflow_helpers(baseline_workflow)
    current_helpers = _workflow_helpers(current_workflow)
    expected = {
        "Native production workflow LOC": (
            len(baseline_workflow.splitlines()), len(current_workflow.splitlines())
        ),
        "Release-specific referenced-helper LOC": (
            sum(_source_lines(path, RELEASE_BASE) for path in baseline_helpers)
            - sum(_source_lines(path, RELEASE_BASE) for path in SHARED_RELEASE_HELPERS),
            sum(_source_lines(path) for path in current_helpers)
            - sum(_source_lines(path) for path in SHARED_RELEASE_HELPERS),
        ),
        "Shared referenced-helper LOC (unique source files)": (
            sum(_source_lines(path, RELEASE_BASE) for path in SHARED_RELEASE_HELPERS),
            sum(_source_lines(path) for path in SHARED_RELEASE_HELPERS),
        ),
        "Production release controllers": (
            sum(path.endswith("gpoint-native-release.yml") for path in _tracked_paths(RELEASE_BASE)),
            sum(path.endswith("gpoint-native-release.yml") for path in _tracked_paths()),
        ),
        "Additional Layer7 staging deployment workflows": (
            sum(path.endswith("gpoint-layer7-staging.yml") for path in _tracked_paths(RELEASE_BASE)),
            sum(path.endswith("gpoint-layer7-staging.yml") for path in _tracked_paths()),
        ),
        "Maximum distinct production images in one full run": (
            sum(step.get("uses", "").startswith("docker/build-push-action@") for step in yaml.safe_load(baseline_workflow)["jobs"]["build"]["steps"]),
            sum(step.get("uses", "").startswith("docker/build-push-action@") for step in yaml.safe_load(current_workflow)["jobs"]["build"]["steps"]),
        ),
        "Custom prepared-run/production-manifest state formats": (
            sum(path in _tracked_paths(RELEASE_BASE) for path in ("scripts/verify_native_prepared_run.py", "scripts/write-kamal-production-manifest.py")),
            sum(path in _tracked_paths() for path in ("scripts/verify_native_prepared_run.py", "scripts/write-kamal-production-manifest.py")),
        ),
        "Core-only ordinary Deploy steps (no migration/Eros/rollback)": (
            _ordinary_core_deploy_steps(RELEASE_BASE), _ordinary_core_deploy_steps()
        ),
    }
    for label, values in expected.items():
        assert _documented_metric(documentation, label) == values

    assert (ROOT / "scripts/write-kamal-production-manifest.py").is_file()
    assert not (ROOT / "scripts/verify_native_prepared_run.py").exists()
