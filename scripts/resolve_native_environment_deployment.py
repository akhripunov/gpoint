#!/usr/bin/env python3
"""Resolve the exact in-progress native production Environment Deployment for this run."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import subprocess
import sys


SHA40 = re.compile(r"^[0-9a-f]{40}$")
POSITIVE = re.compile(r"^[1-9][0-9]*$")


def _latest_status(statuses: list[dict]) -> dict:
    if not isinstance(statuses, list) or not statuses:
        raise ValueError("native production Deployment has no statuses")
    try:
        rows = [(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")), row)
                for row in statuses]
    except (KeyError, TypeError, ValueError):
        raise ValueError("native production Deployment status timestamp is invalid") from None
    newest = max(timestamp for timestamp, _ in rows)
    matches = [row for timestamp, row in rows if timestamp == newest]
    if len(matches) != 1:
        raise ValueError("native production Deployment has ambiguous latest statuses")
    return matches[0]


def _current_attempt_deploy_job_urls(jobs: list[dict], *, repository: str,
                                    run_id: str) -> set[str]:
    if not isinstance(jobs, list):
        raise ValueError("current workflow attempt job list is invalid")
    matches = set()
    for job in jobs:
        if (not isinstance(job, dict)
                or job.get("name") != "Kamal production Deploy and Verify"
                or job.get("status") != "in_progress"):
            continue
        identifier = job.get("id")
        if not isinstance(identifier, int) or identifier <= 0:
            continue
        expected = f"https://github.com/{repository}/actions/runs/{run_id}/job/{identifier}"
        if job.get("html_url") == expected:
            matches.add(expected)
    if len(matches) != 1:
        raise ValueError("current workflow attempt does not identify exactly one active production Deploy job")
    return matches


def resolve_native_deployment_id(deployments: list[dict], statuses_by_id: dict[int, list[dict]], *,
                                 candidate_sha: str, repository: str, run_id: str,
                                 run_attempt: str,
                                 current_attempt_job_urls: set[str] | None = None) -> int:
    if (not SHA40.fullmatch(candidate_sha or "") or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "")
            or not POSITIVE.fullmatch(str(run_id)) or not POSITIVE.fullmatch(str(run_attempt))):
        raise ValueError("exact candidate SHA, repository, run ID and attempt are required")
    run_urls = set(current_attempt_job_urls or ()) | {
        f"https://github.com/{repository}/actions/runs/{run_id}",
        f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}",
    }
    matching = []
    for deployment in deployments:
        identifier = deployment.get("id")
        if (not isinstance(identifier, int) or identifier <= 0
                or deployment.get("sha") != candidate_sha
                or deployment.get("environment") != "production" or deployment.get("task") != "deploy"
                or deployment.get("transient_environment") is not False):
            continue
        latest = _latest_status(statuses_by_id.get(identifier, []))
        if (latest.get("state") == "in_progress" and latest.get("environment") == "production"
                and latest.get("log_url") in run_urls):
            matching.append(identifier)
    if len(matching) != 1:
        raise ValueError("current run does not resolve to exactly one in-progress production Deployment")
    return matching[0]


def _gh_json(*args: str):
    try:
        result = subprocess.run(["gh", "api", "--paginate", "--slurp", *args],
                                check=True, text=True, capture_output=True)
        pages = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        raise ValueError("GitHub read-only Deployment lookup failed") from None
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub Deployment response shape is invalid")
    return [row for page in pages for row in page]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    args = parser.parse_args()
    try:
        route = f"repos/{args.repository}/deployments?sha={args.sha}&environment=production&task=deploy&per_page=100"
        deployments = _gh_json(route)
        job_routes = (f"repos/{args.repository}/actions/runs/{args.run_id}/attempts/"
                      f"{args.run_attempt}/jobs?per_page=100")
        job_urls = _current_attempt_deploy_job_urls(
            _gh_json(job_routes), repository=args.repository, run_id=args.run_id)
        status_rows = {}
        for row in deployments:
            identifier = row.get("id") if isinstance(row, dict) else None
            if isinstance(identifier, int) and identifier > 0 and row.get("sha") == args.sha:
                status_rows[identifier] = _gh_json(
                    f"repos/{args.repository}/deployments/{identifier}/statuses?per_page=100")
        deployment_id = resolve_native_deployment_id(
            deployments, status_rows, candidate_sha=args.sha, repository=args.repository,
            run_id=args.run_id, run_attempt=args.run_attempt,
            current_attempt_job_urls=job_urls)
    except ValueError as error:
        print(f"NATIVE_DEPLOYMENT=BLOCKED reason={error}", file=sys.stderr)
        return 1
    print(f"deployment_id={deployment_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
