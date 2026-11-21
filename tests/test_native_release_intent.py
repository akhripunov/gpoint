"""Exact-SHA and component-scope contracts for Native First V3."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_release_intent", ROOT / "scripts/native_release_intent.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def component_repo(tmp_path, files=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    for name, value in {
        "apps/api/src/main.ts": "export const api = 1;\n",
        "apps/miniapp/src/main.tsx": "export const web = 1;\n",
        "services/eros-engine/runtime.rs": "fn main() {}\n",
        "services/eros-engine/eros-pilot-embedding-relay.mjs": "legacy relay\n",
        "config/deploy.ollama.yml": "service: infra\n",
        "README.md": "release\n",
        **(files or {}),
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo, git(repo, "rev-parse", "HEAD")


def commit_change(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-qm", "change " + path)
    return git(repo, "rev-parse", "HEAD")


def make_inventory(source):
    roles = {}
    repo_digests = {
        "CORE": "ghcr.io/akhripunov/gpoint-runtime@sha256:" + "1" * 64,
        "MINIAPP": "ghcr.io/akhripunov/gpoint-miniapp@sha256:" + "2" * 64,
        "EROS": "ghcr.io/akhripunov/gpoint-eros@sha256:" + "3" * 64,
    }
    image_ids = {key: "sha256:" + str(i) * 64 for i, key in enumerate(repo_digests, 4)}
    for role in MODULE.ROLE_REPOSITORIES:
        component = MODULE.ROLE_IDENTITY_GROUP[role]
        component = "EROS" if component == "EROS_ENGINE" else component
        roles[role] = {
            "role": role,
            "name": role + "-" + source + "-7-1",
            "version": source + "-7-1",
            "source_sha": source,
            "image_id": image_ids[component],
            "repo_digest": repo_digests[component],
        }
    return {"version": source + "-7-1", "source_sha": source, "roles": roles, "missing_roles": []}


def test_active_architecture_has_five_roles_and_three_images():
    assert set(MODULE.ROLE_REPOSITORIES) == {
        "gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot",
        "gpoint-miniapp-web", "gpoint-eros-pilot-engine",
    }
    assert set(MODULE.COMPONENT_IMAGES) == {"CORE", "MINIAPP", "EROS"}
    assert set(MODULE.COMPONENT_IMAGES.values()) == {"CORE", "MINIAPP", "EROS"}


@pytest.mark.parametrize("path,component", [
    ("apps/api/src/main.ts", "CORE"),
    ("apps/worker/src/main.ts", "CORE"),
    ("apps/bot/src/main.ts", "CORE"),
    ("apps/miniapp/src/main.tsx", "MINIAPP"),
    ("services/eros-engine/runtime.rs", "EROS"),
    ("config/deploy.eros.yml", "EROS"),
    ("config/deploy.ollama.yml", "NONE"),
    ("config/deploy.eros-api.yml", "CORE"),
])
def test_runtime_paths_map_only_to_their_active_component(path, component):
    expected = set() if component == "NONE" else {component}
    assert MODULE._component_paths(path) == expected


@pytest.mark.parametrize("path", [
    "services/agata/api.py",
    "services/agata/migrations/versions/0012_legacy.py",
    "config/deploy.agata.yml",
    "config/deploy.eros-relay.yml",
    "services/eros-engine/eros-pilot-embedding-relay.mjs",
    "services/eros-engine/eros-pilot-embedding-relay.test.mjs",
    "services/eros-engine/open-mac-ollama-tunnel.sh",
])
def test_retired_agata_and_relay_paths_do_not_select_an_image(path):
    assert MODULE._component_paths(path) == set()
    assert MODULE._known_non_runtime_path(path)


@pytest.mark.parametrize("path,expected", [
    ("apps/api/src/main.ts", "CORE"),
    ("apps/miniapp/src/main.tsx", "MINIAPP"),
    ("services/eros-engine/runtime.rs", "EROS"),
    ("services/eros-engine/eros-pilot-embedding-relay.mjs", "NONE"),
    ("config/deploy.ollama.yml", "NONE"),
])
def test_component_build_scope_uses_exact_component_diff(tmp_path, path, expected):
    repo, baseline = component_repo(tmp_path)
    candidate = commit_change(repo, path, "changed\n")
    plan = MODULE.classify_component_build_plan(
        repo, candidate, {"CORE": baseline, "MINIAPP": baseline, "EROS": baseline},
    )
    assert plan["deploy_scope"] == expected
    if expected == "NONE":
        assert plan["build_components"] == []
    else:
        assert plan["build_components"] == [expected]


def test_runtime_inventory_pins_updated_images_and_preserves_untouched_baselines():
    source = "a" * 40
    candidate = "b" * 40
    inventory = make_inventory(source)
    result = MODULE.expected_runtime_inventory(
        "CORE", inventory, candidate, candidate + "-101-2",
        "sha256:" + "a" * 64, "sha256:" + "b" * 64,
    )
    assert set(result["roles"]) == set(MODULE.ROLE_REPOSITORIES)
    for role in MODULE.COMPONENT_ROLES["CORE"]:
        row = result["roles"][role]
        assert row["sourceSha"] == candidate
        assert row["repositoryDigest"].endswith("sha256:" + "a" * 64)
        assert row["baselineImageId"] is None
    for role in (*MODULE.COMPONENT_ROLES["MINIAPP"], *MODULE.EROS_ROLES):
        row = result["roles"][role]
        assert row["sourceSha"] == source
        assert row["baselineImageId"] == inventory["roles"][role]["image_id"]


def test_missing_core_bot_forces_core_scope_without_inventing_a_bot_baseline(tmp_path):
    repo, source = component_repo(tmp_path)
    candidate = commit_change(repo, "README.md", "docs only\n")
    inventory = make_inventory(source)
    inventory["roles"].pop("gpoint-core-bot")
    inventory["missing_roles"] = ["gpoint-core-bot"]
    scope = MODULE.classify_component_scope(repo, candidate, inventory)
    assert scope["scope"] == "CORE"
    expected = MODULE.expected_runtime_inventory(
        scope["scope"], inventory, candidate, candidate + "-2-1",
        "sha256:" + "a" * 64, "sha256:" + "b" * 64,
    )
    assert expected["roles"]["gpoint-core-bot"]["sourceSha"] == candidate


def test_infrastructure_requires_protected_dispatch_and_exact_owner_sha():
    assert MODULE.is_infrastructure_change({"config/deploy.ollama.yml"})
    assert not MODULE.push_deploy_authorized(
        mode="DEPLOY", changed_paths={"config/deploy.ollama.yml"},
    )
    result, approved = MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="a" * 40,
        actor="owner", repository_owner="owner", approve_production=True,
    )
    assert (result, approved) == ("DEPLOY", True)
    result, approved = MODULE.classify_dispatch(
        ref="refs/heads/main", candidate_sha="a" * 40, github_sha="b" * 40,
        actor="owner", repository_owner="owner", approve_production=True,
    )
    assert result == "INVALID_DEPLOY" and not approved


def test_bootstrap_builds_the_three_active_application_images_only():
    assert MODULE.COMPONENT_IMAGES == {"CORE": "CORE", "MINIAPP": "MINIAPP", "EROS": "EROS"}
    assert set(MODULE.ALL_COMPONENT_ROLES) == {"CORE", "MINIAPP", "EROS"}
    assert not any("AGATA" in role or "RELAY" in role for role in MODULE.ROLE_REPOSITORIES)
