#!/usr/bin/env python3
"""Validate that a rollback target is a previously verified Native production release."""

from __future__ import annotations

import hashlib
import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROLE_IMAGES = {
    "gpoint-core-api": "core",
    "gpoint-core-worker": "core",
    "gpoint-core-bot": "core",
    "gpoint-agata-api": "agata",
    "gpoint-agata-memory-worker": "agata",
    "gpoint-miniapp-web": "miniapp",
}
REPOSITORIES = {
    "core": "ghcr.io/akhripunov/gpoint-runtime",
    "agata": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "miniapp": "ghcr.io/akhripunov/gpoint-miniapp",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_rollback_evidence(*, deployment: dict[str, Any], statuses: list[dict[str, Any]],
                             run: dict[str, Any], jobs: list[dict[str, Any]],
                             manifest_bytes: bytes, expected_sha: str,
                             expected_deployment_id: int, repository: str) -> dict[str, Any]:
    if not SHA40.fullmatch(expected_sha):
        _fail("rollback target SHA is not exact")
    if (deployment.get("id") != expected_deployment_id or deployment.get("sha") != expected_sha
            or deployment.get("environment") != "production" or deployment.get("task") != "deploy"):
        _fail("selected GitHub Deployment does not identify the requested production release")
    if not statuses:
        _fail("selected deployment has no statuses")
    latest = max(statuses, key=lambda row: _time(row.get("created_at", "")))
    match = re.fullmatch(r"Kamal success SHA ([0-9a-f]{40}) manifest ([0-9a-f]{64})", latest.get("description", ""))
    environment_url_match = re.fullmatch(
        r"https://gpoint\.one/#gpoint-manifest-sha256=([0-9a-f]{64})",
        latest.get("environment_url", ""),
    )
    native_status = environment_url_match is not None
    if (latest.get("state") != "success" or latest.get("environment") != "production"
            or (not native_status and (not match or match.group(1) != expected_sha))):
        _fail("latest status is not a successful verified Kamal production deployment")
    manifest_sha = environment_url_match.group(1) if environment_url_match else match.group(2)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha:
        _fail("production runtime manifest does not match the successful Deployment status")
    log_url = latest.get("log_url", "")
    run_match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)(?:/attempts/([0-9]+))?", log_url)
    if not run_match or run_match.group(1).lower() != repository.lower():
        _fail("successful Deployment status is not linked to a repository workflow run")
    if (str(run.get("id")) != run_match.group(2) or run.get("head_sha") != expected_sha
            or run.get("head_branch") != "main" or run.get("event") not in {"push", "workflow_dispatch"}
            or run.get("path") != ".github/workflows/gpoint-native-release.yml"
            or run.get("conclusion") != "success"):
        _fail("linked run is not a successful Native Release for the target SHA")
    build_jobs = [job for job in jobs if job.get("name") == "Build and publish immutable images"]
    if len(build_jobs) != 1 or build_jobs[0].get("conclusion") != "success":
        _fail("linked successful release run has no successful immutable image build")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("rollback manifest is not valid JSON")
    candidate = manifest.get("candidate", {})
    attempt = manifest.get("attempt", {})
    if (manifest.get("schema") != "gpoint-kamal-production-manifest/v2"
            or manifest.get("operationMode") != "ordinary" or manifest.get("result") != "PASS"
            or candidate.get("gitSha") != expected_sha or candidate.get("appSourceSha") != expected_sha
            or attempt.get("githubDeploymentId") != str(expected_deployment_id)):
        _fail("manifest is not a passing ordinary release for the selected Deployment")
    version = candidate.get("attemptTag", "")
    attempt_run = attempt.get("runId", "")
    attempt_number = attempt.get("runAttempt", "")
    if (not str(attempt_run).isdigit() or int(attempt_run) != int(run_match.group(2))
            or not str(attempt_number).isdigit() or int(attempt_number) <= 0
            or int(run.get("run_attempt", 0)) != int(attempt_number)
            or (run_match.group(3) is not None and int(run_match.group(3)) != int(attempt_number))
            or version != f"{expected_sha}-{attempt_run}-{attempt_number}"):
        _fail("rollback target has no exact immutable build attempt tag")
    images = candidate.get("images", {})
    digests: dict[str, str] = {}
    for key, repo in REPOSITORIES.items():
        value = images.get(key, "")
        prefix = repo + "@sha256:"
        if not value.startswith(prefix) or not SHA256.fullmatch(value[len(prefix):]):
            _fail(f"manifest has no immutable {key} image digest")
        digests[key] = value[len(repo) + 1:]
    services = manifest.get("runtimeServices", [])
    suffix = "-" + version
    by_name = {}
    for item in services:
        name = item.get("name", "")
        if not name.endswith(suffix):
            _fail("manifest runtime role is attached to a different image version")
        role = name[:-len(suffix)]
        if role in by_name:
            _fail("manifest contains a duplicate GPoint Kamal role")
        by_name[role] = item
    if len(services) != len(ROLE_IMAGES) or set(by_name) != set(ROLE_IMAGES):
        _fail("manifest does not contain exactly the six GPoint Kamal roles")
    for name, image_key in ROLE_IMAGES.items():
        service = by_name[name]
        repo_digest = f"{REPOSITORIES[image_key]}@{digests[image_key]}"
        if (service.get("revision") != expected_sha or service.get("running") is not True
                or service.get("repositoryDigest") != repo_digest
                or service.get("name") != f"{name}-{version}"):
            _fail(f"manifest runtime identity is inconsistent for {name}")
    return {"target_sha": expected_sha, "version": version, "manifest_sha256": manifest_sha,
            "digests": digests, "run_id": int(run_match.group(2)),
            "run_attempt": int(attempt.get("runAttempt", 0)), "images": images}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("deployment", "statuses", "run", "jobs", "manifest", "sha", "repository"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--deployment-id", required=True, type=int)
    args = parser.parse_args()
    try:
        evidence = verify_rollback_evidence(
            deployment=json.loads(Path(args.deployment).read_text()),
            statuses=json.loads(Path(args.statuses).read_text()),
            run=json.loads(Path(args.run).read_text()),
            jobs=json.loads(Path(args.jobs).read_text()),
            manifest_bytes=Path(args.manifest).read_bytes(),
            expected_sha=args.sha, expected_deployment_id=args.deployment_id,
            repository=args.repository,
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"GPOINT_ROLLBACK_EVIDENCE=BLOCKED reason={error}", file=sys.stderr)
        return 1
    for key in ("target_sha", "version", "manifest_sha256", "run_id", "run_attempt"):
        print(f"{key}={evidence[key]}")
    for image_key, digest in evidence["digests"].items():
        print(f"{image_key}_digest={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
