import importlib.util
import io
import json
import sys
import zipfile
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
        "id": 1234, "run_attempt": 1, "head_sha": sha,
        "head_branch": "main", "event": "push", "status": "completed",
        "conclusion": "success", "path": ".github/workflows/gpoint-native-release.yml",
    }
    jobs = {"jobs": [{"id": 99, "name": "Build and publish immutable images",
                      "status": "completed", "conclusion": "success"}]}
    commit = {"commit": {"message": "ME-36 candidate\n\nGPOINT-ARTIFACT-PREPARE"}}
    digests = {"CORE": "sha256:" + "b" * 64, "EROS": "sha256:" + "c" * 64}
    lines = "\n".join(
        f"GPOINT_IMAGE_DIGEST {component} {MODULE.REPOSITORIES[component]}@{digest}"
        for component, digest in digests.items()
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("build.txt", lines)
    return run, jobs, stream.getvalue(), sha, digests, commit


def test_prepared_run_requires_successful_exact_sha_native_main_push_and_component_digests():
    run, jobs, logs, sha, digests, commit = fixture()
    assert MODULE.verify_prepared_run(run, jobs, logs, sha, digests, 1234, commit) == 99


def test_prepared_run_rejects_wrong_or_untrusted_run_metadata():
    run, jobs, logs, sha, digests, commit = fixture()
    for field, value in (
        ("head_sha", "d" * 40), ("head_branch", "feature"),
        ("event", "pull_request"), ("conclusion", "failure"),
        ("path", ".github/workflows/other.yml"),
    ):
        bad_run = dict(run, **{field: value})
        try:
            MODULE.verify_prepared_run(bad_run, jobs, logs, sha, digests, 1234, commit)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid run {field}={value}")


def test_prepared_run_rejects_failed_build_and_digest_mismatch_or_duplicate_lines():
    run, jobs, logs, sha, digests, commit = fixture()
    failed_jobs = json.loads(json.dumps(jobs))
    failed_jobs["jobs"][0]["conclusion"] = "failure"
    for candidate_jobs, candidate_logs in (
        (failed_jobs, logs),
        (jobs, logs + zip_with("GPOINT_IMAGE_DIGEST CORE ghcr.io/akhripunov/gpoint-runtime@sha256:" + "d" * 64)),
        (jobs, zip_with("GPOINT_IMAGE_DIGEST CORE ghcr.io/akhripunov/gpoint-runtime@sha256:" + "d" * 64)),
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


def zip_with(line):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("build.txt", line)
    return stream.getvalue()
