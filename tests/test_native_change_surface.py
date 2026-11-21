"""Contracts for Native First's active three-image release surface."""

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())


def test_release_filters_contain_only_active_application_components():
    data = workflow()
    filter_step = next(step for step in data["jobs"]["tests"]["steps"] if step.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    assert {"core", "miniapp", "eros_engine", "database", "infrastructure"} <= set(filters)
    assert "agata" not in filters and "eros_relay" not in filters
    assert "config/deploy.ollama.yml" in filters["infrastructure"]
    assert "!services/eros-engine/eros-pilot-embedding-relay*" in filters["eros_engine"]


def test_ollama_is_an_independent_pinned_internal_accessory():
    config = yaml.safe_load((ROOT / "config/deploy.ollama.yml").read_text())
    accessory = config["accessories"]["ollama"]
    assert accessory["image"] == (
        "ollama/ollama@sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223"
    )
    assert accessory["network"] == "gpoint_ollama_internal"
    assert accessory["port"] == "127.0.0.1:11435:11434"
    assert "ollama-layer7-models:/root/.ollama" in accessory["volumes"]
    assert accessory["options"]["network-alias"] == "ollama"
    assert accessory["options"]["restart"] == "unless-stopped"
    assert accessory["options"]["cpus"] == 1.5
    assert accessory["options"]["memory"] == "3g"


def test_core_api_joins_the_ollama_network_only_in_eros_aware_config():
    api = yaml.safe_load((ROOT / "config/deploy.eros-api.yml").read_text())
    networks = api["servers"]["api"]["options"]["network"]
    assert "gpoint_ollama_internal" in networks
    assert "gpoint_ollama_internal" not in yaml.safe_load((ROOT / "config/deploy.yml").read_text())[
        "servers"]["api"]["options"].get("network", [])
    assert "http://gpoint-core-api:3000/eros-worker/v1/embeddings" in (
        ROOT / "services/eros-engine/eros-production-model-config.toml"
    ).read_text()


def test_retired_relay_paths_never_select_an_image_or_deploy():
    sys.path.insert(0, str(ROOT / "scripts"))
    import native_release_intent as intent

    retired = {
        "config/deploy.eros-relay.yml",
        "services/eros-engine/eros-pilot-embedding-relay.mjs",
        "services/eros-engine/eros-pilot-embedding-relay.test.mjs",
        "services/eros-engine/open-mac-ollama-tunnel.sh",
    }
    for path in retired:
        assert intent._component_paths(path) == set()
        assert intent._known_non_runtime_path(path)
    assert intent._component_paths("config/deploy.ollama.yml") == set()
    assert intent._known_non_runtime_path("config/deploy.ollama.yml")
    assert intent.is_infrastructure_change({"config/deploy.ollama.yml"})
    assert not intent.push_deploy_authorized(mode="DEPLOY", changed_paths={"config/deploy.ollama.yml"})
    assert intent.push_deploy_authorized(mode="DEPLOY", changed_paths={"apps/api/src/messages.ts"})


def test_production_api_does_not_publish_ollama_to_public_networks():
    deploy = next(step for step in workflow()["jobs"]["deploy"]["steps"]
                  if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = deploy["run"]
    assert "docker network create --internal gpoint_ollama_internal" in body
    assert "config/deploy.ollama.yml" in body
    assert "config/deploy.eros-relay.yml" not in body
    assert "deploy --skip-push --version \"$IMAGE_VERSION\" --roles relay" not in body
