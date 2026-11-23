"""One-shot Bot restoration safety tests; no production credentials or network."""
import ast
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/gpoint-bot-bootstrap.sh"
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"
AUTH = ROOT / ".release-authorizations/gpoint-bot-restore-20261123-001.json"
SHA = "66ed5fcd61af29dabadc2948886ccb40ee1c2f33"


def test_bootstrap_script_and_embedded_python_have_valid_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    source = SCRIPT.read_text()
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY", source, flags=re.DOTALL)
    assert len(blocks) == 2
    for block in blocks:
        ast.parse(block)


def test_bootstrap_can_only_run_on_new_authorization_push():
    source = SCRIPT.read_text()
    assert 'GITHUB_EVENT_NAME" != push' in source
    assert 'GITHUB_REF" != refs/heads/main' in source
    assert '"$BOT_AUTH_EVENT_BEFORE" "$GITHUB_SHA"' in source
    assert "git diff --name-status --no-renames" in source
    assert "A\\t" in source
    env = os.environ.copy()
    env.update(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main")
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0 and "SKIPPED event" in result.stdout
    env.update(GITHUB_EVENT_NAME="push", GITHUB_SHA=SHA, BOT_AUTH_EVENT_BEFORE=SHA)
    result = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0 and "SKIPPED no new authorization" in result.stdout


def test_bootstrap_never_redeploys_other_roles_or_mutates_webhook():
    source = SCRIPT.read_text()
    assert "KAMAL_ROLES=bot" in source
    assert "redeploy --skip-push --version" in source
    assert "--roles bot --config-file config/deploy.yml" in source
    assert "live_core_version" in source and "live_core_digest" in source
    assert "getMe" in source and "getWebhookInfo" in source
    assert 'if api("getWebhookInfo").get("url")' in source
    assert "GPOINT_BOT_READY=PASS" in source
    assert "GPOINT_BOT_BOOTSTRAP=PASS" in source
    assert "KAMAL_ROLES=api" not in source
    assert "KAMAL_ROLES=worker" not in source
    assert "KAMAL_ROLES=web" not in source
    assert "deleteWebhook" not in source
    assert "docker rm" not in source
    assert "GPOINT_PRODUCTION_HOST" in source
    assert "95.133.236.59" in source


def test_bootstrap_is_guarded_within_existing_protected_deploy():
    wf = WORKFLOW.read_text()
    start = wf.index("      - name: Restore absent GPoint Bot once from verified installed Core")
    end = wf.index("      - name: Read live application version with Kamal", start)
    bootstrap = wf[start:end]
    assert "github.event_name == 'push'" in bootstrap
    assert "BOT_AUTH_EVENT_BEFORE:" in bootstrap
    assert "bash scripts/gpoint-bot-bootstrap.sh" in bootstrap
    assert wf.index("      - name: Install verified Kamal SSH credentials") < start
    assert wf.index("      - name: Pull and verify pinned Kamal CLI image") < start
    assert wf.index("  deploy:\n") < start < wf.index("      - name: Plan exact component-scoped Kamal update from live baseline")


def test_owner_record_names_exact_baseline_scope_and_stop_conditions():
    data = json.loads(AUTH.read_text())
    assert data["schema_version"] == 3
    assert data["production_baseline_sha"] == SHA
    assert data["authorization_mode"] == "TASK_SCOPED_AUTO"
    assert data["owner_instruction_scope"] == "FULL_PRODUCTION_CYCLE"
    assert data["environment"] == "production"
    assert data["state"] == "ACTIVE" and data["revoked"] is False
    risk = data["risk_contracts"]["release_infrastructure"]
    assert set(risk["allowed_paths"]) == set(data["allowed_paths"])
    assert risk["required_gates"] and risk["stop_conditions"]
    assert risk["rollback_plan"] and risk["backup_plan"]
