import importlib.util
import json
import subprocess
import sys
import tempfile
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "native_prepared_run", ROOT / "scripts/verify_native_prepared_run.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def fixture():
    sha = "a" * 40
    run = {
        "id": 1234, "run_attempt": 2, "head_sha": sha,
        "head_branch": "main", "event": "push", "status": "completed",
        "conclusion": "success", "path": ".github/workflows/gpoint-native-release.yml",
    }
    jobs = {"jobs": [{"id": 99, "name": "Build and publish immutable images",
                      "status": "completed", "conclusion": "success"}]}
    commit = {"sha": sha, "commit": {"message": "ME-36 candidate\n\nGPOINT-ARTIFACT-PREPARE"}}
    digests = {"CORE": "sha256:" + "b" * 64, "EROS": "sha256:" + "c" * 64}
    logs = "\n".join([
        "2026-11-06T08:30:15.123Z ##[group]Run immutable image verification",
        *(f"2026-11-06T08:30:16.000Z GPOINT_IMAGE_DIGEST {component} "
          f"{MODULE.REPOSITORIES[component]}@{digest}"
          for component, digest in digests.items()),
        "2026-11-06T08:30:17.000Z ##[endgroup]",
    ]).encode()
    return run, jobs, logs, sha, digests, commit


def test_prepared_run_requires_successful_exact_sha_native_main_push_and_component_digests():
    run, jobs, logs, sha, digests, commit = fixture()
    assert MODULE.verify_prepared_run(run, jobs, logs, sha, digests, 1234, commit) == (
        99, "a" * 40 + "-1234-2"
    )


def test_prepared_run_rejects_wrong_or_untrusted_run_metadata():
    run, jobs, logs, sha, digests, commit = fixture()
    for field, value in (
        ("head_sha", "d" * 40), ("head_branch", "feature"),
        ("event", "pull_request"), ("conclusion", "failure"),
        ("path", ".github/workflows/other.yml"), ("id", 9999),
    ):
        bad_run = dict(run, **{field: value})
        try:
            MODULE.verify_prepared_run(bad_run, jobs, logs, sha, digests, 1234, commit)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid run {field}={value}")


def test_prepared_run_binds_version_to_exact_candidate_run_id_and_attempt():
    run, jobs, logs, sha, digests, commit = fixture()
    for candidate_sha, expected_run_id in (("d" * 40, 1234), (sha, 5678)):
        try:
            MODULE.verify_prepared_run(run, jobs, logs, candidate_sha, digests,
                                       expected_run_id, commit)
        except ValueError:
            continue
        raise AssertionError("accepted prepared image version with mismatched SHA or run ID")
    for bad_attempt in (None, 0, -1, "2"):
        try:
            MODULE.verify_prepared_run(dict(run, run_attempt=bad_attempt), jobs, logs,
                                       sha, digests, 1234, commit)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid prepared run attempt {bad_attempt!r}")


def test_cli_writes_verified_source_run_image_version_to_github_output():
    run, jobs, logs, sha, digests, commit = fixture()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, value in (("run.json", run), ("jobs.json", jobs), ("commit.json", commit),
                            ("digests.json", digests)):
            (root / name).write_text(json.dumps(value), encoding="utf-8")
        (root / "logs.txt").write_bytes(logs)
        output = root / "github-output"
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/verify_native_prepared_run.py"),
            "--run", str(root / "run.json"), "--jobs", str(root / "jobs.json"),
            "--commit", str(root / "commit.json"), "--logs", str(root / "logs.txt"),
            "--candidate-sha", sha, "--run-id", "1234", "--digests",
            json.dumps(digests), "--output", str(output),
        ], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8") == f"image_version={sha}-1234-2\n"


def test_prepared_run_rejects_failed_build_and_digest_mismatch_or_duplicate_lines():
    run, jobs, logs, sha, digests, commit = fixture()
    failed_jobs = json.loads(json.dumps(jobs))
    failed_jobs["jobs"][0]["conclusion"] = "failure"
    for candidate_jobs, candidate_logs in (
        (failed_jobs, logs),
        (jobs, logs + b"\n2026-11-06T08:30:18Z GPOINT_IMAGE_DIGEST CORE ghcr.io/akhripunov/gpoint-runtime@sha256:" + b"d" * 64),
        (jobs, b"2026-11-06T08:30:18Z GPOINT_IMAGE_DIGEST CORE ghcr.io/akhripunov/gpoint-runtime@sha256:" + b"d" * 64),
    ):
        try:
            MODULE.verify_prepared_run(run, candidate_jobs, candidate_logs, sha, digests, 1234, commit)
        except ValueError:
            continue
        raise AssertionError("accepted a mismatched prepared build result")


def test_prepared_run_rejects_a_normal_deploy_commit_without_artifact_marker():
    run, jobs, logs, sha, digests, _ = fixture()
    ordinary_commit = {"commit": {"message": "ME-36 ordinary release"}}
    try:
        MODULE.verify_prepared_run(run, jobs, logs, sha, digests, 1234, ordinary_commit)
    except ValueError:
        return
    raise AssertionError("accepted an unmarked ordinary main release")


def test_unmarked_commit_is_accepted_only_for_exact_core_and_active_config_candidate():
    run, jobs, logs, sha, digests, _ = fixture()
    core_digest = {"CORE": digests["CORE"]}
    files = [
        {"filename": "apps/api/src/eros-response-prompt.js"},
        {"filename": "services/eros-engine/eros-layer7-staging-model-config.toml"},
        {"filename": "tests/test_layer7_staging_workflow.py"},
    ]
    commit = {"sha": sha, "commit": {"message": "fix(eros): release ME-36"}, "files": files}
    core_logs = f"GPOINT_IMAGE_DIGEST CORE {MODULE.REPOSITORIES['CORE']}@{digests['CORE']}\n".encode()
    assert MODULE.verify_prepared_run(run, jobs, core_logs, sha, core_digest, 1234, commit) == (
        99, sha + "-1234-2"
    )
    commit["files"].append({"filename": "apps/worker/src/main.ts"})
    with pytest.raises(ValueError, match="Core/config-only"):
        MODULE.verify_prepared_run(run, jobs, core_logs, sha, core_digest, 1234, commit)


def test_layer7_eros_prepared_run_is_bound_to_one_image_and_skipped_production_deploy():
    import pytest

    run, jobs, _, sha, digests, _ = fixture()
    commit = {"sha": sha, "commit": {"message": "Eros update\n\nGPOINT-LAYER7-EROS"}}
    eros_digest = {"EROS": digests["EROS"]}
    logs = f"GPOINT_IMAGE_DIGEST EROS {MODULE.REPOSITORIES['EROS']}@{digests['EROS']}\n".encode()
    deploy = {"name": "Kamal production Deploy and Verify", "conclusion": "skipped"}
    jobs["jobs"].append(deploy)
    assert MODULE.verify_prepared_run(run, jobs, logs, sha, eros_digest, 1234, commit,
                                      layer7_eros=True) == (99, sha + "-1234-2")
    with pytest.raises(ValueError, match="exactly the Eros"):
        MODULE.verify_prepared_run(run, jobs, logs, sha, digests, 1234, commit, layer7_eros=True)
    deploy["conclusion"] = "success"
    with pytest.raises(ValueError, match="skipped production"):
        MODULE.verify_prepared_run(run, jobs, logs, sha, eros_digest, 1234, commit, layer7_eros=True)


def test_layer7_readiness_uses_full_installed_sha_to_candidate_git_range_with_bootstrap_build(tmp_path):
    import hashlib
    import subprocess

    from layer7_release_scope import REQUIRED_ROLES

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    def write(path, content):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(message, changes):
        for path, content in changes.items():
            write(path, content)
        git("add", "--all")
        subprocess.run(["git", "commit", "-qm", message], cwd=tmp_path, check=True)
        return git("rev-parse", "HEAD")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    git("config", "user.name", "Layer7 test")
    git("config", "user.email", "layer7@example.test")
    migration = "CREATE TABLE fixture (id integer);\n"
    migration_sha = hashlib.sha256(migration.encode()).hexdigest()
    installed = commit("installed Layer7 baseline", {
        "apps/api/src/eros-response-prompt.js": "export default 'old';\n",
        "services/eros-engine/eros-layer7-staging-model-config.toml": "model = 'old'\n",
        "packages/db/prisma/migrations/20261116000000_fixture/migration.sql": migration,
    })
    commit("first squash: prompt and tests", {
        "apps/api/src/eros-response-prompt.js": "export default 'candidate';\n",
        "tests/test_layer7_staging_workflow.py": "# source change from first PR\n",
    })
    commit("second squash: operational config", {
        "services/eros-engine/eros-layer7-staging-model-config.toml": "model = 'qwen'\n",
        ".github/actions/layer7-deploy/action.yml": "# protected action contract\n",
    })
    commit("third squash: retained Eros Kamal identity contract", {
        ".kamal/hooks/pre-app-boot": "# check retained component identity\n",
        ".kamal/hooks/post-app-boot": "# verify retained component identity\n",
        "tests/test_kamal_post_app_boot_hook.py": "# hook regression\n",
    })
    candidate = commit("fourth squash: final test only", {
        "tests/test_native_prepared_run.py": "# tip commit does not contain runtime paths\n",
    })

    roles = {
        role: {"source": installed, "container": "a" * 64, "image": "sha256:" + "b" * 64,
               "digest": "sha256:" + "c" * 64, "version": installed + "-1-1", "restarts": "0"}
        for role in REQUIRED_ROLES
    }
    snapshot = {
        "roles": roles, "prisma": ["20261116000000_fixture=" + migration_sha],
        "sqlx": ["1=" + "e" * 96],
        "database": {"postgres": "f" * 64, "redis": "1" * 64},
    }
    all_digests = {name: "sha256:" + char * 64 for name, char in
                   (("CORE", "b"), ("AGATA", "d"), ("MINIAPP", "e"), ("EROS", "f"))}
    full_build_logs = "\n".join(
        f"GPOINT_IMAGE_DIGEST {name} {MODULE.REPOSITORIES[name]}@{digest}"
        for name, digest in all_digests.items()
    ).encode()
    run, jobs, _, _, _, _ = fixture()
    run.update(id=1234, head_sha=candidate, run_attempt=2)
    commit_payload = {"sha": candidate, "commit": {"message": "ME-59 candidate\nGPOINT-ARTIFACT-PREPARE"},
                     # Deliberately reflects only the tip squash commit and is not trusted for scope.
                     "files": [{"filename": "tests/test_native_prepared_run.py"}]}

    result = MODULE.verify_prepared_run(
        run, jobs, full_build_logs, candidate, {"CORE": all_digests["CORE"]}, 1234,
        commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
    )
    assert result == (99, candidate + "-1234-2")
    assert MODULE.verify_prepared_run(
        run, jobs, full_build_logs, candidate, {"CORE": all_digests["CORE"]}, 1234,
        commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
    ) == result

    with pytest.raises(ValueError, match="do not match"):
        MODULE.verify_prepared_run(
            run, jobs, full_build_logs, candidate, {"CORE": "sha256:" + "a" * 64}, 1234,
            commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
        )
    with pytest.raises(ValueError, match="exact lowercase Git SHA"):
        MODULE.verify_prepared_run(
            run, jobs, full_build_logs, "not-a-sha", {"CORE": all_digests["CORE"]}, 1234,
            commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
        )
    with pytest.raises(ValueError, match="exact SHA"):
        MODULE.verify_prepared_run(
            run, jobs, full_build_logs, "d" * 40, {"CORE": all_digests["CORE"]}, 1234,
            commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
        )
    with pytest.raises(ValueError, match="digest map is malformed"):
        MODULE.verify_prepared_run(
            run, jobs, full_build_logs, candidate, ["CORE"], 1234,
            commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
        )
    with pytest.raises(ValueError, match="invalid or duplicate digest"):
        bad_repository_logs = full_build_logs.replace(
            MODULE.REPOSITORIES["CORE"].encode(), MODULE.REPOSITORIES["AGATA"].encode(), 1
        )
        MODULE.verify_prepared_run(
            run, jobs, bad_repository_logs, candidate, {"CORE": all_digests["CORE"]}, 1234,
            commit_payload, layer7_core_config=True, layer7_inventory=snapshot, repo=tmp_path,
        )

    for path, label in (("apps/worker/src/main.ts", "Worker"),
                        ("apps/miniapp/src/main.tsx", "Mini App"),
                        ("services/agata/api/main.py", "AGATA")):
        subprocess.run(["git", "checkout", "-q", "-B", "off-scope-test", candidate],
                       cwd=tmp_path, check=True)
        off_scope = commit(f"off-scope {label} change", {path: "changed\n"})
        off_scope_run = dict(run, head_sha=off_scope)
        off_scope_payload = dict(commit_payload, sha=off_scope)
        with pytest.raises(ValueError, match="outside"):
            MODULE.verify_prepared_run(
                off_scope_run, jobs, full_build_logs, off_scope,
                {"CORE": all_digests["CORE"]}, 1234,
                off_scope_payload, layer7_core_config=True,
                layer7_inventory=snapshot, repo=tmp_path,
            )

    subprocess.run(["git", "checkout", "-q", candidate], cwd=tmp_path, check=True)

    unknown_inventory = json.loads(json.dumps(snapshot))
    unknown_inventory["roles"]["gpoint-untracked-service"] = dict(roles["gpoint-core-api"])
    with pytest.raises(ValueError, match="role inventory differs"):
        MODULE.verify_prepared_run(
            run, jobs, full_build_logs, candidate, {"CORE": all_digests["CORE"]}, 1234,
            commit_payload, layer7_core_config=True,
            layer7_inventory=unknown_inventory, repo=tmp_path,
        )
