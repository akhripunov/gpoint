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
    "config/deploy.eros.yml",
    "config/deploy.eros-relay.yml",
    "config/deploy.eros-api.yml",
    "config/deploy.layer7-staging.yml",
    "config/deploy.layer7-staging.miniapp.yml",
    "config/deploy.layer7-staging.eros-api.yml",
    "config/deploy.layer7-staging.eros.yml",
    "config/deploy.layer7-staging.eros-relay.yml",
]
REGISTRY_PASSWORD = "config-validation-placeholder"
KAMAL_IMAGE = "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625"


def test_kamal_config_checker_pulls_pinned_image_and_passes_exact_cli_arguments(tmp_path):
    result, calls = run_config_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["command"] for call in calls] == ["pull", "image inspect"] + ["run"] * (len(CONFIGS) * 2)
    assert calls[0]["args"] == ["pull", "--platform", "linux/amd64", KAMAL_IMAGE]
    assert calls[1]["args"] == [
        "image", "inspect", "--format",
        "{{.Os}}/{{.Architecture}} {{json .RepoDigests}}", KAMAL_IMAGE,
    ]
    run_calls = calls[2:]
    assert all("--platform" in call["args"] and
               call["args"][call["args"].index("--platform") + 1] == "linux/amd64"
               for call in run_calls)
    assert all("--network" in call["args"] and
               call["args"][call["args"].index("--network") + 1] == "none"
               for call in run_calls)
    expected_configs = [config for config in CONFIGS for _ in range(2)]
    assert len(run_calls) == len(expected_configs)
    for call, config in zip(run_calls, expected_configs):
        kamal_argv = call["args"][call["args"].index(KAMAL_IMAGE) + 1:]
        assert kamal_argv[-2:] == ["--config-file", config]


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
trace_path = os.environ['DOCKER_TRACE']
def record(call):
    with open(trace_path, 'a') as trace:
        trace.write(json.dumps(call) + '\\n')
if args[:1] == ['pull']:
    record({'command': 'pull', 'args': args})
    raise SystemExit(0)
if args[:2] == ['image', 'inspect']:
    record({'command': 'image inspect', 'args': args})
    print('linux/amd64 [\\"ghcr.io/basecamp/kamal@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625\\"]')
    raise SystemExit(0)
if args[:1] != ['run']:
    raise SystemExit('unexpected Docker command: ' + ' '.join(args))
env = {}
for index, arg in enumerate(args[:-1]):
    if arg == '--env':
        key, value = args[index + 1].split('=', 1)
        env[key] = value
config = args[args.index('--config-file') + 1]
record({'command': 'run', 'args': args, 'env': env, 'config': config})
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


def test_kamal_config_checker_runs_all_services_with_synthetic_credentials(tmp_path):
    result, calls = run_config_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (
        "KAMAL_IMAGE_PULL=PASS PLATFORM=linux/amd64 "
        "DIGEST=sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625\n"
    )
    assert result.stderr == ""
    calls = [call for call in calls if call["command"] == "run"]
    assert [call["config"] for call in calls] == [config for config in CONFIGS for _ in range(2)]
    assert all('secrets' in calls[index]['args'] and 'print' in calls[index]['args']
               for index in range(1, len(calls), 2))
    assert all(call["env"]["KAMAL_REGISTRY_PASSWORD"] == REGISTRY_PASSWORD for call in calls)
    assert all(call["env"]["GPOINT_PRODUCTION_HOST"] == "192.0.2.10" for call in calls)
    assert all(call["env"]["GPOINT_LAYER7_STAGING_HOST"] == "192.0.2.20" for call in calls)
    assert all(call["env"]["GPOINT_SSH_KEY"] == "/tmp/gpoint-kamal-config-test-key" for call in calls)
    assert all(call["env"]["GPOINT_APP_SOURCE_SHA"] == call["env"]["GPOINT_COMMIT_SHA"] for call in calls)
    assert all("never-pass-this-value" not in json.dumps(call) for call in calls)
    assert not list(tmp_path.glob("gpoint-kamal-config.*"))


def test_kamal_config_checker_rejects_unresolved_registry_secret_before_deploy(tmp_path):
    result, calls = run_config_check(tmp_path, missing_registry_secret=True)
    calls = [call for call in calls if call["command"] == "run"]
    assert result.returncode != 0
    assert [call['config'] for call in calls] == [CONFIGS[0], CONFIGS[0]]
    assert 'registry credential did not resolve' in result.stderr
    assert REGISTRY_PASSWORD not in result.stdout + result.stderr
    assert not list(tmp_path.glob('gpoint-kamal-config.*'))


def test_kamal_config_checker_preserves_error_redacts_synthetic_password_and_stops(tmp_path):
    result, calls = run_config_check(tmp_path, fail_config=CONFIGS[0])
    calls = [call for call in calls if call["command"] == "run"]

    assert result.returncode == 7
    output = result.stdout + result.stderr
    assert "Kamal config validation failed for config/deploy.yml (exit status 7)" in output
    assert "Specify one of 'host' or 'hosts', not both" in output
    assert REGISTRY_PASSWORD not in output
    assert "never-pass-this-value" not in output
    assert [call["config"] for call in calls] == [CONFIGS[0]]
    assert not list(tmp_path.glob("gpoint-kamal-config.*"))
