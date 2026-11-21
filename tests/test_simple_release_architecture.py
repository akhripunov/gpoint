from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
def _workflow_source() -> str:
    path = ".github/workflows/gpoint-native-release.yml"
    return (ROOT / path).read_text()


def _documented_metric(documentation: str, label: str) -> tuple[int, int]:
    row = next(line for line in documentation.splitlines() if line.startswith(f"| {label} |"))
    columns = [column.strip().replace(",", "") for column in row.strip("|").split("|")]
    return int(columns[1]), int(columns[2])


def _ordinary_core_deploy_steps() -> int:
    workflow = yaml.safe_load(_workflow_source())
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


def _tracked_paths() -> set[str]:
    return set(subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines())


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
    assert 'value["build_components"] = ["CORE", "EROS", "MINIAPP"]' in build_scope["run"]
    tests = workflow["jobs"]["tests"]
    eros_e2e = next(step for step in tests["steps"] if step.get("id") == "eros_e2e")
    assert "github.event_name == 'pull_request'" in eros_e2e["if"]
    assert "contains(github.event.pull_request.labels.*.name, 'me-62-full-build')" in eros_e2e["if"]
    assert "eros_e2e" not in tests["outputs"]
    assert "tests" in build["needs"]
    assert "needs.tests.result == 'success'" in build["if"]
    path_filter = next(step for step in tests["steps"] if step.get("id") == "filter")
    assert path_filter["with"]["base"] == "${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.before }}"
    assert path_filter["with"]["token"] == ""
    assert "pull-requests" not in tests["permissions"]
    assert not any(step.get("name") == "Require exact-candidate Eros test evidence" for step in build["steps"])
    assert "steps.infrastructure_baseline.outputs.eros_engine" in eros_e2e["if"]
    assert "steps.dispatch_build_scope.outputs.eros_engine" in eros_e2e["if"]
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


def test_production_release_controller_and_unique_image_build_counts_are_explicit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    build_steps = [
        step for step in workflow["jobs"]["build"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert len(build_steps) == 3
    assert len([job for job in workflow["jobs"].values() if job.get("environment", {}).get("name") == "production"]) == 1


def test_deploy_uses_one_authoritative_live_inventory_not_redundant_kamal_text_views():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    inventory = next(step for step in steps if step.get("id") == "kamal_inventory")
    topology = next(step for step in steps if step.get("id") == "topology")

    assert "kamal app version" in inventory["run"]
    assert "kamal app details" not in inventory["run"]
    assert "kamal app containers" not in inventory["run"]
    assert "verify-kamal-production-baseline.py" in inventory["run"]
    assert "RepoDigests" in (ROOT / "scripts/verify-kamal-production-baseline.py").read_text()


def test_release_architecture_invariants_match_documented_state():
    documentation = (ROOT / "docs/release/automation.md").read_text()
    current_workflow = _workflow_source()
    current = {
        "Production release controllers": sum(
            path.endswith("gpoint-native-release.yml") for path in _tracked_paths()
        ),
        "Additional Layer7 staging deployment workflows": sum(
            path.endswith("gpoint-layer7-staging.yml") for path in _tracked_paths()
        ),
        "Maximum distinct production images in one full run": sum(
            step.get("uses", "").startswith("docker/build-push-action@")
            for step in yaml.safe_load(current_workflow)["jobs"]["build"]["steps"]
        ),
        "Custom prepared-run/production-manifest state formats": sum(
            path in _tracked_paths()
            for path in (
                "scripts/verify_native_prepared_run.py",
                "scripts/write-kamal-production-manifest.py",
            )
        ),
    }
    assert current["Production release controllers"] == 1
    assert current["Additional Layer7 staging deployment workflows"] == 0
    assert current["Maximum distinct production images in one full run"] == 3
    assert current["Custom prepared-run/production-manifest state formats"] == 1
    for label, value in current.items():
        assert _documented_metric(documentation, label)[1] == value

    assert (ROOT / "scripts/write-kamal-production-manifest.py").is_file()
    assert not (ROOT / "scripts/verify_native_prepared_run.py").exists()


def test_retired_gsales_is_not_an_active_release_gate():
    workflow = _workflow_source()
    documentation = (ROOT / "docs/release/automation.md").read_text()

    assert "tests/test_gsales_" not in workflow
    assert "services/gsales/tests/" not in workflow
    assert "tests/integration/gpoint-chat-schema-docker.py" in workflow
    assert "GSales не входит в активную архитектуру релизов GPoint" in documentation
    assert "Исторический контракт GSales Worker ниже не является release gate" in documentation
