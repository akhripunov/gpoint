import subprocess
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resolve_native_build_scope as MODULE


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ".github/workflows/gpoint-native-release.yml"
REPOSITORY = "akhripunov/gpoint"
BASELINE = "1" * 40
MANIFEST_SHA = "a" * 64


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def repo_with_baseline(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("baseline\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "baseline")
    return repo, git(repo, "rev-parse", "HEAD")


def commit_file(repo: Path, path: str, content: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-qm", "candidate")
    return git(repo, "rev-parse", "HEAD")


def deployment_fixture(*, task="deploy", target_sha=BASELINE, environment_url=None,
                       state="success", run_head_sha=BASELINE, run_event="push",
                       run_branch="main", run_conclusion="success", attempt=1):
    deployment_id, run_id = 200, 100
    deployment = {
        "id": deployment_id, "sha": target_sha, "environment": "production",
        "production_environment": True, "transient_environment": False,
        "task": task, "created_at": "2026-11-02T12:00:00Z",
    }
    source_url = environment_url or (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-core-source-sha=" + BASELINE
        + "&gpoint-agata-source-sha=" + BASELINE
        + "&gpoint-miniapp-source-sha=" + BASELINE
    )
    status = {
        "state": state, "environment": "production",
        "created_at": "2026-11-02T12:01:00Z",
        "environment_url": source_url,
        "log_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}",
    }
    run = {
        "path": WORKFLOW_PATH, "head_sha": run_head_sha,
        "head_branch": run_branch, "event": run_event,
        "status": "completed", "conclusion": run_conclusion,
        "run_attempt": attempt,
    }
    return [deployment], {deployment_id: [status]}, {run_id: run}


def test_latest_native_deployment_exposes_exact_component_baselines():
    deployments, statuses, runs = deployment_fixture()

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY
    )

    assert baseline == {
        "status": "VERIFIED",
        "deployment_id": 200,
        "manifest_sha256": MANIFEST_SHA,
        "component_sources": {"CORE": BASELINE, "AGATA": BASELINE, "MINIAPP": BASELINE,
                              "EROS": BASELINE},
    }


def test_successful_native_environment_record_supplies_component_baselines():
    deployments, statuses, runs = deployment_fixture()
    deployments[0].update(production_environment=False, payload={})
    statuses[200][0]["log_url"] = (
        f"https://github.com/{REPOSITORY}/actions/runs/100/job/300"
    )

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY,
        jobs_by_id={300: {"run_id": 100, "run_attempt": 1, "status": "completed", "conclusion": "success"}},
    )

    assert baseline["status"] == "VERIFIED"
    assert baseline["component_sources"] == dict.fromkeys(MODULE.ALL_COMPONENTS, BASELINE)


def test_successful_native_environment_record_cannot_hide_invalid_manifest():
    deployments, statuses, runs = deployment_fixture()
    deployments[0].update(production_environment=False, payload={})
    statuses[200][0]["environment_url"] = "https://example.invalid/"

    with pytest.raises(ValueError, match="verified GPoint manifest URL"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY
        )


def test_successful_native_environment_record_requires_successful_canonical_run():
    deployments, statuses, runs = deployment_fixture(run_conclusion="failure")
    deployments[0].update(production_environment=False, payload={})

    with pytest.raises(ValueError, match="canonical Native workflow"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY
        )


def test_cli_uses_latest_failed_native_attempt_as_unverified_baseline(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    deployments, statuses, runs = deployment_fixture()
    deployments[0].update(production_environment=False, payload={})
    statuses[200][0]["log_url"] = f"https://github.com/{REPOSITORY}/actions/runs/100/job/300"
    deployments.extend([
        dict(deployments[0], id=201, created_at="2026-11-03T12:00:00Z"),
        dict(deployments[0], id=199, created_at="2026-11-01T12:00:00Z"),
    ])
    calls = []

    def gh_json(route):
        calls.append(route)
        if "/deployments?" in route:
            return deployments
        if "/201/" in route:
            return [dict(statuses[200][0], state="failure", log_url=
                         f"https://github.com/{REPOSITORY}/actions/runs/101/job/301")]
        raise AssertionError("must stop before older history")

    monkeypatch.setattr(MODULE, "_gh_json", gh_json)
    def gh_run(args, **kwargs):
        assert args[-1].endswith(("/runs/101", "/jobs/301"))
        value = ({"run_id": 101, "run_attempt": 1, "status": "completed", "conclusion": "failure"}
                 if "/actions/jobs/301" in args[-1] else dict(runs[100], conclusion="failure"))
        return SimpleNamespace(stdout=json.dumps(value))

    monkeypatch.setattr(MODULE.subprocess, "run", gh_run)
    monkeypatch.setattr(MODULE, "resolve_build_plan", lambda repo, sha, ds, ss, rs, **kwargs:
                        MODULE.resolve_production_component_sources(
                            ds, ss, rs, repository=REPOSITORY, jobs_by_id=kwargs["jobs_by_id"]))
    monkeypatch.setattr(sys, "argv", ["resolve_native_build_scope.py", "--candidate-sha", BASELINE,
                                    "--repository", REPOSITORY])

    assert MODULE.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "BOOTSTRAP_FULL"
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["failure", "error"])
def test_failed_native_attempt_requires_fresh_live_reconciliation(state):
    deployments, statuses, runs = deployment_fixture(state=state, run_conclusion="failure")
    deployments[0].update(production_environment=False, payload={})
    plan = MODULE.resolve_production_component_sources(deployments, statuses, runs, repository=REPOSITORY)
    assert plan["status"] == "BOOTSTRAP_FULL"
    assert plan["reason"] == "failed_native_attempt_requires_live_reconciliation"


def test_pending_native_attempt_blocks_instead_of_using_older_success():
    deployments, statuses, runs = deployment_fixture(state="pending")
    deployments[0].update(production_environment=False, payload={})
    older = dict(deployments[0], id=199, created_at="2026-11-01T12:00:00Z")
    deployments.append(older)
    statuses[199] = [dict(statuses[200][0], state="success")]
    with pytest.raises(ValueError, match="not successful"):
        MODULE.resolve_production_component_sources(deployments, statuses, runs, repository=REPOSITORY)


def test_failed_native_attempt_from_unrelated_workflow_blocks():
    deployments, statuses, runs = deployment_fixture(state="failure", run_conclusion="failure")
    deployments[0].update(production_environment=False, payload={})
    runs[100]["path"] = ".github/workflows/unrelated.yml"
    with pytest.raises(ValueError, match="canonical Native workflow"):
        MODULE.resolve_production_component_sources(deployments, statuses, runs, repository=REPOSITORY)


@pytest.mark.parametrize("job", [
    {},
    {"run_id": 101, "run_attempt": 1, "status": "completed", "conclusion": "success"},
    {"run_id": 100, "run_attempt": 2, "status": "completed", "conclusion": "success"},
    {"run_id": 100, "run_attempt": 1, "status": "completed", "conclusion": "failure"},
])
def test_native_job_url_requires_the_same_successful_run_attempt(job):
    deployments, statuses, runs = deployment_fixture()
    deployments[0].update(production_environment=False, payload={})
    statuses[200][0]["log_url"] = f"https://github.com/{REPOSITORY}/actions/runs/100/job/300"

    with pytest.raises(ValueError, match="job.*attempt"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY, jobs_by_id={300: job})


def test_ignores_github_environment_job_record_when_selecting_production_baseline():
    deployments, statuses, runs = deployment_fixture()
    environment_job = {
        "id": 201,
        "sha": "2" * 40,
        "environment": "production",
        "production_environment": False,
        "transient_environment": False,
        "task": "deploy",
        "created_at": "2026-11-04T04:55:55Z",
        "payload": {},
    }
    deployments.append(environment_job)

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY
    )

    assert baseline["status"] == "VERIFIED"
    assert baseline["deployment_id"] == 200


def test_latest_failed_deployment_blocks_instead_of_using_older_baseline():
    deployments, statuses, runs = deployment_fixture(state="failure")
    older = dict(deployments[0], id=199, created_at="2026-11-01T12:00:00Z")
    deployments.insert(0, older)
    statuses[199] = [dict(statuses[200][0], created_at="2026-11-01T12:01:00Z")]

    with pytest.raises(ValueError, match="latest production Deployment is not successful"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY
        )


def test_rejects_component_metadata_with_a_missing_source_key():
    deployments, statuses, runs = deployment_fixture()
    statuses[200][0]["environment_url"] = statuses[200][0]["environment_url"].replace(
        "&gpoint-miniapp-source-sha=" + BASELINE, ""
    )

    with pytest.raises(ValueError, match="component source evidence is incomplete"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY
        )


def test_legacy_success_without_component_sources_uses_one_bootstrap_build():
    deployments, statuses, runs = deployment_fixture(
        environment_url="https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA,
        run_head_sha=BASELINE,
    )

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY
    )

    assert baseline == {"status": "BOOTSTRAP_FULL", "deployment_id": 200,
                        "reason": "successful_legacy_deployment_has_no_component_source_manifest"}


def test_tooling_only_push_without_manifest_uses_verify_only_plan(tmp_path):
    repo, before = repo_with_baseline(tmp_path)
    candidate = commit_file(repo, "tests/test_release_tooling.py", "assert True\n")
    deployments, statuses, runs = deployment_fixture(
        environment_url="https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA,
        run_head_sha=BASELINE,
    )

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs,
        repository=REPOSITORY, push_before=before,
    )

    assert plan == {
        "status": "VERIFY_ONLY",
        "deploy_scope": "UNRESOLVED",
        "build_components": [],
        "reason": "verified_tooling_only_push",
    }


@pytest.mark.parametrize("path", [
    "apps/api/src/main.ts",
    "unmapped/release-file.bin",
    "packages/db/prisma/migrations/20261104120000_add_flag/migration.sql",
    "packages/db/prisma/production-approved-migrations.json",
])
def test_runtime_unknown_or_migration_push_path_does_not_use_verify_only(tmp_path, path):
    repo, before = repo_with_baseline(tmp_path)
    candidate = commit_file(repo, path, "candidate content\n")
    deployments, statuses, runs = deployment_fixture(
        environment_url="https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA,
        run_head_sha=BASELINE,
    )

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs,
        repository=REPOSITORY, push_before=before,
    )

    assert plan["status"] == "BOOTSTRAP_FULL"
    assert plan["build_components"] == ["CORE", "AGATA", "MINIAPP", "EROS"]


def test_tooling_only_candidate_without_exact_push_before_does_not_use_verify_only(tmp_path):
    repo, _before = repo_with_baseline(tmp_path)
    candidate = commit_file(repo, "tests/test_release_tooling.py", "assert True\n")
    deployments, statuses, runs = deployment_fixture(
        environment_url="https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA,
        run_head_sha=BASELINE,
    )

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs, repository=REPOSITORY,
    )

    assert plan["status"] == "BOOTSTRAP_FULL"
    assert plan["build_components"] == ["CORE", "AGATA", "MINIAPP", "EROS"]


def test_rollback_baseline_requires_all_sources_equal_the_exact_target_sha():
    target = "2" * 40
    deployments, statuses, runs = deployment_fixture(
        task="rollback", target_sha=target, run_head_sha="3" * 40,
        run_event="workflow_dispatch",
    )
    statuses[200][0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-core-source-sha=" + target
        + "&gpoint-agata-source-sha=" + target
        + "&gpoint-miniapp-source-sha=" + target
    )

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY
    )

    assert baseline["component_sources"] == {
        "CORE": target, "AGATA": target, "MINIAPP": target, "EROS": target,
    }


def test_native_environment_rollback_tracks_workflow_sha_and_runtime_target_separately():
    target = "2" * 40
    workflow_sha = "3" * 40
    deployments, statuses, runs = deployment_fixture(
        task="deploy", target_sha=workflow_sha, run_head_sha=workflow_sha,
        run_event="workflow_dispatch",
    )
    statuses[200][0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-runtime-target-sha=" + target
        + "&gpoint-core-source-sha=" + target
        + "&gpoint-agata-source-sha=" + target
        + "&gpoint-miniapp-source-sha=" + target
    )

    baseline = MODULE.resolve_production_component_sources(
        deployments, statuses, runs, repository=REPOSITORY
    )

    assert baseline["status"] == "VERIFIED"
    assert baseline["component_sources"] == {
        "CORE": target, "AGATA": target, "MINIAPP": target, "EROS": target,
    }


def test_native_environment_rollback_rejects_invalid_runtime_target_sha():
    target = "invalid"
    workflow_sha = "3" * 40
    deployments, statuses, runs = deployment_fixture(
        task="deploy", target_sha=workflow_sha, run_head_sha=workflow_sha,
        run_event="workflow_dispatch",
    )
    statuses[200][0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-runtime-target-sha=" + target
        + "&gpoint-core-source-sha=" + workflow_sha
        + "&gpoint-agata-source-sha=" + workflow_sha
        + "&gpoint-miniapp-source-sha=" + BASELINE
    )

    with pytest.raises(ValueError, match="runtime target SHA"):
        MODULE.resolve_production_component_sources(
            deployments, statuses, runs, repository=REPOSITORY
        )


def test_selective_plan_builds_only_the_component_changed_since_its_baseline(tmp_path):
    repo, baseline_sha = repo_with_baseline(tmp_path)
    candidate = commit_file(repo, "apps/miniapp/src/main.tsx", "export default 1;\n")
    deployments, statuses, runs = deployment_fixture(target_sha=baseline_sha,
                                                       run_head_sha=baseline_sha)
    url = "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
    url += "&gpoint-core-source-sha=" + baseline_sha
    url += "&gpoint-agata-source-sha=" + baseline_sha
    url += "&gpoint-miniapp-source-sha=" + baseline_sha
    statuses[200][0]["environment_url"] = url

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs, repository=REPOSITORY
    )

    assert plan["deploy_scope"] == "MINIAPP"
    assert plan["build_components"] == ["MINIAPP"]


def test_selective_plan_returns_verify_only_for_staggered_sources_and_old_migrations(tmp_path):
    repo, baseline_sha = repo_with_baseline(tmp_path)
    migration = "packages/db/prisma/migrations/20261103120000_add_flag/migration.sql"
    migration_sha = commit_file(repo, migration, "ALTER TABLE users ADD COLUMN flag boolean;\n")
    core_source_sha = commit_file(repo, "apps/api/src/main.ts", "export const version = 2;\n")
    candidate = commit_file(repo, "docs/release/automation.md", "release tooling only\n")
    deployments, statuses, runs = deployment_fixture(
        target_sha=baseline_sha, run_head_sha=baseline_sha
    )
    statuses[200][0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-core-source-sha=" + core_source_sha
        + "&gpoint-agata-source-sha=" + baseline_sha
        + "&gpoint-miniapp-source-sha=" + baseline_sha
    )

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs, repository=REPOSITORY
    )

    assert plan["status"] == "VERIFIED"
    assert plan["baseline_sources"] == {
        "CORE": core_source_sha, "AGATA": baseline_sha, "MINIAPP": baseline_sha,
        "EROS": core_source_sha,
    }
    assert plan["deploy_scope"] == "NONE"
    assert plan["build_components"] == []
    assert plan["reason"] == "no_runtime_changes"
    assert migration_sha != baseline_sha


def test_layer7_staging_only_paths_do_not_select_application_images(tmp_path):
    repo, baseline_sha = repo_with_baseline(tmp_path)
    paths = (
        ".github/actionlint.yaml",
        "config/deploy.layer7-staging.yml",
        "config/deploy.layer7-staging.miniapp.yml",
        "config/deploy.layer7-staging.eros-api.yml",
        "config/deploy.layer7-staging.eros.yml",
        "config/deploy.layer7-staging.eros-relay.yml",
        "infra/staging/layer7_known_hosts",
        "scripts/layer7-transport.sh",
        "scripts/layer7_staging_database.py",
        "scripts/layer7_staging_env.py",
        "scripts/write_layer7_staging_env.py",
        "scripts/write_layer7_staging_eros_api_env.py",
    )
    candidate = baseline_sha
    for index, path in enumerate(paths):
        candidate = commit_file(repo, path, f"staging-only {index}\n")
    deployments, statuses, runs = deployment_fixture(target_sha=baseline_sha,
                                                       run_head_sha=baseline_sha)
    statuses[200][0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + MANIFEST_SHA
        + "&gpoint-core-source-sha=" + baseline_sha
        + "&gpoint-agata-source-sha=" + baseline_sha
        + "&gpoint-miniapp-source-sha=" + baseline_sha
    )

    plan = MODULE.resolve_build_plan(
        repo, candidate, deployments, statuses, runs, repository=REPOSITORY
    )

    assert plan["deploy_scope"] == "NONE"
    assert plan["build_components"] == []
    assert plan["reason"] == "no_runtime_changes"


def test_missing_deployment_history_is_fail_closed_for_component_scope(tmp_path):
    repo, baseline_sha = repo_with_baseline(tmp_path)
    candidate = commit_file(repo, "apps/api/src/main.ts", "export {};\n")

    plan = MODULE.resolve_build_plan(
        repo, candidate, [], {}, {}, repository=REPOSITORY
    )

    assert plan == {"status": "BOOTSTRAP_FULL", "deploy_scope": "UNRESOLVED",
                    "build_components": ["CORE", "AGATA", "MINIAPP", "EROS"],
                    "reason": "production_component_baseline_unavailable"}
