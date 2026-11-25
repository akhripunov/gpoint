"""Keep dorny CI selection and production-baseline planning fail-closed."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_release_intent as intent  # noqa: E402

# The production baseline the failed run 43068398600 measured against.
PRODUCTION_BASELINE = "7d7d614ba36ba436f25f9e486491723f487f85e6"


def _changed_paths(baseline: str, candidate: str) -> list[str]:
    try:
        subprocess.check_output(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{baseline}^{{commit}}"],
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.skip("production baseline is not in this checkout")
    raw = subprocess.check_output(
        ["git", "-C", str(ROOT), "diff", "--name-only", baseline, candidate], text=True)
    return [line for line in raw.splitlines() if line]


@pytest.fixture(scope="module")
def candidate_paths():
    head = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    return _changed_paths(PRODUCTION_BASELINE, head)


def test_every_candidate_path_is_mapped_or_explicitly_non_runtime(candidate_paths):
    head = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    deleted = set(subprocess.check_output(
        ["git", "-C", str(ROOT), "diff", "--diff-filter=D", "--name-only",
         PRODUCTION_BASELINE, head], text=True).splitlines())
    unmapped = [path for path in candidate_paths
                if not intent._component_paths(path)
                and not intent._known_non_runtime_path(path)
                and not (path in deleted and path.startswith("scripts/"))]
    assert not unmapped, (
        "the deploy scope planner would abort the release on these paths: "
        f"{unmapped}")


def test_unknown_roots_stay_fail_closed():
    # The planner must keep rejecting a path it cannot classify.
    assert intent._component_paths("somewhere/else.txt") == set()
    assert intent._known_non_runtime_path("somewhere/else.txt") is False


def test_only_well_formed_release_authorization_addenda_are_non_runtime():
    addendum = (
        ".release-authorizations/addenda/gpoint-prod-recovery-20261122-001/"
        "gpoint-prod-recovery-ollama-preflight-20261122-001.json")
    assert intent._known_non_runtime_path(addendum)
    assert intent._path_kind(addendum) == "ops"
    assert intent.classify_push(
        event_name="push", ref="refs/heads/main", changed_paths={addendum}) == "DEPLOY"
    assert intent.push_deploy_authorized(mode="DEPLOY", changed_paths={addendum})
    assert not intent._known_non_runtime_path(
        ".release-authorizations/addenda/unknown/metadata.json")
    assert intent._path_kind(
        ".release-authorizations/addenda/unknown/metadata.json") == "unknown"
    assert intent._path_kind(
        ".release-authorizations/addenda/gpoint-prod-recovery-20261122-001/tests/notes.txt") == "unknown"
    assert not intent._known_non_runtime_path(
        ".release-authorizations/addenda/gpoint-prod-recovery-20261122-001/notes.txt")
    assert not intent._known_non_runtime_path(
        ".release-authorizations/unreviewed.json")


def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, text=True).strip()


def _scope_repo(tmp_path, changed_paths=("apps/api/src/main.ts", "apps/miniapp/src/main.tsx",
                                         "services/agata/app.py", "services/eros-engine/app.py")):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "apps/api/src").mkdir(parents=True)
    (repo / "apps/api/src/main.ts").write_text("export const v = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    for path in changed_paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    repositories = {
        "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
        "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
        "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros",
    }
    identity = {}
    for index, image_repo in enumerate(dict.fromkeys(repositories.values()), 1):
        image_id = "sha256:" + str(index) * 64
        identity[image_repo] = (image_id, image_repo + "@sha256:" + str(index) * 64)
    inventory = {"roles": {}}
    for role, image_repo in repositories.items():
        version = f"{baseline}-100-1"
        image_id, repo_digest = identity[image_repo]
        inventory["roles"][role] = {
            "name": f"{role}-{version}", "version": version, "source_sha": baseline,
            "image_id": image_id, "repo_digest": repo_digest,
        }
    return repo, baseline, candidate, inventory


def test_accumulated_miniapp_change_selects_only_miniapp_deploy(tmp_path):
    repo, baseline, candidate, inventory = _scope_repo(
        tmp_path, changed_paths=("apps/miniapp/src/main.tsx",))

    scope = intent.classify_component_scope(repo, candidate, inventory)

    assert scope["scope"] == "MINIAPP"


def test_shared_dependency_selects_each_owning_component(tmp_path):
    repo, _baseline, candidate, inventory = _scope_repo(
        tmp_path, changed_paths=("packages/contracts/src/index.ts",))

    scope = intent.classify_component_scope(repo, candidate, inventory)

    assert scope["scope"] == "CORE+MINIAPP"


def test_missing_live_component_is_forced_into_deploy_scope(tmp_path):
    repo, _baseline, candidate, inventory = _scope_repo(
        tmp_path, changed_paths=("README.md",))
    inventory["roles"].pop("gpoint-eros-pilot-engine")

    scope = intent.classify_component_scope(repo, candidate, inventory)

    assert scope["scope"] == "EROS"
    assert scope["reason"] == "missing_runtime_roles"


@pytest.mark.parametrize(("path", "expected_scope"), [
    ("infra/production/Gpoint.Miniapp.Dockerfile", "MINIAPP"),
    ("scripts/docker_dependency_manifests.py", "CORE+MINIAPP"),
])
def test_build_input_changes_select_owning_components(tmp_path, path, expected_scope):
    repo, _baseline, candidate, inventory = _scope_repo(tmp_path, changed_paths=(path,))
    assert intent.classify_component_scope(repo, candidate, inventory)["scope"] == expected_scope
