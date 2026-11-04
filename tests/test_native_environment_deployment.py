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


def test_resolves_only_exact_in_progress_deployment_for_current_run_attempt():
    result = resolver.resolve_native_deployment_id(
        [deployment()], {101: [status()]}, candidate_sha=SHA, repository=REPOSITORY,
        run_id=RUN, run_attempt=ATTEMPT)
    assert result == 101


def test_main_resolves_exact_active_deploy_job_url_for_this_attempt(monkeypatch, capsys):
    job_url = f"https://github.com/{REPOSITORY}/actions/runs/{RUN}/job/126479089691"
    monkeypatch.setattr(sys, "argv", [
        "resolve_native_environment_deployment.py", "--sha", SHA,
        "--repository", REPOSITORY, "--run-id", RUN, "--run-attempt", ATTEMPT,
    ])

    def gh_json(*args):
        route = args[-1]
        if route.startswith(f"repos/{REPOSITORY}/deployments?"):
            return [deployment() | {"production_environment": False}]
        if route == f"repos/{REPOSITORY}/deployments/101/statuses?per_page=100":
            return [status(url=job_url)]
        raise AssertionError(f"unexpected GitHub API route: {route}")

    monkeypatch.setattr(resolver, "_gh_json", gh_json)
    jobs_route = f"repos/{REPOSITORY}/actions/runs/{RUN}/attempts/{ATTEMPT}/jobs?per_page=100"
    job = {"id": 126479089691, "name": "Kamal production Deploy and Verify",
           "status": "in_progress", "html_url": job_url}
    monkeypatch.setattr(resolver, "_gh_run_jobs",
                        lambda route: [job] if route == jobs_route else [], raising=False)

    assert resolver.main() == 0
    assert capsys.readouterr().out.strip() == "deployment_id=101"
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    deploy_permissions = workflow.split("  deploy:\n", 1)[1].split("    env:\n", 1)[0]
    assert "      actions: read" in deploy_permissions


def test_gh_run_jobs_unwraps_github_object_pages(monkeypatch):
    job = {"id": 126479089691, "name": "Kamal production Deploy and Verify",
           "status": "in_progress"}
    result = SimpleNamespace(returncode=0, stdout=json.dumps([{"total_count": 1, "jobs": [job]}]),
                             stderr="")
    monkeypatch.setattr(resolver.subprocess, "run", lambda *_args, **_kwargs: result)

    assert resolver._gh_run_jobs("repos/example/repo/actions/runs/1/attempts/1/jobs") == [job]


def test_resolves_protected_production_environment_when_api_flag_is_false():
    result = resolver.resolve_native_deployment_id(
        [deployment() | {"production_environment": False}], {101: [status()]},
        candidate_sha=SHA, repository=REPOSITORY, run_id=RUN,
        run_attempt=ATTEMPT)
    assert result == 101


def test_resolves_exact_sha_even_when_native_deployment_ref_is_main_branch():
    result = resolver.resolve_native_deployment_id(
        [deployment() | {"ref": "refs/heads/main"}], {101: [status()]},
        candidate_sha=SHA, repository=REPOSITORY, run_id=RUN, run_attempt=ATTEMPT)
    assert result == 101


@pytest.mark.parametrize("bad", [
    deployment(101) | {"sha": "b" * 40},
    deployment(101) | {"environment": "staging"},
    deployment(101) | {"transient_environment": True},
])
def test_rejects_wrong_native_deployment_identity(bad):
    with pytest.raises(ValueError):
        resolver.resolve_native_deployment_id(
            [bad], {101: [status()]}, candidate_sha=SHA, repository=REPOSITORY,
            run_id=RUN, run_attempt=ATTEMPT)


def test_rejects_different_run_or_non_in_progress_status():
    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve_native_deployment_id(
            [deployment()], {101: [status(url=f"https://github.com/{REPOSITORY}/actions/runs/999")]},
            candidate_sha=SHA, repository=REPOSITORY, run_id=RUN, run_attempt=ATTEMPT)
    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve_native_deployment_id(
            [deployment()], {101: [status(state="success")]}, candidate_sha=SHA,
            repository=REPOSITORY, run_id=RUN, run_attempt=ATTEMPT)


def test_rejects_ambiguous_current_run_deployments():
    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve_native_deployment_id(
            [deployment(101), deployment(102)], {101: [status()], 102: [status()]},
            candidate_sha=SHA, repository=REPOSITORY, run_id=RUN, run_attempt=ATTEMPT)
