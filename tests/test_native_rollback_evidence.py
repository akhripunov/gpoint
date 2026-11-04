import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rollback_evidence", ROOT / "scripts/verify_native_rollback_evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SHA = "a" * 40
DEPLOYMENT_ID = 81
RUN_ID = 91
DIGESTS = {
    "core": "sha256:" + "1" * 64,
    "agata": "sha256:" + "2" * 64,
    "miniapp": "sha256:" + "3" * 64,
}
REPOS = {
    "core": "ghcr.io/akhripunov/gpoint-runtime",
    "agata": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "miniapp": "ghcr.io/akhripunov/gpoint-miniapp",
}
VERSION = f"{SHA}-{RUN_ID}-1"


def evidence():
    services = []
    for name, key in MODULE.ROLE_IMAGES.items():
        services.append({
            "name": f"{name}-{VERSION}",
            "revision": SHA,
            "running": True,
            "repositoryDigest": f"{REPOS[key]}@{DIGESTS[key]}",
        })
    manifest = {
        "schema": "gpoint-kamal-production-manifest/v2",
        "operationMode": "ordinary",
        "result": "PASS",
        "candidate": {
            "gitSha": SHA,
            "appSourceSha": SHA,
            "attemptTag": VERSION,
            "images": {key: f"{REPOS[key]}@{DIGESTS[key]}" for key in DIGESTS},
        },
        "attempt": {
            "githubDeploymentId": str(DEPLOYMENT_ID),
            "runId": str(RUN_ID),
            "runAttempt": "1",
        },
        "runtimeServices": services,
    }
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    deployment = {
        "id": DEPLOYMENT_ID,
        "sha": SHA,
        "environment": "production",
        "task": "deploy",
    }
    status = {
        "state": "success",
        "environment": "production",
        "created_at": "2026-01-01T00:00:00Z",
        "description": f"Kamal success SHA {SHA} manifest {hashlib.sha256(raw).hexdigest()}",
        "log_url": f"https://github.com/akhripunov/gpoint/actions/runs/{RUN_ID}",
    }
    run = {
        "id": RUN_ID,
        "run_attempt": 1,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/gpoint-native-release.yml",
        "conclusion": "success",
    }
    jobs = [{"name": "Build and publish immutable images", "conclusion": "success"}]
    return deployment, [status], run, jobs, raw


def mixed_component_evidence():
    args = evidence()
    deployment, statuses, run, jobs, raw = args
    manifest = json.loads(raw)
    sources = {"core": SHA, "agata": "b" * 40, "miniapp": "c" * 40}
    versions = {"core": VERSION, "agata": f"{sources['agata']}-92-1",
                "miniapp": f"{sources['miniapp']}-93-2"}
    for service in manifest["runtimeServices"]:
        name = service["name"][:-len("-" + VERSION)]
        if name in {"gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"}:
            component = "core"
        elif name in {"gpoint-agata-api", "gpoint-agata-memory-worker"}:
            component = "agata"
        else:
            component = "miniapp"
        source = sources[component]
        version = versions[component]
        service["name"] = f"{name}-{version}"
        service["version"] = version
        service["revision"] = source
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    statuses[0].pop("description")
    statuses[0]["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + hashlib.sha256(raw).hexdigest()
        + "&gpoint-runtime-target-sha=" + SHA
        + "&gpoint-core-source-sha=" + sources["core"]
        + "&gpoint-agata-source-sha=" + sources["agata"]
        + "&gpoint-miniapp-source-sha=" + sources["miniapp"]
    )
    statuses[0]["log_url"] = f"https://github.com/akhripunov/gpoint/actions/runs/{RUN_ID}/attempts/1"
    run["run_attempt"] = 1
    return deployment, statuses, run, jobs, raw


def verify(args, **overrides):
    deployment, statuses, run, jobs, manifest_bytes = args
    values = {
        "deployment": deployment,
        "statuses": statuses,
        "run": run,
        "jobs": jobs,
        "manifest_bytes": manifest_bytes,
        "expected_sha": SHA,
        "expected_deployment_id": DEPLOYMENT_ID,
        "repository": "akhripunov/gpoint",
    }
    values.update(overrides)
    return MODULE.verify_rollback_evidence(**values)


def test_accepts_exact_successful_release_manifest_and_all_six_role_digests():
    result = verify(evidence())
    assert result["version"] == VERSION
    assert result["run_id"] == RUN_ID


def test_accepts_verified_rollback_target_with_mixed_component_versions_and_sources():
    result = verify(mixed_component_evidence())

    assert result["component_versions"] == {
        "core": VERSION,
        "agata": "b" * 40 + "-92-1",
        "miniapp": "c" * 40 + "-93-2",
    }
    assert result["component_sources"] == {
        "core": SHA, "agata": "b" * 40, "miniapp": "c" * 40,
    }


def test_rejects_native_manifest_when_environment_component_sources_disagree():
    args = mixed_component_evidence()
    args[1][0]["environment_url"] = args[1][0]["environment_url"].replace(
        "gpoint-agata-source-sha=" + "b" * 40,
        "gpoint-agata-source-sha=" + "d" * 40,
    )

    try:
        verify(args)
    except ValueError as exc:
        assert "manifest component sources" in str(exc)
    else:
        raise AssertionError("Deployment URL with different component sources was accepted")


def test_accepts_native_environment_status_with_manifest_url_and_exact_attempt_run_link():
    args = evidence()
    status = dict(args[1][0])
    status.pop("description")
    status["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + hashlib.sha256(args[4]).hexdigest()
        + "&gpoint-core-source-sha=" + SHA
        + "&gpoint-agata-source-sha=" + SHA
        + "&gpoint-miniapp-source-sha=" + SHA
    )
    status["log_url"] = f"https://github.com/akhripunov/gpoint/actions/runs/{RUN_ID}/attempts/1"
    result = verify(args, statuses=[status])
    assert result["manifest_sha256"] == hashlib.sha256(args[4]).hexdigest()
    assert result["run_attempt"] == 1


def test_rejects_failed_or_newer_non_success_deployment_status():
    args = evidence()
    args[1].append({
        "state": "failure",
        "created_at": "2026-01-02T00:00:00Z",
        "description": "failed",
        "log_url": args[1][0]["log_url"],
    })
    try:
        verify(args)
    except ValueError as exc:
        assert "latest status" in str(exc)
    else:
        raise AssertionError("non-success latest status was accepted")


def test_rejects_manifest_tamper_target_sha_missing_build_and_attempt_mismatch():
    args = evidence()
    invalid_inputs = (
        {"manifest_bytes": args[4] + b" "},
        {"expected_sha": "b" * 40},
        {"jobs": []},
        {"run": {**args[2], "run_attempt": 2}},
    )
    for overrides in invalid_inputs:
        try:
            verify(args, **overrides)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe target accepted: {overrides.keys()}")


def test_rejects_duplicate_role_in_production_manifest():
    args = evidence()
    manifest = json.loads(args[4])
    manifest["runtimeServices"].append(dict(manifest["runtimeServices"][0]))
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    statuses = [dict(
        args[1][0],
        description=f"Kamal success SHA {SHA} manifest {hashlib.sha256(raw).hexdigest()}",
    )]
    try:
        verify(args, statuses=statuses, manifest_bytes=raw)
    except ValueError as exc:
        assert "duplicate GPoint Kamal role" in str(exc)
    else:
        raise AssertionError("duplicate role accepted")
