import yaml
import re
import os
import subprocess
import pytest
import json
import sys
from urllib.parse import urlsplit
from pathlib import Path


def test_host_relay_uses_stock_kamal_accessory_with_one_network():
    # App#run in pinned Kamal 2.12 always adds --network kamal. A host
    # network relay must use Accessory#run, which supports network: host.
    text = (ROOT / 'config/deploy.layer7-staging.eros-relay.yml').read_text()
    config = yaml.safe_load(re.sub(r'<%=.*?%>', 'test', text))
    assert config['primary_role'] == 'relay'
    assert config['servers']['relay']['cmd'] == '/bin/false'
    assert config['servers']['relay']['proxy'] is False
    assert 'options' not in config['servers']['relay']
    relay = config['accessories']['relay']
    assert relay['network'] == 'host'
    assert 'network' not in relay['options']
    assert relay['env']['clear']['EROS_PILOT_RELAY_BIND'] == '172.31.254.1:11434'
    assert relay['image'].startswith('ghcr.io/akhripunov/gpoint-runtime@')
    assert 'GPOINT_KAMAL_EXPECTED_DIGEST' in text
    assert relay['options']['memory'] == '64m'
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    deploy = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Deploy exact immutable images with stock Kamal 2 to Layer7 staging')
    assert 'kamal accessory boot relay' in deploy
    assert "--filter 'name=^/gpoint-eros-relay-relay$' --filter label=service=gpoint-eros-relay-relay" in deploy
    assert 'kamal accessory reboot relay' in deploy
    accessory_commands = [line for line in deploy.splitlines() if 'kamal accessory ' in line]
    assert len(accessory_commands) == 4
    assert all('--hosts \"$GPOINT_LAYER7_STAGING_HOST\"' in line for line in accessory_commands)
    assert 'kamal deploy --skip-push --version "$IMAGE_VERSION" --roles relay' not in deploy
    verify = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Verify runtime roles, health, Eros embeddings, and API-to-engine route')
    assert 'container=gpoint-eros-relay-relay' in verify
    assert '{{.HostConfig.NetworkMode}}' in verify


@pytest.mark.parametrize('existing,expected,action', [
    ('', 0, 'boot'),
    ('gpoint-eros-relay-relay', 0, 'reboot'),
    ('unexpected-relay', 1, ''),
])
def test_relay_accessory_lifecycle_is_exact_and_stock_kamal(tmp_path, existing, expected, action):
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    deploy = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Deploy exact immutable images with stock Kamal 2 to Layer7 staging')
    code = deploy[deploy.index('relay_container="$('):deploy.index('# The permanent Mac SSH forward')]
    log = tmp_path / 'actions'
    stub = "set -euo pipefail\nssh() { printf '%s' \"$EXISTING\"; }\nkamal() { printf '%s\\n' \"$*\" >> \"$LOG\"; }\n"
    result = subprocess.run(['bash', '-c', stub + code], env=dict(os.environ, EXISTING=existing, LOG=str(log), CORE_DIGEST='sha256:' + 'a'*64, GITHUB_WORKSPACE='/scope', GPOINT_LAYER7_STAGING_HOST='95.133.236.59'), text=True, capture_output=True)
    assert result.returncode == expected
    calls = log.read_text().splitlines() if log.exists() else []
    assert calls == ([f'accessory {action} relay --hosts 95.133.236.59 --config-file config/deploy.layer7-staging.eros-relay.yml'] if action else [])


def test_staging_label_lookup_templates_use_valid_string_literals():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    commands = '\n'.join(s.get('run', '') for s in workflow['jobs']['layer7_staging']['steps'])
    args = re.findall(r"\{\{index \.Config\.Labels (.*?)\}\}", commands)
    assert len(args) >= 4
    assert {json.loads(arg) for arg in args} == {'com.gpoint.source.revision', 'org.opencontainers.image.revision'}


def test_staging_route_probe_receives_stdin_and_uses_generated_eros_host():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    verify = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Verify runtime roles, health, Eros embeddings, and API-to-engine route')
    assert 'docker exec -i "$api" node --input-type=module -' in verify
    writer = (ROOT / 'scripts/eros-production-secrets.py').read_text()
    endpoint = re.search(r"'EROS_SERVICE_URL': '([^']+)'", writer).group(1)
    checked_host = re.search(r"endpoint.hostname !== '([^']+)'", verify).group(1)
    assert checked_host == urlsplit(endpoint).hostname


@pytest.mark.parametrize('leak', ['', 'core_on_data', 'alias_on_shared'])
def test_staging_network_isolation_probe_uses_actual_topology(tmp_path, leak):
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    verify = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Verify runtime roles, health, Eros embeddings, and API-to-engine route')
    code = re.search(r"<<'PY_CORE_DB'\n(.*?)\nPY_CORE_DB", verify, re.S).group(1)
    api = {'gpoint_eros_api': {}, 'shared_internal': {}}
    db = {'gpoint_eros_data': {'Aliases': ['gpoint-eros-db']}, 'shared_internal': {'Aliases': None}}
    if leak == 'core_on_data':
        api['gpoint_eros_data'] = {}
    if leak == 'alias_on_shared':
        db['shared_internal']['Aliases'] = ['gpoint-eros-db']
    result = subprocess.run([sys.executable, '-c', code, json.dumps(api), json.dumps(db)], text=True, capture_output=True)
    assert (result.returncode == 0) == (not leak)
    assert ('EROS_DATABASE_NETWORK_ISOLATION=PASS' in result.stdout) == (not leak)


@pytest.mark.parametrize('previous,raced,expected,actions', [
    ('', 'false', 0, []),
    ('gpoint-core-api-' + 'a' * 40 + '-1-1', 'false', 0, ['stop']),
    ('gpoint-core-api-' + 'a' * 40 + '-1-1', 'true', 1, ['stop', 'start']),
    ('unexpected-container', 'false', 1, []),
])
def test_private_gateway_stop_shell_preserves_first_install_and_race_recovery(tmp_path, previous, raced, expected, actions):
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    deploy = next(s['run'] for s in workflow['jobs']['layer7_staging']['steps'] if s.get('name') == 'Deploy exact immutable images with stock Kamal 2 to Layer7 staging')
    section = deploy[deploy.index('check_queue_empty() {'):]
    section = section[:section.index('GPOINT_KAMAL_EXPECTED_DIGEST="$CORE_DIGEST"')]
    log = tmp_path / 'kamal.log'
    script = '''set -euo pipefail
    queue_calls=0
    ssh() {
      if [[ "${!#}" == 'docker ps'* ]]; then printf '%s' "$PREVIOUS"; return; fi
      cat >/dev/null
      queue_calls=$((queue_calls+1))
      if [[ "$RACED" == true && "$queue_calls" == 2 ]]; then return 1; fi
    }
    kamal() { printf '%s\\n' "$2" >> "$ACTION_LOG"; }
    ''' + section
    result = subprocess.run(['bash', '-c', script], stdin=subprocess.DEVNULL, text=True, capture_output=True,
                            env={**os.environ, 'PREVIOUS': previous, 'RACED': raced, 'ACTION_LOG': str(log),
                                 'GITHUB_WORKSPACE': str(ROOT), 'GPOINT_LAYER7_STAGING_HOST': '95.133.236.59'})
    assert result.returncode == expected, result.stderr
    assert (log.read_text().splitlines() if log.exists() else []) == actions


def test_staging_api_keeps_the_existing_private_worker_forward_port():
    text = (ROOT / 'config/deploy.layer7-staging.eros-api.yml').read_text()
    config = yaml.safe_load(re.sub(r'<%=.*?%>', 'rendered', text))
    assert config['servers']['api']['options']['publish'] == ['127.0.0.1:13001:3000']
    assert config['servers']['api']['proxy'] is False


def test_staging_stops_only_managed_api_after_proving_queue_empty():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
    steps = workflow['jobs']['layer7_staging']['steps']
    deploy = next(x['run'] for x in steps if x.get('name') == 'Deploy exact immutable images with stock Kamal 2 to Layer7 staging')
    queue = deploy.index('LAYER7_GPOINT_QUEUE_EMPTY=PASS')
    stop = deploy.index('kamal app stop --roles api')
    boot = deploy.index('kamal deploy --skip-push --version "$IMAGE_VERSION" --roles api')
    assert queue < stop < boot
    assert stop < deploy.index('check_queue_empty', stop) < boot
    assert deploy.index('if [[ -n "$previous_api" ]]') < stop
    assert 'if ! check_queue_empty; then' in deploy
    assert 'kamal app start --version "$previous_version" --roles api' in deploy
    assert "status IN ('PENDING','RUNNING')" in deploy
    assert '-d gpoint_staging' in deploy
    assert '127.0.0.1:13001' in next(x['run'] for x in steps if x.get('name') == 'Verify runtime roles, health, Eros embeddings, and API-to-engine route')


ROOT = Path(__file__).resolve().parents[1]


def test_layer7_staging_is_owner_dispatched_on_trusted_runner_with_read_only_packages():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["layer7_staging"]
    actionlint = yaml.safe_load((ROOT / ".github/actionlint.yaml").read_text())

    assert job["runs-on"] == ["self-hosted", "macOS"]
    assert actionlint["self-hosted-runner"]["labels"] == ["macOS"]
    assert job["environment"]["name"] == "layer7-staging"
    assert job["permissions"] == {
        "contents": "read",
        "packages": "read",
        "actions": "read",
        "deployments": "write",
    }
    assert "GPOINT_KAMAL_HOST" not in str(job)
    assert "production" not in str(job["environment"]).lower()


def test_layer7_staging_runs_pinned_kamal_without_a_local_docker_engine():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["layer7_staging"]
    ruby = next(step for step in job["steps"] if step.get("uses", "").startswith("ruby/setup-ruby@"))
    assert ruby["uses"] == "ruby/setup-ruby@7f797815d8adceb1c412e7abefdd1ab4abc47a5f"
    assert ruby["with"]["ruby-version"] == "3.3.6"
    assert ruby["with"]["self-hosted"] == "true"
    installer = next(step for step in job["steps"] if step.get("name") == "Install pinned Kamal 2.12.0")
    assert "gem install kamal --version 2.12.0" in installer["run"]
    deploy = next(step for step in job["steps"] if step.get("name") == "Deploy exact immutable images with stock Kamal 2 to Layer7 staging")
    assert "docker run" not in deploy["run"]
    assert 'command kamal "$@"' in deploy["run"]
    assert "$HOME/.ssh/gpoint_layer7" in deploy["run"]
    image_pull = next(step for step in job["steps"] if step.get("name") == "Pull and verify exact private images with the read-only job token")
    assert "kamal_image=ghcr.io/basecamp/kamal" not in image_pull["run"]
    assert 'docker pull --platform linux/amd64 "$kamal_image"' not in image_pull["run"]


def test_layer7_staging_requires_exact_successful_prepare_and_explicit_staging_intent():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["deploy_layer7_staging"]["type"] == "boolean"
    assert inputs["deploy_layer7_staging"]["default"] == "false"
    assert inputs["approve_layer7_staging"]["default"] == "false"
    assert inputs["staging_image_sha"]["type"] == "string"
    assert inputs["staging_image_sha"]["required"] == "false"

    job = workflow["jobs"]["layer7_staging"]
    assert job["needs"] == ["tests"]
    assert "inputs.deploy_layer7_staging == true" in job["if"]
    assert "inputs.staging_image_sha != ''" in job["if"]
    step_text = yaml.safe_dump(job["steps"])
    assert "verify_native_prepared_run.py" in step_text
    assert "prepared_run_id" in step_text
    assert "prepared_eros_digest" in step_text
    assert "StrictHostKeyChecking=yes" in step_text
    assert job["env"]["CANDIDATE_SHA"] == "${{ inputs.staging_image_sha }}"
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    recheck = next(step for step in job["steps"] if step.get("name") == "Recheck owner dispatch, current main, and prepared image ancestry")
    assert "STAGING_IMAGE_SHA" in recheck["env"]
    assert "compare/$STAGING_IMAGE_SHA...$WORKFLOW_SHA" in recheck["run"]
    assert 'merge_base_commit",{}).get("sha")' in recheck["run"]
    assert "staging_preflight.outcome == 'success'" in step_text
    assert "Remove temporary Kamal GHCR authorization" in step_text
    assert "docker logout ghcr.io" in step_text
    assert "no_existing_credentials=true" in step_text
    assert "CORE_API_EROS_ROUTE=PASS EROS_DATABASE_NETWORK_ISOLATION=PASS" in (
        ROOT / ".github/workflows/gpoint-native-release.yml"
    ).read_text()


def test_staging_kamal_configs_are_separate_from_public_production_routes():
    config_paths = [
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
    ]
    for relative in config_paths:
        path = ROOT / relative
        assert path.is_file(), relative
        text = path.read_text()
        assert "GPOINT_KAMAL_HOST" not in text
        assert "GPOINT_PRODUCTION_HOST" not in text
        assert "gpoint.one" not in text
        assert "GPOINT_LAYER7_STAGING_HOST" in text
    api_config = (ROOT / "config/deploy.layer7-staging.yml").read_text()
    eros_config = (ROOT / "config/deploy.layer7-staging.eros.yml").read_text()
    assert "- gpoint_eros_api" in api_config
    assert "- gpoint_eros_data" not in api_config
    assert "- gpoint_eros_data" in eros_config and "- gpoint_eros_api" in eros_config


def test_layer7_staging_never_uses_production_environment_or_production_host():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    staging = workflow["jobs"]["layer7_staging"]
    serialized = yaml.safe_dump(staging)
    assert "secrets.GPOINT_KAMAL_HOST" not in serialized
    assert "environment: production" not in serialized
    assert "GPOINT_LAYER7_STAGING_HOST" in serialized
    assert "GITHUB_TOKEN" in serialized


def test_layer7_staging_is_separate_from_build_and_production_deploy_jobs():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    staging = workflow["jobs"]["layer7_staging"]
    assert "LAYER7_STAGING" not in workflow["jobs"]["build"]["if"]
    assert "LAYER7_STAGING" not in workflow["jobs"]["deploy"]["if"]
    assert "needs.tests.outputs.deploy == 'true'" in workflow["jobs"]["deploy"]["if"]
    assert staging["env"]["KAMAL_REGISTRY_PASSWORD"] == "${{ secrets.GITHUB_TOKEN }}"
    assert staging["permissions"]["packages"] == "read"
    assert staging["permissions"].get("contents") == "read"
    assert staging["permissions"].get("deployments") == "write"
    assert "packages: write" not in yaml.safe_dump(staging)
    steps = yaml.safe_dump(staging["steps"])
    assert "steps.prepared.outputs.image_version" in steps
    for config in (
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
    ):
        assert config in steps
    assert "--mode prepare" in steps
    assert "--mode apply" in steps
    assert "docker inspect --format" in steps
    assert "com.gpoint.source.revision" in steps
    assert "StrictHostKeyChecking=yes" in steps


def test_layer7_staging_can_run_core_and_eros_without_a_staging_telegram_bot():
    workflow = yaml.load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text(), Loader=yaml.BaseLoader)
    staging = workflow["jobs"]["layer7_staging"]
    steps = staging["steps"]
    validation = next(step for step in steps if step.get("id") == "staging_env")
    assert "bot_enabled=" in validation["run"]
    assert "TELEGRAM_BOT_TOKEN" not in validation["env"].get("TELEGRAM_BOT_TOKEN", "")
    assert "secrets.GPOINT_LAYER7_STAGING_APP_ENV" in validation["env"]["STAGING_ENV_BUNDLE"]
    deploy = next(step for step in steps if step.get("name") == "Deploy exact immutable images with stock Kamal 2 to Layer7 staging")
    assert deploy["env"]["STAGING_BOT_ENABLED"] == "${{ steps.staging_env.outputs.bot_enabled }}"
    assert 'if [[ "$role" == bot && "$STAGING_BOT_ENABLED" != true ]]' in deploy["run"]
    assert 'kamal deploy --skip-push --version "$IMAGE_VERSION" --roles api' in deploy["run"]
    verify = next(step for step in steps if step.get("name") == "Verify runtime roles, health, Eros embeddings, and API-to-engine route")
    assert verify["env"]["STAGING_BOT_ENABLED"] == "${{ steps.staging_env.outputs.bot_enabled }}"
    assert '[[ "$bot_enabled" == true || "$bot_enabled" == false ]]' in verify["run"]
    assert '[[ "$bot_enabled" == false && "$spec" == gpoint-core-bot\\|* ]]' in verify["run"]
    assert "STAGING_BOT_ENABLED" not in str(staging["env"])
    assert "secrets.GPOINT_PRODUCTION" not in yaml.safe_dump(staging)
