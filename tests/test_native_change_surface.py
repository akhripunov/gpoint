"""Contracts for declarative Native Release path filters."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _filters():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    steps = workflow["jobs"]["tests"]["steps"]
    step = next(step for step in steps if step.get("id") == "filter")
    return workflow, step, yaml.safe_load(step["with"]["filters"])


def test_pinned_dorny_filters_cover_five_logical_components():
    workflow, step, filters = _filters()

    assert step["uses"] == "dorny/paths-filter@ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d"
    assert step["with"]["base"] == (
        "${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.base.sha || github.event.before }}"
    )
    assert "pull-requests" in workflow["jobs"]["tests"]["permissions"]
    assert {"core", "miniapp", "agata", "eros_engine", "eros_relay"} <= set(filters)
    expected_paths = {
        "core": "apps/api/**",
        "miniapp": "apps/miniapp/**",
        "agata": "services/agata/**",
        "eros_engine": "services/eros-engine/**",
        "eros_relay": "services/eros-engine/eros-pilot-embedding-relay.mjs",
    }
    for component, path in expected_paths.items():
        assert path in filters[component]
    assert "services/eros-engine/eros-pilot-embedding-relay.mjs" not in filters["core"]
    assert "!services/eros-engine/eros-pilot-embedding-relay.mjs" in filters["eros_engine"]


def test_shared_build_dependencies_select_core_and_miniapp_filters():
    _workflow, _step, filters = _filters()

    shared_paths = (
        "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "tsconfig.base.json",
        "packages/**", "infra/production/Gpoint.Dockerfile",
    )
    for path in shared_paths:
        assert path in filters["core"]
        assert path in filters["miniapp"]
    for path in ("apps/api/package.json", "apps/bot/package.json", "apps/worker/package.json"):
        assert path in filters["miniapp"]


def test_database_and_production_infrastructure_filters_are_explicit():
    workflow, _step, filters = _filters()
    assert "packages/db/prisma/schema.prisma" in filters["database"]
    assert "packages/db/prisma/migrations/**" in filters["database"]
    assert "services/agata/migrations/**" in filters["database"]
    for path in (".github/**", ".kamal/**", "config/deploy.eros-relay.yml",
                 "infra/production/**", "scripts/**", "systemd/**"):
        assert path in filters["infrastructure"]
    assert "paths" not in workflow.get("on", workflow.get(True))["push"]


def test_relay_kamal_change_is_not_core_and_never_auto_applies():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import native_release_intent as intent  # noqa: E402

    relay_config = "config/deploy.eros-relay.yml"
    relay_runtime = "services/eros-engine/eros-pilot-embedding-relay.mjs"
    assert intent._component_paths(relay_config) == {"EROS"}
    assert intent._component_paths(relay_runtime) == {"EROS"}
    assert intent.is_infrastructure_change({relay_config})
    assert not intent.push_deploy_authorized(mode="DEPLOY", changed_paths={relay_config})
    assert intent.push_deploy_authorized(mode="DEPLOY", changed_paths={"apps/api/src/messages.ts"})
    deploy = _filters()[0]["jobs"]["deploy"]
    assert "production" in deploy["environment"]["name"]


def test_eros_relay_kamal_config_stays_separate_from_engine_and_protected_config():
    _workflow, _step, filters = _filters()
    assert "config/deploy.eros-relay.yml" in filters["eros_relay"]
    assert "config/deploy.eros.yml" not in filters["eros_relay"]
    assert "config/deploy.eros-relay.yml" in filters["infrastructure"]
    assert "config/deploy.layer7-staging.eros.yml" not in filters["infrastructure"]


def test_eros_api_remains_loopback_only():
    text = (ROOT / "config/deploy.eros-api.yml").read_text()
    config = yaml.safe_load(re.sub(r"<%=.*?%>", "rendered", text))
    assert config["servers"]["api"]["options"]["publish"] == ["127.0.0.1:13003:3000"]
    assert config["servers"]["api"]["proxy"]["path_prefixes"] == ["/api"]


def test_latest_commit_paths_are_mapped_or_explicitly_non_runtime():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import native_release_intent as intent  # noqa: E402

    try:
        changed = subprocess.check_output(
            ["git", "-C", str(ROOT), "diff", "--name-only", "HEAD~1", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).split()
    except subprocess.CalledProcessError:
        import pytest
        pytest.skip("single-commit checkout")
    if not changed:
        import pytest
        pytest.skip("no parent diff")
    unmapped = [path for path in changed
                if not intent._component_paths(path)
                and not intent._known_non_runtime_path(path)
                and path not in intent.KNOWN_RETIRED_NON_RUNTIME_FILES]
    assert not unmapped
