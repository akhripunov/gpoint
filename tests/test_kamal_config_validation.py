import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-kamal-configs.sh"
CONFIGS = [
    "config/deploy.yml",
    "config/deploy.agata.yml",
    "config/deploy.miniapp.yml",
]
REGISTRY_PASSWORD = "config-validation-placeholder"


def setup_fake_tools(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "docker-trace.jsonl"

    ssh_keygen = fake_bin / "ssh-keygen"
    ssh_keygen.write_text("""#!/usr/bin/env python3
from pathlib import Path
import sys
key = Path(sys.argv[-1])
key.write_text('synthetic-only-key\\n')
key.chmod(0o600)
key.with_suffix(key.suffix + '.pub').write_text('ssh-ed25519 synthetic-only-key\\n')
""")
    ssh_keygen.chmod(0o755)

    docker = fake_bin / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
env = {}
for index, arg in enumerate(args[:-1]):
    if arg == '--env':
        key, value = args[index + 1].split('=', 1)
        env[key] = value
config = args[args.index('--config-file') + 1]
with open(os.environ['DOCKER_TRACE'], 'a') as trace:
    trace.write(json.dumps({'args': args, 'env': env, 'config': config}) + '\\n')
if 'secrets' in args and 'print' in args:
    if os.environ.get('MISSING_REGISTRY_SECRET') != 'true':
        print('KAMAL_REGISTRY_PASSWORD=' + env['KAMAL_REGISTRY_PASSWORD'])
else:
    print('Rendered config registry password: ' + env['KAMAL_REGISTRY_PASSWORD'])
if config == os.environ.get('FAIL_CONFIG'):
    print("ERROR (Kamal::ConfigurationError): proxy: Specify one of 'host' or 'hosts', not both", file=sys.stderr)
    raise SystemExit(7)
""")
    docker.chmod(0o755)
    return fake_bin, trace


def run_config_check(tmp_path, *, fail_config=None, missing_registry_secret=False):
    fake_bin, trace = setup_fake_tools(tmp_path)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_SHA": "a" * 40,
        "KAMAL_IMAGE": "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625",
        "DOCKER_TRACE": str(trace),
        "REGISTRY_SECRET_CANARY": "never-pass-this-value",
    })
    if fail_config:
        env["FAIL_CONFIG"] = fail_config
    if missing_registry_secret:
        env["MISSING_REGISTRY_SECRET"] = 'true'
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    return result, calls


def test_kamal_config_checker_runs_all_services_with_synthetic_credentials_and_silent_success(tmp_path):
    result, calls = run_config_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert [call["config"] for call in calls] == [config for config in CONFIGS for _ in range(2)]
    assert all('secrets' in calls[index]['args'] and 'print' in calls[index]['args']
               for index in (1, 3, 5))
    assert all(call["env"]["KAMAL_REGISTRY_PASSWORD"] == REGISTRY_PASSWORD for call in calls)
    assert all(call["env"]["GPOINT_PRODUCTION_HOST"] == "192.0.2.10" for call in calls)
    assert all(call["env"]["GPOINT_SSH_KEY"] == "/tmp/gpoint-kamal-config-test-key" for call in calls)
    assert all("never-pass-this-value" not in json.dumps(call) for call in calls)
    assert not list(tmp_path.glob("gpoint-kamal-config.*"))


def test_kamal_config_checker_rejects_unresolved_registry_secret_before_deploy(tmp_path):
    result, calls = run_config_check(tmp_path, missing_registry_secret=True)
    assert result.returncode != 0
    assert [call['config'] for call in calls] == [CONFIGS[0], CONFIGS[0]]
    assert 'registry credential did not resolve' in result.stderr
    assert REGISTRY_PASSWORD not in result.stdout + result.stderr
    assert not list(tmp_path.glob('gpoint-kamal-config.*'))


def test_kamal_config_checker_preserves_error_redacts_synthetic_password_and_stops(tmp_path):
    result, calls = run_config_check(tmp_path, fail_config=CONFIGS[0])

    assert result.returncode == 7
    output = result.stdout + result.stderr
    assert "Kamal config validation failed for config/deploy.yml (exit status 7)" in output
    assert "Specify one of 'host' or 'hosts', not both" in output
    assert REGISTRY_PASSWORD not in output
    assert "never-pass-this-value" not in output
    assert [call["config"] for call in calls] == [CONFIGS[0]]
    assert not list(tmp_path.glob("gpoint-kamal-config.*"))
