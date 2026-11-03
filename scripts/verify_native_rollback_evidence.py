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
from urllib.parse import parse_qsl, urlparse

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
    environment_url = latest.get("environment_url", "")
    native_manifest_sha = None
    native_sources = None
    parsed_url = urlparse(environment_url)
    if (parsed_url.scheme == "https" and parsed_url.netloc == "gpoint.one"
            and parsed_url.path == "/" and parsed_url.fragment):
        try:
            pairs = parse_qsl(parsed_url.fragment, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            _fail("native Deployment URL metadata is malformed")
        values = dict(pairs)
        if len(values) != len(pairs):
            _fail("native Deployment URL metadata contains duplicate fields")
        source_keys = {"gpoint-core-source-sha", "gpoint-agata-source-sha", "gpoint-miniapp-source-sha"}
        supported_keys = source_keys | {"gpoint-manifest-sha256", "gpoint-runtime-target-sha"}
        if set(values) - supported_keys:
            _fail("native Deployment URL metadata contains unsupported fields")
        native_manifest_sha = values.get("gpoint-manifest-sha256")
        if not SHA256.fullmatch(native_manifest_sha or ""):
            _fail("native Deployment URL manifest SHA is invalid")
        present_sources = source_keys.intersection(values)
        if present_sources and present_sources != source_keys:
            _fail("native Deployment URL component source evidence is incomplete")
        if present_sources:
            native_sources = {
                "core": values["gpoint-core-source-sha"],
                "agata": values["gpoint-agata-source-sha"],
                "miniapp": values["gpoint-miniapp-source-sha"],
            }
            if any(not SHA40.fullmatch(value) for value in native_sources.values()):
                _fail("native Deployment URL component source SHA is invalid")
        runtime_target = values.get("gpoint-runtime-target-sha")
        if runtime_target is not None and not SHA40.fullmatch(runtime_target):
            _fail("native Deployment URL runtime target SHA is invalid")
    native_status = native_manifest_sha is not None
    if (latest.get("state") != "success" or latest.get("environment") != "production"
            or (not native_status and (not match or match.group(1) != expected_sha))):
        _fail("latest status is not a successful verified Kamal production deployment")
    manifest_sha = native_manifest_sha if native_status else match.group(2)
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
    by_name = {}
    for item in services:
        name = item.get("name", "")
        role = next((candidate for candidate in ROLE_IMAGES
                     if name.startswith(candidate + "-")), None)
        if role is None:
            _fail("manifest contains an unknown GPoint Kamal role")
        role_version = name[len(role) + 1:]
        if role in by_name:
            _fail("manifest contains a duplicate GPoint Kamal role")
        by_name[role] = (item, role_version)
    if len(services) != len(ROLE_IMAGES) or set(by_name) != set(ROLE_IMAGES):
        _fail("manifest does not contain exactly the six GPoint Kamal roles")
    component_versions = {}
    component_sources = {}
    component_digests = {}
    for name, image_key in ROLE_IMAGES.items():
        service, role_version = by_name[name]
        version_match = re.fullmatch(r"([0-9a-f]{40})-([1-9][0-9]*)-([1-9][0-9]*)", role_version)
        if not version_match:
            _fail(f"manifest runtime version is invalid for {name}")
        source_sha = service.get("revision")
        if source_sha != version_match.group(1):
            _fail(f"manifest runtime source SHA differs from its version for {name}")
        repo_digest = f"{REPOSITORIES[image_key]}@{digests[image_key]}"
        if (not SHA40.fullmatch(source_sha or "") or service.get("running") is not True
                or service.get("repositoryDigest") != repo_digest
                or service.get("name") != f"{name}-{role_version}"):
            _fail(f"manifest runtime identity is inconsistent for {name}")
        component_versions[image_key] = role_version
        component_sources[image_key] = source_sha
        component_digests[image_key] = repo_digest
        if service.get("component") not in (None, image_key.upper()):
            _fail(f"manifest runtime component is inconsistent for {name}")
    for component, role_names in {
        "core": ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
        "agata": ("gpoint-agata-api", "gpoint-agata-memory-worker"),
        "miniapp": ("gpoint-miniapp-web",),
    }.items():
        identities = {(by_name[name][1], by_name[name][0].get("revision"),
                       by_name[name][0].get("repositoryDigest")) for name in role_names}
        if len(identities) != 1:
            _fail(f"manifest shared component roles have inconsistent identities for {component}")
    if native_sources is not None and native_sources != component_sources:
        _fail("native Deployment manifest component sources do not match runtimeServices")
    return {"target_sha": expected_sha, "version": version, "manifest_sha256": manifest_sha,
            "digests": digests, "run_id": int(run_match.group(2)),
            "run_attempt": int(attempt.get("runAttempt", 0)), "images": images,
            "component_versions": component_versions, "component_sources": component_sources}


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
    for component, version in evidence["component_versions"].items():
        print(f"{component}_version={version}")
        print(f"{component}_source_sha={evidence['component_sources'][component]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
