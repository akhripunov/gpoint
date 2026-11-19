"""Contracts for declarative Native Release path filters."""

import re
import subprocess
import json
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
    assert step["with"]["predicate-quantifier"] == "some-with-excludes"
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


def _match_with_picomatch(filters, paths):
    picomatch = next((ROOT / "node_modules/.pnpm").glob(
        "picomatch@*/node_modules/picomatch"), None)
    assert picomatch is not None, "repository-pinned picomatch dependency is required"
    script = r'''const pm = require(process.argv[1]);
const payload = JSON.parse(process.argv[2]);
const [filters, paths] = payload;
const matches = Object.fromEntries(Object.entries(filters).map(([name, patterns]) => {
const positives = patterns.filter(pattern => !pattern.startsWith("!"));
const excludes = patterns.filter(pattern => pattern.startsWith("!")).map(pattern => pattern.slice(1));
return [name, paths.map(path => positives.some(pattern => pm(pattern)(path))
  && !excludes.some(pattern => pm(pattern)(path)))];
}));
process.stdout.write(JSON.stringify(matches));'''
    result = subprocess.run(
        ["node", "-e", script, str(picomatch), json.dumps([filters, paths])],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_all_five_component_filters_match_real_paths():
    _workflow, _step, filters = _filters()
    paths = [
        "apps/api/src/messages.ts",
        "apps/miniapp/src/main.tsx",
        "services/agata/src/main.py",
        "services/eros-engine/src/main.rs",
        "services/eros-engine/eros-pilot-embedding-relay.mjs",
    ]
    matches = _match_with_picomatch(
        {name: filters[name] for name in ("core", "miniapp", "agata", "eros_engine", "eros_relay")},
        paths,
    )
    assert matches == {
        "core": [True, False, False, False, False],
        "miniapp": [False, True, False, False, False],
        "agata": [False, False, True, False, False],
        "eros_engine": [False, False, False, True, False],
        "eros_relay": [False, False, False, False, True],
    }


def test_eros_engine_negative_globs_exclude_relay_and_configuration():
    _workflow, _step, filters = _filters()
    paths = [
        "services/eros-engine/src/main.rs",
        "services/eros-engine/eros-pilot-embedding-relay.mjs",
        "services/eros-engine/eros-layer7-staging-model-config.toml",
        "services/eros-engine/production.toml",
    ]
    matches = _match_with_picomatch({"eros_engine": filters["eros_engine"]}, paths)
    assert matches["eros_engine"] == [True, False, False, False]


def test_shared_dependencies_and_relay_kamal_config_match_intended_filters():
    _workflow, _step, filters = _filters()
    paths = ["packages/contracts/src/message.ts", "config/deploy.eros-relay.yml"]
    matches = _match_with_picomatch(
        {name: filters[name] for name in ("core", "miniapp", "eros_engine", "eros_relay", "infrastructure")},
        paths,
    )
    assert matches == {
        "core": [True, False], "miniapp": [True, False],
        "eros_engine": [False, False], "eros_relay": [False, True],
        "infrastructure": [False, True],
    }


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
    assert intent._component_paths(relay_config) == {"EROS"}


def test_eros_relay_kamal_config_stays_separate_from_engine_and_protected_config():
    _workflow, _step, filters = _filters()
    assert "config/deploy.eros-relay.yml" in filters["eros_relay"]
    assert "config/deploy.eros.yml" not in filters["eros_relay"]
    assert "config/deploy.eros-relay.yml" in filters["infrastructure"]
    assert "config/deploy.layer7-staging.eros.yml" not in filters["infrastructure"]
    assert filters["eros_engine"] != filters["eros_relay"]


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
