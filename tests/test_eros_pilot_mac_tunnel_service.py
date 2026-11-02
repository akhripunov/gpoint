import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "services/eros-engine/eros_pilot_mac_tunnel_service.py"
SPEC = importlib.util.spec_from_file_location("eros_pilot_mac_tunnel_service", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def build(**overrides):
    args = {
        "username": "gpoint",
        "home": "/Users/gpoint",
        "target": "root@vps.example.test",
        "identity": "/Users/gpoint/.ssh/id_ed25519",
        "known_hosts": "/Users/gpoint/.ssh/known_hosts",
        "script": "/Users/gpoint/Library/Application Support/GPoint/Eros Pilot/open.sh",
        "node": "/opt/homebrew/bin/node",
        "source_sha": "a" * 40,
    }
    args.update(overrides)
    return MODULE.build_plist(**args)


def test_system_launchdaemon_starts_at_boot_and_restarts_with_loopback_only_inputs():
    plist = build()
    assert plist["Label"] == "com.gpoint.eros-pilot-ollama-tunnel"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 15
    assert plist["Umask"] == 0o077
    assert plist["UserName"] == "gpoint"
    assert plist["ProgramArguments"] == ["/bin/bash", "/Users/gpoint/Library/Application Support/GPoint/Eros Pilot/open.sh"]
    assert plist["EnvironmentVariables"]["EROS_PILOT_VPS_SSH_TARGET"] == "root@vps.example.test"
    assert plist["EnvironmentVariables"]["EROS_PILOT_SOURCE_SHA"] == "a" * 40
    assert plist["EnvironmentVariables"]["GPOINT_SSH_IDENTITY_FILE"].endswith("/.ssh/id_ed25519")
    assert "private-key-content" not in repr(plist)


@pytest.mark.parametrize("target", ["root@host;curl evil", "root@-bad", "user@vps.example.test", "root@host/path"])
def test_launchdaemon_rejects_nonliteral_or_nonroot_ssh_targets(target):
    with pytest.raises(ValueError, match="VPS SSH target"):
        build(target=target)


def test_loaded_but_stopped_launchdaemon_is_not_reported_as_tunnel_ready():
    running = """\n\tstate = running\n\tpid = 4321\n"""
    assert MODULE.launchd_running_pid(running) == "4321"
    for loaded_but_stopped in (
        "state = waiting\npid = (not running)\n",
        "state = running\npid = 0\n",
        "state = running\n",
    ):
        with pytest.raises(ValueError, match="not running"):
            MODULE.launchd_running_pid(loaded_but_stopped)


def test_tags_verifier_checks_pinned_qwen_digest_without_embedding_request():
    verifier = (ROOT / "services/eros-engine/verify-mac-ollama-tags.mjs").read_text()
    assert "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d" in verifier
    assert "/api/tags" in verifier
    assert "/api/embed" not in verifier
    assert "127.0.0.1" in verifier


def test_workflow_does_not_own_or_stop_the_persistent_tunnel_process():
    workflow = (ROOT / ".github/workflows/gpoint-native-release.yml").read_text()
    assert "Stop Mac Ollama reverse tunnel" not in workflow
    assert "eros-pilot-mac-tunnel-service.sh install" in workflow
    assert "eros-pilot-mac-tunnel-service.sh verify" in workflow
    installer = (ROOT / "scripts/eros-pilot-mac-tunnel-service.sh").read_text()
    assert '"$source_root/services/eros-engine/open-mac-ollama-tunnel.sh"' in installer
    assert '"$source_root/services/eros-engine/verify-mac-ollama-tags.mjs"' in installer
    assert 'stable_manager="$service_dir/eros-pilot-mac-tunnel-service.sh"' in installer
    assert 'cp "$source_root/scripts/eros-pilot-mac-tunnel-service.sh" "$stable_manager"' in installer
    assert '"$stable_manager" "$stable_known_hosts"' in installer
    assert "--launchd-pid" in installer
    assert "launchdaemon_process_identity_mismatch" in installer
    assert "launchdaemon_run_at_load_disabled" in installer
    assert "launchdaemon_keepalive_disabled" in installer
    assert "ProgramArguments:1" in installer


def test_separate_workflow_verifies_after_native_release_workflow_has_completed():
    postcheck = (ROOT / ".github/workflows/gpoint-eros-pilot-tunnel-postcheck.yml").read_text()
    assert "workflow_run:" in postcheck
    assert "types:\n      - completed" in postcheck
    assert "apply_succeeded == 'true'" in postcheck
    assert "Protected Native Kamal synthetic Eros Pilot Apply" in postcheck
    assert '"$HOME/Library/Application Support/GPoint/Eros Pilot/eros-pilot-mac-tunnel-service.sh" verify' in postcheck
