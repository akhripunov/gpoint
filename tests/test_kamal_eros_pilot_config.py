from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_eros_kamal_services_are_private_and_use_exact_release_owned_inputs():
    engine = yaml.safe_load((ROOT / "config/deploy.eros.yml").read_text())
    relay = yaml.safe_load((ROOT / "config/deploy.eros-relay.yml").read_text())
    api = yaml.safe_load((ROOT / "config/deploy.eros-api.yml").read_text())

    assert engine["service"] == "gpoint-eros-pilot"
    assert engine["image"] == "akhripunov/gpoint-eros-pilot"
    assert set(engine["servers"]) == {"engine"}
    assert engine["servers"]["engine"]["proxy"] is False
    assert engine["servers"]["engine"]["options"]["network"] == "gpoint_eros_data"
    assert engine["servers"]["engine"]["options"]["env-file"] == ["/opt/gpoint/env/eros-engine.env"]
    assert engine["servers"]["engine"]["options"]["volume"].endswith(
        "/source/services/eros-engine/eros-production-model-config.toml:/etc/eros-engine/model_config.toml:ro"
    )

    assert relay["service"] == "gpoint-eros-relay"
    assert relay["image"] == "akhripunov/gpoint-runtime"
    assert set(relay["servers"]) == {"relay"}
    assert relay["servers"]["relay"]["proxy"] is False
    assert relay["servers"]["relay"]["options"]["network"] == "host"
    assert relay["servers"]["relay"]["options"]["volume"].endswith(
        "/source/services/eros-engine/eros-pilot-embedding-relay.mjs:/opt/gpoint/eros-pilot-embedding-relay.mjs:ro"
    )
    assert "proxy" not in engine and "proxy" not in relay
    assert api["service"] == "gpoint-core"
    assert api["servers"]["api"]["options"]["env-file"] == [
        "/opt/gpoint/env/production.env", "/opt/gpoint/env/eros-api.env"
    ]
    assert api["servers"]["api"]["cmd"] == "node apps/api/dist/apps/api/src/main.js"
    assert "gpoint_app" not in api["servers"]["api"]["options"].get("network", "")
    assert "EROS_PRODUCTION_TEST_USER_IDS" not in (ROOT / "config/deploy.yml").read_text()
    assert engine["servers"]["engine"]["options"]["network"] == "gpoint_eros_data"


def test_only_post_boot_hooks_attach_the_eros_service_to_internal_networks():
    pre = (ROOT / ".kamal/hooks/pre-app-boot").read_text()
    post = (ROOT / ".kamal/hooks/post-app-boot").read_text()
    assert "gpoint-eros-pilot" in pre
    assert "gpoint-eros-relay" in pre
    assert "gpoint-eros-pilot:engine" in post
    assert "gpoint_eros_data" in (ROOT / "config/deploy.eros.yml").read_text()
    assert "gpoint_app:eros" in post
    assert "gpoint-eros-relay:relay" in post
    assert "stock Eros engine did not become healthy" in post
    assert "private Eros embedding relay did not become healthy" in post
    assert post.index('if [[ "$service:$role" == "gpoint-core:api" ]]') > post.index(
        "AGATA memory worker did not become healthy"
    )


def test_native_workflow_runs_pilot_in_disposable_environment_and_never_uses_ordinary_deploy():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["workflow_dispatch"]["inputs"]["synthetic_eros_pilot"]["default"] is False
    jobs = workflow["jobs"]
    assert jobs["eros-pilot-integration"]["if"] == "needs.workspace.outputs.mode == 'SYNTHETIC_PILOT_READY' || needs.workspace.outputs.mode == 'SYNTHETIC_PILOT_APPLY_REQUESTED' || needs.workspace.outputs.mode == 'SYNTHETIC_PILOT_VERIFY_ONLY'"
    steps = jobs["eros-pilot-integration"]["steps"]
    assert any("run-local-e2e.sh" in step.get("run", "") for step in steps)
    assert "needs.workspace.outputs.deploy == 'true'" in jobs["deploy"]["if"]
    assert jobs["eros-pilot-integration"]["permissions"]["packages"] == "read"
    assert jobs["eros-pilot-apply"]["permissions"]["packages"] == "write"
    apply_publish = next(step for step in jobs["eros-pilot-apply"]["steps"]
                         if "Build and publish the pinned stock Eros" in step.get("name", ""))
    assert jobs["eros-pilot-apply"]["steps"].index(apply_publish) > next(
        i for i, step in enumerate(jobs["eros-pilot-apply"]["steps"])
        if step.get("name", "").startswith("Recheck exact owner-approved pilot intent"))
    assert all("docker push" not in step.get("run", "") for step in jobs["eros-pilot-integration"]["steps"])
    assert "mode == 'SYNTHETIC_PILOT_READY'" not in jobs["deploy"]["if"]


def test_native_pilot_apply_is_separately_gated_and_ordinary_release_is_eros_aware():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["approve_synthetic_eros_pilot_apply"]["default"] is False
    jobs = workflow["jobs"]
    apply = jobs["eros-pilot-apply"]
    assert apply["environment"] == {"name": "gpoint-eros-pilot", "deployment": False}
    assert "SYNTHETIC_PILOT_APPLY" in apply["if"]
    assert "SYNTHETIC_PILOT_APPLY" not in jobs["deploy"]["if"]
    assert {"workspace", "python", "eros-pilot-integration", "build"}.issubset(set(apply["needs"]))
    assert apply["concurrency"]["group"] == "gpoint-production-release"
    assert apply["runs-on"] == ["self-hosted", "macOS", "gpoint-production"]
    assert "runner.temp" not in str(apply.get("env", {}))
    failure_cleanup = next(step for step in apply["steps"]
                           if step.get("name") == "Remove persistent Mac Ollama tunnel after failed Pilot Apply")
    assert failure_cleanup["if"] == "failure() || cancelled()"
    assert "GITHUB_ENV" in str(apply["steps"])
    assert any("eros-native-pilot-lifecycle.sh" in step.get("run", "") for step in apply["steps"])
    assert "approve_production" not in str(apply)
    lifecycle = (ROOT / "scripts/eros-native-pilot-lifecycle.sh").read_text()
    assert "--ip 172.31.254.3" not in lifecycle
    assert 'docker run --rm --network gpoint_eros_embeddings \\' in lifecycle
    assert "verifier_ip=dynamic" in lifecycle
    assert "EROS_QWEN3_RELAY=PASS" in lifecycle
    assert "'/opt/gpoint/releases')/pilot_sha/'source/scripts/eros_native_pilot_ops.py'" in lifecycle
    topology = next(step for step in jobs["deploy"]["steps"] if step.get("id") == "topology")
    assert "NO_EROS_STATE" in topology.get("run", "")
    assert "eros_pilot_active=true" in topology.get("run", "")
    assert "collect_kamal_eros_state.py" in topology.get("run", "")


def test_native_apply_reads_synthetic_allowlist_and_mode_from_the_api_container():
    lifecycle = (ROOT / "scripts/eros-native-pilot-lifecycle.sh").read_text()
    verify = lifecycle.split("REMOTE_VERIFY", 1)[1].split("PY\n", 1)[0]
    assert "api_values.get(key)!=expected" in verify
    assert "api_values.get('EROS_ENABLED')" in verify
    assert "api_values.get('EROS_PRODUCTION_TEST_MODE')" in verify
    assert "api_values.get('EROS_DATA_MODE')" in verify
    assert "if values.get(key)!=expected" not in verify


def test_mac_embedding_tunnel_uses_verified_known_hosts_and_loopback_reverse_forward():
    tunnel = (ROOT / "services/eros-engine/open-mac-ollama-tunnel.sh").read_text()
    assert 'UserKnownHostsFile="$known_hosts"' in tunnel
    assert "GatewayPorts=no" in tunnel
    assert "-R 127.0.0.1:11435:127.0.0.1:11434" in tunnel
    assert "ClearAllForwardings=yes" in tunnel
    assert "EROS_PILOT_REMOTE_QWEN3=PASS" in tunnel


def test_pilot_apply_uses_managed_tunnel_and_checks_it_after_apply_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    jobs = workflow["jobs"]
    apply_steps = jobs["eros-pilot-apply"]["steps"]
    names = [step.get("name", "") for step in apply_steps]
    assert any(name == "Install and verify persistent Mac Ollama tunnel" for name in names)
    assert not any(name == "Stop Mac Ollama reverse tunnel" for name in names)
    cleanup = jobs["eros-pilot-cleanup"]["steps"]
    cleanup_names = [step.get("name", "") for step in cleanup]
    assert cleanup_names.index("Remove Mac Ollama tunnel after successful pilot cleanup") > next(
        i for i, name in enumerate(cleanup_names) if "Restore the pre-pilot database backup" in name)
