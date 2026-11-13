"""Every release classifier must agree on the same accumulated candidate.

The first release of #146 aborted in Deploy because the deploy scope planner met
`scripts/native_change_surface.py` and had no rule for it. CI had classified the
same file as release infrastructure. The two questions are different — which
tests to run, and which images to rebuild — but they must never contradict:
a path that CI treats as runtime must be mapped by the deploy planner, and a
path the deploy planner ignores must still force full CI.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_change_surface as surface  # noqa: E402
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
    unmapped = [path for path in candidate_paths
                if not intent._component_paths(path)
                and not intent._known_non_runtime_path(path)
                and path not in intent.KNOWN_RETIRED_NON_RUNTIME_FILES]
    assert not unmapped, (
        "the deploy scope planner would abort the release on these paths: "
        f"{unmapped}")


def test_ci_never_calls_a_path_runtime_that_the_deploy_planner_ignores(candidate_paths):
    contradictions = []
    for path in candidate_paths:
        deploy_components = intent._component_paths(path)
        ci_components = surface.classify([path])
        if not deploy_components and ci_components["full_ci"] is False:
            # Ignored by Deploy and not escalated by CI: release infrastructure
            # must still be fully tested, otherwise the path would be invisible.
            if not intent._known_non_runtime_path(path):
                contradictions.append(path)
    assert not contradictions, f"paths invisible to both classifiers: {contradictions}"


def test_release_infrastructure_still_forces_full_ci(candidate_paths):
    infra = [path for path in candidate_paths if path.startswith(("scripts/", ".github/"))]
    if not infra:
        pytest.skip("candidate touches no release infrastructure")
    assert surface.classify(infra)["full_ci"] is True


def test_unknown_roots_stay_fail_closed():
    # The planner must keep rejecting a path it cannot classify.
    assert intent._component_paths("somewhere/else.txt") == set()
    assert intent._known_non_runtime_path("somewhere/else.txt") is False


def test_accumulated_candidate_always_requires_candidate_images():
    """Regression for run 43148553035.

    Production (7d7d614b) is behind `main` by application code. A build plan that
    reports "no images" can never satisfy Deploy's fresh live scope, so Build must
    derive its plan from the production -> candidate diff and never from the push
    diff. Any runtime change on that accumulated diff must select its component.
    """
    try:
        subprocess.check_output(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{PRODUCTION_BASELINE}^{{commit}}"],
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.skip("production baseline is not in this checkout")
    head = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    baseline_sources = {component: PRODUCTION_BASELINE
                        for component in ("CORE", "AGATA", "MINIAPP", "EROS")}

    plan = intent.classify_component_build_plan(ROOT, head, baseline_sources)

    assert plan["build_components"], (
        "production is behind the candidate by runtime code; an empty build plan "
        "would fail Deploy with a missing prebuilt candidate image")
    assert "CORE" in plan["build_components"]
    assert plan["deploy_scope"] != "NONE"


def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, text=True).strip()


def _scope_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "apps/api/src").mkdir(parents=True)
    (repo / "apps/api/src/main.ts").write_text("export const v = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    for path in ("apps/api/src/main.ts", "apps/miniapp/src/main.tsx",
                 "services/agata/app.py", "services/eros-engine/app.py"):
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
        "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
        "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
        "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
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


def test_build_plan_and_live_scope_agree_on_the_same_production_baseline(tmp_path):
    """The regression behind run 43148553035: Build and Deploy must agree.

    Build derives its plan from the production component baseline, Deploy from
    the fresh live inventory. For the same sources both must select the same
    components, and every component Deploy will update must already be built.
    """
    repo, baseline, candidate, inventory = _scope_repo(tmp_path)
    sources = {"CORE": baseline, "AGATA": baseline, "MINIAPP": baseline, "EROS": baseline}

    plan = intent.classify_component_build_plan(repo, candidate, sources)
    scope = intent.classify_component_scope(repo, candidate, inventory)

    assert scope["scope"] == plan["deploy_scope"]
    live_components = set(scope["scope"].split("+")) - {"NONE"}
    assert live_components <= set(plan["build_components"]), (
        "Deploy would require an image Build never produced")


def test_bootstrap_plan_covers_any_live_scope(tmp_path):
    repo, baseline, candidate, inventory = _scope_repo(tmp_path)
    sources = {"CORE": baseline, "AGATA": baseline, "MINIAPP": baseline, "EROS": baseline}
    scope = intent.classify_component_scope(repo, candidate, inventory)
    live_components = set(scope["scope"].split("+")) - {"NONE"}

    bootstrap = set(intent.classify_component_build_plan(repo, candidate, sources)["build_components"])
    # A bootstrap build is the whole image set, so any live scope is covered.
    assert live_components <= bootstrap
