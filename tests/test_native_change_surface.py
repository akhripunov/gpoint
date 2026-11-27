"""Contracts for Native First's active three-image release surface."""

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())


def test_release_filters_contain_only_active_application_components():
    data = workflow()
    filter_step = next(step for step in data["jobs"]["tests"]["steps"] if step.get("id") == "filter")
    assert filter_step["with"]["filters"] == ".github/filters/native-release.json"
    filters = yaml.safe_load((ROOT / filter_step["with"]["filters"]).read_text())
    assert {"core", "miniapp", "eros_engine", "eros_config", "database", "infrastructure"} <= set(filters)
    assert "agata" not in filters and "eros_relay" not in filters
    assert "config/deploy.ollama.yml" in filters["infrastructure"]
    assert "!services/eros-engine/eros-pilot-embedding-relay*" in filters["eros_engine"]
    assert "services/eros-engine/*.toml" in filters["eros_config"]


def test_component_path_classifier_agrees_with_the_shared_filter_source():
    sys.path.insert(0, str(ROOT / "scripts"))
    import native_release_intent as intent

    assert intent._component_paths("apps/miniapp/package.json") == {"MINIAPP"}
    assert intent._component_paths("apps/api/package.json") == {"CORE", "MINIAPP"}
    assert intent._component_paths("packages/contracts/src/message.ts") == {"CORE", "MINIAPP"}
    assert intent._component_paths("services/eros-engine/src/main.rs") == {"EROS"}
    assert intent._component_paths("services/eros-engine/ollama-model.lock.json") == {"CORE"}
    assert intent._component_paths("config/deploy.miniapp.yml") == {"MINIAPP"}
    assert intent._component_paths("services/eros-engine/eros-pilot-embedding-relay.mjs") == set()


def test_eros_engine_filter_excludes_independent_components_and_test_harness():
    sys.path.insert(0, str(ROOT / "scripts"))
    import native_release_intent as intent

    assert intent.paths_match_filter({"services/eros-engine/src/main.rs"}, "eros_engine")
    for path in ("services/eros-engine/local-test-e2e.mjs",
                 "services/eros-engine/local-e2e.test.mjs",
                 "services/eros-engine/run-local-e2e.sh",
                 "services/eros-engine/compose.release-test.yml",
                 "services/eros-engine/local-test-api-entrypoint.mjs"):
        assert not intent.paths_match_filter({path}, "eros_engine")
        assert intent._component_paths(path) == set()
        assert intent._known_non_runtime_path(path)
    assert intent.paths_match_filter({"config/deploy.eros.yml"}, "eros_engine")
    for path in ("config/deploy.eros-api.yml", "apps/api/src/messages.ts",
                 "apps/miniapp/src/page.tsx", "services/eros-engine/eros-pilot-embedding-relay.mjs",
                 ".github/workflows/gpoint-release.yml"):
        assert not intent.paths_match_filter({path}, "eros_engine")


def test_core_api_uses_only_current_eros_networks():
    api = yaml.safe_load((ROOT / "config/deploy.eros-api.yml").read_text())
    networks = api["servers"]["api"]["options"]["network"]
    assert set(networks) == {"shared_internal", "gpoint_eros_data"}
    assert "gpoint_ollama_internal" not in networks
    assert "gpoint_ollama_internal" not in yaml.safe_load((ROOT / "config/deploy.yml").read_text())[
        "servers"]["api"]["options"].get("network", [])
    assert 'model = "voyage-4-lite"' in (ROOT / "services/eros-engine/eros-production-model-config.toml").read_text()


def test_retired_relay_paths_are_non_runtime_and_ollama_can_auto_release():
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
    assert intent.push_deploy_authorized(mode="DEPLOY", changed_paths={"config/deploy.ollama.yml"})
    assert intent.push_deploy_authorized(mode="DEPLOY", changed_paths={"apps/api/src/messages.ts"})


def test_production_has_no_ollama_service_and_embeddings_are_native_or_test_only():
    steps = workflow()["jobs"]["deploy"]["steps"]
    deploy = next(step for step in steps if step.get("name") == "Deploy exact immutable images with stock Kamal")
    body = deploy["run"]
    assert not (ROOT / "config/deploy.ollama.yml").exists()
    assert not (ROOT / "scripts/verify-layer7-ollama.py").exists()
    assert "ollama" not in body.lower()
    assert "qwen" not in body.lower()
    assert "config/deploy.eros-relay.yml" not in body
    assert "deploy --skip-push --version \"$IMAGE_VERSION\" --roles relay" not in body


def test_synthetic_embedding_route_is_disposable_local_only():
    policy = (ROOT / "apps/api/src/eros-network-policy.js").read_text()
    gateway = (ROOT / "apps/api/src/eros-worker-gateway.ts").read_text()
    secret_writer = (ROOT / "scripts/eros-production-secrets.py").read_text()
    assert "if (!localTest) return false" in policy
    assert "return url.hostname === '127.0.0.1'" in policy
    assert "if (!isDisposableSyntheticLocalTest())" in gateway
    assert "EROS_TEST_EMBEDDING_URL" in gateway
    assert "EROS_OLLAMA_EMBEDDING_URL" not in secret_writer
