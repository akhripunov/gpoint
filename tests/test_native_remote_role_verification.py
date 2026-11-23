import json
import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"
VERIFIER = ROOT / "scripts/verify-kamal-role.sh"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step_script(job, name):
    return next(step["run"] for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)


def _fake_docker(tmp_path, names):
    binary = tmp_path / "docker"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_TRACE"], "a") as trace:
    trace.write(json.dumps(args) + "\n")

if args[0] == "ps":
    print(os.environ.get("FAKE_DOCKER_NAMES", ""))
elif args[0] == "inspect":
    expected = '{{.State.Running}}|{{index .Config.Labels "service"}}|{{index .Config.Labels "role"}}|{{.Image}}'
    if len(args) != 4 or args[1] != "--format" or args[2] != expected:
        raise SystemExit("unexpected docker inspect format")
    print("|".join([
        os.environ.get("FAKE_DOCKER_RUNNING", "true"),
        os.environ.get("FAKE_DOCKER_SERVICE", "gpoint-core"),
        os.environ.get("FAKE_DOCKER_ROLE", "worker"),
        "sha256:" + "c" * 64,
    ]))
elif args[:2] == ["image", "inspect"]:
    valid = {
        '{{json .RepoDigests}}|{{index .Config.Labels "org.opencontainers.image.revision"}}',
        '{{json .RepoDigests}}|{{index .Config.Labels "com.gpoint.source.revision"}}',
    }
    if len(args) != 5 or args[2] != "--format" or args[3] not in valid:
        raise SystemExit("unexpected docker image inspect format")
    repository = os.environ.get("FAKE_DOCKER_REPOSITORY", "ghcr.io/akhripunov/gpoint-runtime")
    print(json.dumps([repository + "@sha256:" + "d" * 64]) + "|" + os.environ["FAKE_DOCKER_REVISION"])
else:
    raise SystemExit("unexpected docker command")
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_DOCKER_NAMES"] = "\n".join(names)
    env["FAKE_DOCKER_REVISION"] = "a" * 40
    env["FAKE_DOCKER_TRACE"] = str(tmp_path / "docker-trace.jsonl")
    return env


def _run_verifier(tmp_path, names, expected_sha="a" * 40):
    return subprocess.run(
        ["bash", str(VERIFIER), "gpoint-core-worker", "a" * 40 + "-1-1", expected_sha],
        env=_fake_docker(tmp_path, names),
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_role_inspection_runs_over_the_pinned_vps_ssh_connection():
    script = _step_script("deploy", "Deploy exact immutable images with stock Kamal")
    assert 'ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$@"' in script
    assert '< "$GITHUB_WORKSPACE/scripts/verify-kamal-role.sh"' in script
    assert "verify_single_running_role gpoint-core-worker" in script
    assert "verify_single_running_role gpoint-core-bot" in script
    assert "gpoint-core-worker-proxy" in script
    assert "gpoint-core-worker-proxy" in _step_script("deploy", "Verify public routes and exact running services")


def test_rollback_role_inspection_runs_over_the_pinned_vps_ssh_connection():
    script = _step_script("deploy", "Roll back consumers and then APIs with stock Kamal")
    assert 'ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$1" "$2" "$3"' in script
    assert '< "$GITHUB_WORKSPACE/scripts/verify-kamal-role.sh"' in script
    assert "verify_role gpoint-core-worker" in script


def test_rollback_passes_target_version_as_kamal_positional_argument():
    script = _step_script("deploy", "Roll back consumers and then APIs with stock Kamal")
    calls = [line.strip() for line in script.splitlines()
             if "kamal rollback" in line and ' rollback "$' in line]
    assert len(calls) == 4
    assert all('rollback "$' in line for line in calls)
    assert any('"$CORE_VERSION"' in line for line in calls)
    assert any('"$MINIAPP_VERSION"' in line for line in calls)
    assert all("rollback --version" not in line for line in calls)


def test_remote_verifier_accepts_one_exact_running_role_and_revision(tmp_path):
    result = _run_verifier(tmp_path, [
        "gpoint-core-worker-" + "a" * 40 + "-1-1", "gpoint-core-worker-proxy",
    ])
    assert result.returncode == 0, result.stderr


def test_remote_verifier_rejects_duplicate_running_roles(tmp_path):
    result = _run_verifier(
        tmp_path,
        ["gpoint-core-worker-" + "a" * 40 + "-1-1", "gpoint-core-worker-" + "b" * 40 + "-1-2"],
    )
    assert result.returncode != 0


def test_remote_verifier_rejects_wrong_source_revision(tmp_path):
    env = _fake_docker(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
    env["FAKE_DOCKER_REVISION"] = "b" * 40
    result = subprocess.run(
        ["bash", str(VERIFIER), "gpoint-core-worker", "a" * 40 + "-1-1", "a" * 40],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_remote_verifier_rejects_missing_exact_worker_even_with_accessory(tmp_path):
    result = _run_verifier(tmp_path, ["gpoint-core-worker-proxy"])
    assert result.returncode != 0


def test_remote_verifier_rejects_wrong_role_service_label(tmp_path):
    env = _fake_docker(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
    env["FAKE_DOCKER_SERVICE"] = "gpoint-core-worker-proxy"
    result = subprocess.run(
        ["bash", str(VERIFIER), "gpoint-core-worker", "a" * 40 + "-1-1", "a" * 40],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


def test_remote_verifier_rejects_wrong_kamal_version(tmp_path):
    result = _run_verifier(tmp_path, [
        "gpoint-core-worker-" + "a" * 40 + "-1-1",
        "gpoint-core-worker-" + "b" * 40 + "-1-2",
    ])
    assert result.returncode != 0


def test_verifier_uses_one_container_and_one_image_inspect(tmp_path):
    result = _run_verifier(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "docker-trace.jsonl").read_text().splitlines()]
    assert [call[:2] for call in calls] == [
        ["ps", "--no-trunc"], ["inspect", "--format"], ["image", "inspect"],
    ]


def test_remote_verifier_rejects_stopped_role_even_if_listed(tmp_path):
    env = _fake_docker(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
    env["FAKE_DOCKER_RUNNING"] = "false"
    result = subprocess.run(
        ["bash", str(VERIFIER), "gpoint-core-worker", "a" * 40 + "-1-1", "a" * 40],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_remote_verifier_rejects_wrong_image_repository(tmp_path):
    env = _fake_docker(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
    env["FAKE_DOCKER_REPOSITORY"] = "ghcr.io/another-owner/gpoint-runtime"
    result = subprocess.run(
        ["bash", str(VERIFIER), "gpoint-core-worker", "a" * 40 + "-1-1", "a" * 40],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_eros_role_uses_upstream_image_source_label_contract(tmp_path):
    env = _fake_docker(tmp_path, ["gpoint-eros-pilot-engine-" + "a" * 40 + "-1-1"])
    env.update(FAKE_DOCKER_SERVICE="gpoint-eros-pilot",
               FAKE_DOCKER_ROLE="engine",
               FAKE_DOCKER_REPOSITORY="ghcr.io/akhripunov/gpoint-eros")
    result = subprocess.run(
        ["bash", str(VERIFIER), "gpoint-eros-pilot-engine", "a" * 40 + "-1-1", "a" * 40],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "docker-trace.jsonl").read_text().splitlines()]
    assert "com.gpoint.source.revision" in calls[-1][3]
