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


@pytest.mark.parametrize("reported_job_status", ["in_progress", "queued", "completed"])
def test_main_resolves_exact_active_deploy_job_url_for_this_attempt(monkeypatch, capsys, reported_job_status):
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
           "status": reported_job_status, "html_url": job_url}
    monkeypatch.setattr(resolver, "_gh_run_jobs",
                        lambda route: [job] if route == jobs_route else [], raising=False)

    assert resolver.main() == 0
    assert capsys.readouterr().out.strip() == "deployment_id=101"
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text()
    deploy_permissions = workflow.split("  deploy:\n", 1)[1].split("    env:\n", 1)[0]
    assert "      actions: read" in deploy_permissions


def test_deploy_job_url_rejects_wrong_identity_even_if_status_is_in_progress():
    expected = f"https://github.com/{REPOSITORY}/actions/runs/{RUN}/job/126479089691"
    wrong = f"https://github.com/{REPOSITORY}/actions/runs/999/job/126479089691"
    jobs = [{"id": 126479089691, "name": "Kamal production Deploy and Verify",
             "status": "in_progress", "html_url": wrong}]
    with pytest.raises(ValueError, match="exactly one"):
        resolver._current_attempt_deploy_job_urls(jobs, repository=REPOSITORY, run_id=RUN)
    jobs[0]["html_url"] = expected
    assert resolver._current_attempt_deploy_job_urls(jobs, repository=REPOSITORY, run_id=RUN) == {expected}
    jobs.append({"id": 126479089692, "name": "Kamal production Deploy and Verify",
                 "status": "queued",
                 "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN}/job/126479089692"})
    with pytest.raises(ValueError, match="exactly one"):
        resolver._current_attempt_deploy_job_urls(jobs, repository=REPOSITORY, run_id=RUN)


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
