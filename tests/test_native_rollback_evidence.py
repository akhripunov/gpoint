import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("rollback_evidence", ROOT / "scripts/verify_native_rollback_evidence.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SHA = "a" * 40
DEPLOYMENT_ID = 81
RUN_ID = 91
VERSION = f"{SHA}-{RUN_ID}-1"
DIGESTS = {key: "sha256:" + value * 64 for key, value in {"core": "1", "miniapp": "2", "eros": "3"}.items()}
REPOS = {
    "core": "ghcr.io/akhripunov/gpoint-runtime",
    "miniapp": "ghcr.io/akhripunov/gpoint-miniapp",
    "eros": "ghcr.io/akhripunov/gpoint-eros",
}


def evidence(*, include_bot=True):
    services = []
    for role, component in MODULE.ROLE_IMAGES.items():
        if role == "gpoint-core-bot" and not include_bot:
            continue
        key = component.lower()
        services.append({
            "name": f"{role}-{VERSION}", "revision": SHA, "running": True,
            "repositoryDigest": f"{REPOS[key]}@{DIGESTS[key]}",
        })
    manifest = {
        "schema": "gpoint-kamal-production-manifest/v2", "operationMode": "ordinary", "result": "PASS",
        "candidate": {"gitSha": SHA, "appSourceSha": SHA, "attemptTag": VERSION,
                      "images": {key: f"{REPOS[key]}@{DIGESTS[key]}" for key in DIGESTS}},
        "attempt": {"githubDeploymentId": str(DEPLOYMENT_ID), "runId": str(RUN_ID), "runAttempt": "1"},
        "runtimeServices": services,
    }
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    deployment = {"id": DEPLOYMENT_ID, "sha": SHA, "environment": "production", "task": "deploy"}
    status = {
        "state": "success", "environment": "production", "created_at": "2026-01-01T00:00:00Z",
        "description": f"Kamal success SHA {SHA} manifest {hashlib.sha256(raw).hexdigest()}",
        "log_url": f"https://github.com/akhripunov/gpoint/actions/runs/{RUN_ID}",
    }
    run = {"id": RUN_ID, "run_attempt": 1, "head_sha": SHA, "head_branch": "main",
           "event": "push", "path": ".github/workflows/gpoint-native-release.yml", "conclusion": "success"}
    jobs = [{"name": "Build and publish immutable images", "conclusion": "success"}]
    return deployment, [status], run, jobs, raw


def verify(args, **overrides):
    deployment, statuses, run, jobs, manifest_bytes = args
    values = {"deployment": deployment, "statuses": statuses, "run": run, "jobs": jobs,
              "manifest_bytes": manifest_bytes, "expected_sha": SHA,
              "expected_deployment_id": DEPLOYMENT_ID, "repository": "akhripunov/gpoint"}
    values.update(overrides)
    return MODULE.verify_rollback_evidence(**values)


def test_rollback_roles_follow_the_five_role_three_image_architecture():
    assert set(MODULE.ROLE_IMAGES) == set(MODULE.ROLE_REPOSITORIES)
    assert set(MODULE.REPOSITORIES) == {"core", "miniapp", "eros"}
    assert len(MODULE.ROLE_IMAGES) == 5


def test_accepts_exact_successful_five_role_manifest_with_immutable_digests():
    result = verify(evidence())
    assert result["version"] == VERSION
    assert result["run_id"] == RUN_ID
    assert result["digests"] == DIGESTS
    assert set(result["component_sources"]) == {"core", "miniapp", "eros"}
    assert result["core_bot_enabled"] is True


def test_accepts_exact_rollback_manifest_without_disabled_core_bot():
    result = verify(evidence(include_bot=False))
    assert result["core_bot_enabled"] is False
    assert result["component_versions"]["core"] == VERSION
    assert result["component_sources"]["core"] == SHA


def test_accepts_native_manifest_status_url_with_exact_component_sources():
    args = evidence()
    status = dict(args[1][0])
    status.pop("description")
    status["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + hashlib.sha256(args[4]).hexdigest()
        + "&gpoint-core-source-sha=" + SHA + "&gpoint-miniapp-source-sha=" + SHA
        + "&gpoint-eros-source-sha=" + SHA
    )
    status["log_url"] = f"https://github.com/akhripunov/gpoint/actions/runs/{RUN_ID}/attempts/1"
    result = verify(args, statuses=[status])
    assert result["manifest_sha256"] == hashlib.sha256(args[4]).hexdigest()
    assert result["component_sources"]["eros"] == SHA


def test_rejects_agata_or_relay_roles_and_retired_source_keys():
    args = evidence()
    manifest = json.loads(args[4])
    manifest["runtimeServices"].append({
        "name": "gpoint-eros-relay-relay-" + VERSION, "revision": SHA, "running": True,
        "repositoryDigest": f"{REPOS['core']}@{DIGESTS['core']}",
    })
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    statuses = [dict(args[1][0], description=f"Kamal success SHA {SHA} manifest {hashlib.sha256(raw).hexdigest()}")]
    with __import__("pytest").raises(ValueError, match="unknown GPoint Kamal role"):
        verify(args, statuses=statuses, manifest_bytes=raw)

    status = dict(args[1][0])
    status["environment_url"] = (
        "https://gpoint.one/#gpoint-manifest-sha256=" + hashlib.sha256(args[4]).hexdigest()
        + "&gpoint-core-source-sha=" + SHA + "&gpoint-miniapp-source-sha=" + SHA
        + "&gpoint-eros-relay-source-sha=" + SHA
    )
    with __import__("pytest").raises(ValueError, match="unsupported fields"):
        verify(args, statuses=[status])


def test_rejects_ambiguous_or_failed_latest_deployment_status_and_tampering():
    args = evidence()
    failed = dict(args[1][0], state="failure", created_at="2026-01-02T00:00:00Z")
    with __import__("pytest").raises(ValueError, match="latest status"):
        verify(args, statuses=[args[1][0], failed])
    with __import__("pytest").raises(ValueError, match="does not match"):
        verify(args, manifest_bytes=args[4] + b" ")
    with __import__("pytest").raises(ValueError, match="no successful immutable"):
        verify(args, jobs=[])


def test_rejects_duplicate_runtime_roles():
    args = evidence()
    manifest = json.loads(args[4])
    manifest["runtimeServices"].append(dict(manifest["runtimeServices"][0]))
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    statuses = [dict(args[1][0], description=f"Kamal success SHA {SHA} manifest {hashlib.sha256(raw).hexdigest()}")]
    with __import__("pytest").raises(ValueError, match="duplicate"):
        verify(args, statuses=statuses, manifest_bytes=raw)
