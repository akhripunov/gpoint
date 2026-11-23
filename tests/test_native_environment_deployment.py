from pathlib import Path
import json
import sys
from types import SimpleNamespace

import importlib.util
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "resolve_native_environment_deployment",
    ROOT / "scripts/resolve_native_environment_deployment.py",
)
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)

SHA = "a" * 40
RUN = "41234"
ATTEMPT = "2"
REPOSITORY = "akhripunov/gpoint"


def deployment(identifier=101):
    return {"id": identifier, "sha": SHA, "ref": SHA, "environment": "production",
            "task": "deploy", "production_environment": True, "transient_environment": False}


def status(*, state="in_progress", url=None, time="2026-11-03T11:00:00Z"):
    return {"state": state, "environment": "production",
            "log_url": url or f"https://github.com/{REPOSITORY}/actions/runs/{RUN}/attempts/{ATTEMPT}",
            "created_at": time}


def test_main_outputs_verified_successful_native_and_installed_eros_source_shas(monkeypatch, capsys):
    eros_sha = "d" * 40
    deployment_row = deployment() | {"created_at": "2026-11-21T10:00:00Z"}
    status_row = status(
        state="success", url=f"https://github.com/{REPOSITORY}/actions/runs/100",
    ) | {"environment_url": f"https://gpoint.one/#gpoint-eros-source-sha={eros_sha}"}
    monkeypatch.setattr(sys, "argv", [
        "resolve_native_environment_deployment.py", "--repository", REPOSITORY,
        "--latest-successful-native-sha",
    ])

    def gh_json(*args):
        route = args[-1]
        if route == f"repos/{REPOSITORY}/deployments?environment=production&task=deploy&per_page=100":
            return [deployment_row]
        if route == f"repos/{REPOSITORY}/deployments/101/statuses?per_page=100":
            return [status_row]
        raise AssertionError(f"unexpected GitHub API route: {route}")

    monkeypatch.setattr(resolver, "_gh_json", gh_json)
    monkeypatch.setattr(resolver.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        stdout=json.dumps({
            "head_sha": SHA, "conclusion": "success",
            "path": ".github/workflows/gpoint-release.yml@refs/heads/main",
        }),
    ))

    assert resolver.main() == 0
    assert capsys.readouterr().out.splitlines() == [f"sha={SHA}", f"eros_source_sha={eros_sha}"]



def test_deployment_produced_by_direct_api_not_status_polling():
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "deployment: ${{ inputs.mode == 'rollback' }}" in deploy
    assert "name: Create exact GitHub production Deployment" in deploy
    assert 'required_contexts: []' in deploy
    assert 'auto_merge: false' in deploy
    assert 'deployment_id="$(jq -er --arg sha "$CANDIDATE_SHA"' in deploy
    assert 'echo "id=$deployment_id" >> "$GITHUB_OUTPUT"' in deploy
    assert "scripts/resolve_native_environment_deployment.py \\" not in deploy
    assert not hasattr(resolver, "resolve_native_deployment_id")
    assert not hasattr(resolver, "_current_attempt_deploy_job_urls")


def test_direct_deployment_statuses_preserve_rollback_and_baseline_evidence():
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "name: Publish verified GitHub production Deployment" in deploy
    assert "steps.verify.outcome == 'success'" in deploy
    assert "steps.verify.outputs.manifest_sha" in deploy
    assert "steps.verify.outputs.environment_url" in deploy
    assert '"Kamal success SHA "' in deploy
    assert '" manifest "' in deploy
    assert "name: Publish unsuccessful GitHub production Deployment" in deploy
    assert "steps.deployment_success.outcome != 'success'" in deploy
    assert "steps.deployment.outputs.id" in deploy


def test_baseline_command_rejects_current_deployment_resolution(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "resolve_native_environment_deployment.py", "--repository", REPOSITORY,
    ])
    assert resolver.main() == 1
    assert "only verified successful" in capsys.readouterr().err


def test_infrastructure_baseline_uses_latest_successful_native_deploy_sha_only():
    older_sha, newer_sha, failed_sha = "a" * 40, "b" * 40, "c" * 40
    deployments = [
        deployment(101) | {"sha": older_sha, "created_at": "2026-11-20T10:00:00Z"},
        deployment(102) | {"sha": newer_sha, "created_at": "2026-11-21T10:00:00Z"},
        deployment(103) | {"sha": failed_sha, "created_at": "2026-11-21T11:00:00Z"},
    ]
    statuses = {
        101: [status(state="success", url=f"https://github.com/{REPOSITORY}/actions/runs/100")],
        102: [status(state="success", url=f"https://github.com/{REPOSITORY}/actions/runs/101") | {
            "environment_url": "https://gpoint.one/#gpoint-eros-source-sha=" + "d" * 40,
        }],
        103: [status(state="failure", url=f"https://github.com/{REPOSITORY}/actions/runs/102")],
    }
    runs = {
        100: {"head_sha": older_sha, "conclusion": "success",
              "path": ".github/workflows/gpoint-native-release.yml@refs/heads/main"},
        101: {"head_sha": newer_sha, "conclusion": "success",
              "path": ".github/workflows/gpoint-release.yml@refs/heads/main"},
        102: {"head_sha": failed_sha, "conclusion": "failure",
              "path": ".github/workflows/gpoint-release.yml@refs/heads/main"},
    }
    assert resolver.latest_successful_native_source_sha(
        deployments, statuses, runs, repository=REPOSITORY) == newer_sha
    assert resolver.latest_successful_native_eros_source_sha(
        deployments, statuses, runs, repository=REPOSITORY) == "d" * 40


def test_infrastructure_baseline_ignores_recovery_workflow_deployments():
    row = deployment() | {"created_at": "2026-11-21T10:00:00Z"}
    statuses = {101: [status(state="success", url=f"https://github.com/{REPOSITORY}/actions/runs/100")]}
    runs = {100: {"head_sha": SHA, "conclusion": "success",
                  "path": ".github/workflows/miniapp-recovery.yml@refs/heads/main"}}
    with pytest.raises(ValueError, match="no successful verified Native"):
        resolver.latest_successful_native_source_sha([row], statuses, runs, repository=REPOSITORY)
