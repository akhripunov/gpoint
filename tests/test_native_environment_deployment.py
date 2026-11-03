from pathlib import Path

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
