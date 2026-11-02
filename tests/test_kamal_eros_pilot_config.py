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
    assert engine["servers"]["engine"]["options"]["env-file"] == ["/opt/gpoint/env/eros.env"]
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
        "/opt/gpoint/env/production.env", "/opt/gpoint/env/eros.env"
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
    assert jobs["eros-pilot-integration"]["if"] == "needs.workspace.outputs.mode == 'SYNTHETIC_PILOT_READY' || needs.workspace.outputs.mode == 'SYNTHETIC_PILOT_APPLY_REQUESTED'"
    steps = jobs["eros-pilot-integration"]["steps"]
    assert any("run-local-e2e.sh" in step.get("run", "") for step in steps)
    assert "needs.workspace.outputs.deploy == 'true'" in jobs["deploy"]["if"]
    assert "mode == 'SYNTHETIC_PILOT_READY'" not in jobs["deploy"]["if"]


def test_native_pilot_apply_is_separately_gated_and_ordinary_release_fails_closed_after_pilot():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["approve_synthetic_eros_pilot_apply"]["default"] is False
    jobs = workflow["jobs"]
    apply = jobs["eros-pilot-apply"]
    assert apply["environment"] == {"name": "gpoint-eros-pilot", "deployment": False}
    assert "SYNTHETIC_PILOT_APPLY" in apply["if"]
    assert "SYNTHETIC_PILOT_APPLY" not in jobs["deploy"]["if"]
    topology = next(step for step in jobs["deploy"]["steps"] if step.get("id") == "topology")
    assert "NO_EROS_STATE" in topology.get("run", "")
    assert "collect_kamal_eros_state.py" in topology.get("run", "")
