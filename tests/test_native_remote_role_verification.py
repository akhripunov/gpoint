import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"
VERIFIER = ROOT / "scripts/verify-kamal-role.sh"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step_script(job, name):
    return next(step["run"] for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)


def _fake_docker(tmp_path, names):
    binary = tmp_path / "docker"
    binary.write_text(
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[0] == "ps":
    print(os.environ.get("FAKE_DOCKER_NAMES", ""))
elif args[0] == "inspect":
    output_format = args[2]
    if output_format == "{{.State.Running}}":
        print("true")
    elif output_format == '{{index .Config.Labels "org.opencontainers.image.revision"}}':
        print(os.environ["FAKE_DOCKER_REVISION"])
    else:
        raise SystemExit("unexpected docker inspect format")
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
    assert "verify_single_running_role gpoint-agata-memory-worker" in script
    assert "docker inspect" not in script
    assert "docker ps" not in script


def test_rollback_role_inspection_runs_over_the_pinned_vps_ssh_connection():
    script = _step_script("rollback", "Roll back consumers and then APIs with stock Kamal")
    assert 'ssh -T "root@${GPOINT_PRODUCTION_HOST}" bash -s -- "$1" "$TARGET_VERSION" "$TARGET_SHA"' in script
    assert '< "$GITHUB_WORKSPACE/scripts/verify-kamal-role.sh"' in script
    assert "docker inspect" not in script
    assert "docker ps" not in script


def test_rollback_passes_target_version_as_kamal_positional_argument():
    script = _step_script("rollback", "Roll back consumers and then APIs with stock Kamal")
    calls = [line.strip() for line in script.splitlines() if "kamal rollback" in line]
    assert len(calls) == 6
    assert all('kamal rollback "$TARGET_VERSION" --roles ' in line for line in calls)
    assert all("rollback --version" not in line for line in calls)


def test_remote_verifier_accepts_one_exact_running_role_and_revision(tmp_path):
    result = _run_verifier(tmp_path, ["gpoint-core-worker-" + "a" * 40 + "-1-1"])
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
