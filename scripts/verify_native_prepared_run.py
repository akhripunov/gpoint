#!/usr/bin/env python3
"""Bind prepared component digests to a successful Native main push run."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

WORKFLOW_PATH = ".github/workflows/gpoint-native-release.yml"
BUILD_JOB_NAME = "Build and publish immutable images"
ARTIFACT_PREPARE_MARKER = "GPOINT-ARTIFACT-PREPARE"
REPOSITORIES = {
    "CORE": "ghcr.io/akhripunov/gpoint-runtime",
    "AGATA": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "MINIAPP": "ghcr.io/akhripunov/gpoint-miniapp",
    "EROS": "ghcr.io/akhripunov/gpoint-eros",
}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_LINE = re.compile(
    r"^GPOINT_IMAGE_DIGEST (CORE|AGATA|MINIAPP|EROS) "
    r"(ghcr\.io/akhripunov/[a-z0-9-]+)@(sha256:[0-9a-f]{64})$"
)


def verify_prepared_run(run: Any, jobs_payload: Any, logs_zip: bytes,
                        candidate_sha: str, digests: dict[str, str],
                        expected_run_id: int, commit_payload: Any) -> int:
    if not isinstance(run, dict):
        raise ValueError("prepared run metadata is malformed")
    expected_run = {
        "id": expected_run_id,
        "head_sha": candidate_sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "path": WORKFLOW_PATH,
    }
    if not SHA40.fullmatch(candidate_sha or ""):
        raise ValueError("candidate SHA is not an exact lowercase Git SHA")
    if not isinstance(expected_run_id, int) or expected_run_id <= 0:
        raise ValueError("prepared run ID is invalid")
    if any(run.get(field) != value for field, value in expected_run.items()):
        raise ValueError("prepared run is not a successful Native main push for this exact SHA")
    commit = commit_payload.get("commit") if isinstance(commit_payload, dict) else None
    message = commit.get("message") if isinstance(commit, dict) else None
    if (not isinstance(message, str)
            or not re.search(rf"(?m)^{re.escape(ARTIFACT_PREPARE_MARKER)}$", message)):
        raise ValueError("prepared run commit is not marked for artifact-only preparation")
    if not isinstance(digests, dict) or not digests or any(
            component not in REPOSITORIES or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest) for component, digest in digests.items()):
        raise ValueError("prepared image digest map is malformed")

    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("prepared run job list is malformed")
    builds = [job for job in jobs if isinstance(job, dict)
              and job.get("name") == BUILD_JOB_NAME]
    if len(builds) != 1:
        raise ValueError("prepared run must contain exactly one Native Build job")
    build = builds[0]
    if (build.get("status") != "completed" or build.get("conclusion") != "success"
            or not isinstance(build.get("id"), int)):
        raise ValueError("prepared Native Build job did not succeed")

    try:
        with zipfile.ZipFile(io.BytesIO(logs_zip)) as archive:
            lines = [line for name in archive.namelist() if not name.endswith("/")
                     for line in archive.read(name).decode("utf-8", "replace").splitlines()]
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("prepared Native Build logs are unavailable or invalid") from exc
    recorded: dict[str, str] = {}
    for line in lines:
        match = IMAGE_LINE.fullmatch(line.strip())
        if not match:
            continue
        component, repository, digest = match.groups()
        if repository != REPOSITORIES[component] or component in recorded:
            raise ValueError("prepared Native Build logs contain an invalid or duplicate digest line")
        recorded[component] = digest
    if recorded != digests:
        raise ValueError("prepared digest inputs do not match the successful Build job logs")
    return build["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--commit", required=True, type=Path)
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--digests", required=True,
                        help="JSON object mapping component names to OCI digests")
    args = parser.parse_args()
    try:
        build_job_id = verify_prepared_run(
            json.loads(args.run.read_text(encoding="utf-8")),
            json.loads(args.jobs.read_text(encoding="utf-8")),
            args.logs.read_bytes(), args.candidate_sha,
            json.loads(args.digests), args.run_id,
            json.loads(args.commit.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("PREPARED_RUN=FAIL reason=" + str(exc)) from None
    print(f"PREPARED_RUN=PASS run_id={args.run_id} build_job_id={build_job_id} candidate_sha={args.candidate_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
