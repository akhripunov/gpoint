"""Pinned stock Kamal invocation parity, no network or production credentials."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run-pinned-kamal.sh"
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"
PIN = ("ghcr.io/basecamp/kamal:v2.12.0@sha256:"
       "7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625")
COMMON = {"GPOINT_COMMIT_SHA", "GPOINT_APP_SOURCE_SHA", "GPOINT_PRODUCTION_HOST",
          "GPOINT_SSH_KEY", "GPOINT_KNOWN_HOSTS_FILE", "KAMAL_REGISTRY_PASSWORD"}
DEPLOY_ENV = {"GPOINT_KAMAL_EXPECTED_DIGEST", "GPOINT_KAMAL_SERVICE", "KAMAL_ROLES"}
APPLY_ENV = {"GPOINT_WORKER_PROXY_DIGEST", "GPOINT_KAMAL_CORE_API_VERSION",
             "GPOINT_KAMAL_CORE_API_SOURCE_SHA"}


def _invoke(tmp_path, phase, checkout, *, override=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "rollback-core").mkdir()
    (workspace / "rollback-miniapp").mkdir()
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "docker-invocation.json"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "open(os.environ['TEST_DOCKER_TRACE'],'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env.update(HOME=str(home), GITHUB_WORKSPACE=str(workspace), TEST_DOCKER_TRACE=str(trace),
               PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""), KAMAL_IMAGE=override or PIN)
    result = subprocess.run(
        ["bash", str(RUNNER), phase, checkout, "app", "version",
         "--config-file", "config/deploy.yml"],
        env=env, capture_output=True, text=True, check=False,
    )
    args = json.loads(trace.read_text()) if trace.exists() else None
    return result, args, workspace, home


@pytest.mark.parametrize("phase,checkout,extra", [
    ("inventory", ".", set()),
    ("apply", ".", DEPLOY_ENV | APPLY_ENV),
    ("rollback", "rollback-core", DEPLOY_ENV),
    ("rollback", "rollback-miniapp", DEPLOY_ENV),
    ("rollback", ".", DEPLOY_ENV),
])
def test_pinned_kamal_runner_preserves_checkout_and_step_env_contract(tmp_path, phase, checkout, extra):
    result, args, workspace, home = _invoke(tmp_path, phase, checkout)
    assert result.returncode == 0, result.stderr
    assert args[:5] == ["run", "--rm", "--pull=never", "--platform", "linux/amd64"]
    assert args[args.index("--workdir") + 1] == "/workdir"
    assert args[args.index(PIN) + 1:] == [
        "app", "version", "--config-file", "config/deploy.yml",
    ]
    mounts = [args[i + 1] for i, item in enumerate(args[:-1]) if item == "--volume"]
    expected_checkout = workspace if checkout == "." else workspace / checkout
    assert mounts == [
        f"{expected_checkout}:/workdir",
        f"{home}/.ssh:/root/.ssh:ro",
        f"{home}/.ssh:{home}/.ssh:ro",
    ]
    env_names = {args[i + 1] for i, item in enumerate(args[:-1]) if item == "--env"}
    assert env_names == COMMON | extra


@pytest.mark.parametrize("phase,checkout", [
    ("deploy", "."), ("inventory", "rollback-core"), ("apply", "rollback-miniapp"),
    ("rollback", "../escape"), ("rollback", "/tmp"), ("rollback", "rollback-bot"),
])
def test_unrecognized_phase_or_checkout_fails_before_docker(tmp_path, phase, checkout):
    result, args, *_ = _invoke(tmp_path, phase, checkout)
    assert result.returncode != 0
    assert args is None


def test_mismatched_image_pin_blocks_invocation(tmp_path):
    result, args, *_ = _invoke(tmp_path, "apply", ".", override="ghcr.io/basecamp/kamal:latest")
    assert result.returncode != 0
    assert args is None


def test_launcher_and_all_caller_modes_are_explicit_and_bot_remains_unchanged():
    subprocess.run(["bash", "-n", str(RUNNER)], check=True)
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    inv = next(s["run"] for s in steps if s.get("id") == "kamal_inventory")
    app = next(s["run"] for s in steps if s.get("name") == "Deploy exact immutable images with stock Kamal")
    rollback = next(s["run"] for s in steps if s.get("id") == "rollback_apply")
    assert 'run-pinned-kamal.sh" inventory . "$@"' in inv
    assert 'run-pinned-kamal.sh" apply . "$@"' in app
    assert 'run-pinned-kamal.sh" rollback "$checkout" "$@"' in rollback
    assert "docker run --rm" not in inv + app + rollback
    assert "bash scripts/gpoint-bot-bootstrap.sh" in next(
        s["run"] for s in steps
        if s.get("name") == "Restore absent GPoint Bot once from verified installed Core"
    )
    pull = next(s for s in steps if s.get("name") == "Pull and verify pinned Kamal CLI image")
    assert pull["env"]["KAMAL_IMAGE"] == PIN
    assert PIN in RUNNER.read_text()
