"""Layer7 SSH access must come from materialized environment values only.

The workflows write the key material into ``$RUNNER_TEMP/layer7/key`` and export
``GPOINT_SSH_KEY``/``GPOINT_KNOWN_HOSTS_FILE``. The composite action must resolve
both from that environment, never from ``$HOME/.ssh`` as the old Mac runner did.
"""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/layer7-deploy/action.yml"
NATIVE = ROOT / ".github/workflows/gpoint-native-release.yml"
MANUAL = ROOT / ".github/workflows/gpoint-layer7-staging.yml"
MATERIALIZED_KEY = "$RUNNER_TEMP/layer7/key"
PINNED_KNOWN_HOSTS = "$GITHUB_WORKSPACE/infra/staging/layer7_known_hosts"


def action_steps():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]


def step_by_name(name):
    return next(step for step in action_steps() if step.get("name") == name)


def test_host_key_step_uses_the_materialized_key_only():
    run = step_by_name("Verify pinned Layer7 host key and existing SSH access")["run"]
    assert 'key="$GPOINT_SSH_KEY"' in run
    assert f'test "$key" = "{MATERIALIZED_KEY}"' in run
    assert "infra/staging/layer7_known_hosts" in run
    assert "$HOME/.ssh" not in run
    assert "gpoint_layer7" not in run


def test_kamal_step_uses_the_materialized_key_and_pinned_known_hosts():
    run = step_by_name("Deploy exact immutable images with stock Kamal 2 to Layer7 staging")["run"]
    assert ': "${GPOINT_SSH_KEY:?materialized Layer7 SSH key path is required}"' in run
    assert f'export GPOINT_KNOWN_HOSTS_FILE="{PINNED_KNOWN_HOSTS}"' in run
    assert "$HOME/.ssh" not in run
    assert "gpoint_layer7" not in run


def test_both_workflows_materialize_the_documented_variables():
    for path in (NATIVE, MANUAL):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
        materialize = next(step for step in steps
                           if step.get("name") == "Materialize Layer7 SSH credentials")
        run = materialize["run"]
        assert f"LAYER7_SSH_KEY={MATERIALIZED_KEY}" in run
        assert f"GPOINT_SSH_KEY={MATERIALIZED_KEY}" in run
        assert f"GPOINT_KNOWN_HOSTS_FILE={PINNED_KNOWN_HOSTS}" in run
        assert ">> \"$GITHUB_ENV\"" in run
        assert "$HOME/.ssh" not in run


def test_kamal_env_contract_accepts_materialized_values_and_rejects_home_paths(tmp_path):
    run = step_by_name("Deploy exact immutable images with stock Kamal 2 to Layer7 staging")["run"]
    prelude = "\n".join(
        line for line in run.splitlines()
        if line.strip().startswith(': "${GPOINT_SSH_KEY')
        or line.strip() == 'test -f "$GPOINT_SSH_KEY"'
        or line.strip().startswith('export GPOINT_KNOWN_HOSTS_FILE=')
        or line.strip() == 'test -f "$GPOINT_KNOWN_HOSTS_FILE"')
    assert prelude, "the Kamal step lost its environment contract"

    workspace = tmp_path / "workspace"
    (workspace / "infra/staging").mkdir(parents=True)
    (workspace / "infra/staging/layer7_known_hosts").write_text("pinned\n")
    key = tmp_path / "key"
    key.write_text("fake\n")
    key.chmod(0o600)

    def execute(**extra):
        env = {**os.environ, "GITHUB_WORKSPACE": str(workspace), **extra}
        return subprocess.run(["bash", "-c", "set -u\n" + prelude],
                              capture_output=True, text=True, env=env)

    ok = execute(GPOINT_SSH_KEY=str(key))
    assert ok.returncode == 0, ok.stderr

    # The old Mac assumption (a HOME path, no materialized variable) must fail.
    failed = execute(GPOINT_SSH_KEY="")
    assert failed.returncode != 0


def test_transport_requires_an_explicit_identity():
    transport = (ROOT / "scripts/layer7-transport.sh").read_text(encoding="utf-8")
    assert "gpoint_layer7" not in transport
    assert 'LAYER7_SSH_KEY or GPOINT_SSH_KEY is required' in transport
    result = subprocess.run(["bash", str(ROOT / "scripts/layer7-transport.sh"), "ssh", "host"],
                            capture_output=True, text=True,
                            env={k: v for k, v in os.environ.items()
                                 if k not in {"LAYER7_SSH_KEY", "GPOINT_SSH_KEY"}})
    assert result.returncode != 0
    assert "required" in result.stderr
