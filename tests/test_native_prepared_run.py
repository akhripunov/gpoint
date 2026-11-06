import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    commit = {"commit": {"message": "ME-36 candidate\n\nGPOINT-ARTIFACT-PREPARE"}}
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
