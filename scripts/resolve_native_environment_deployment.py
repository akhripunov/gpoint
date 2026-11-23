#!/usr/bin/env python3
"""Read the verified latest successful native production Deployment baseline."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlparse


SHA40 = re.compile(r"^[0-9a-f]{40}$")
NATIVE_WORKFLOW_PATHS = {
    ".github/workflows/gpoint-release.yml",
    ".github/workflows/gpoint-native-release.yml",
}


def latest_deployment_status(statuses: list[dict]) -> dict:
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


def latest_successful_native_source_sha(deployments: list[dict], statuses_by_id: dict[int, list[dict]],
                                       runs_by_id: dict[int, dict], *, repository: str) -> str:
    """Return the newest verified successful Native Deploy SHA for cumulative checks."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise ValueError("repository identity is invalid")
    rows = []
    for deployment in deployments:
        if (not isinstance(deployment, dict)
                or deployment.get("environment") != "production"
                or deployment.get("task") != "deploy"
                or deployment.get("transient_environment") is not False):
            continue
        source_sha = deployment.get("sha")
        identifier = deployment.get("id")
        if not isinstance(source_sha, str) or not SHA40.fullmatch(source_sha):
            continue
        if not isinstance(identifier, int) or identifier <= 0:
            continue
        try:
            created = datetime.fromisoformat(deployment["created_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((created, identifier, source_sha))
    for _created, identifier, source_sha in sorted(rows, reverse=True):
        statuses = statuses_by_id.get(identifier, [])
        try:
            status = latest_deployment_status(statuses)
        except ValueError:
            continue
        if status.get("state") != "success":
            continue
        log_url = status.get("log_url", "")
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?",
            log_url if isinstance(log_url, str) else "",
        )
        if not match:
            continue
        run = runs_by_id.get(int(match.group(1)))
        workflow_path = run.get("path", "").split("@", 1)[0] if isinstance(run, dict) else ""
        if (not isinstance(run, dict) or run.get("head_sha") != source_sha
                or run.get("conclusion") != "success"
                or workflow_path not in NATIVE_WORKFLOW_PATHS):
            continue
        return source_sha
    raise ValueError("no successful verified Native production Deployment SHA was found")


def latest_successful_native_eros_source_sha(deployments: list[dict], statuses_by_id: dict[int, list[dict]],
                                             runs_by_id: dict[int, dict], *, repository: str) -> str:
    """Read the Eros source SHA recorded after Runtime Verify in the successful deployment URL."""
    source_sha = latest_successful_native_source_sha(
        deployments, statuses_by_id, runs_by_id, repository=repository)
    for deployment in sorted(
            [row for row in deployments if isinstance(row, dict) and row.get("sha") == source_sha],
            key=lambda row: row.get("created_at", ""), reverse=True):
        identifier = deployment.get("id")
        if not isinstance(identifier, int):
            continue
        try:
            status = latest_deployment_status(statuses_by_id.get(identifier, []))
        except ValueError:
            continue
        if status.get("state") != "success":
            continue
        log_url = status.get("log_url", "")
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?",
            log_url if isinstance(log_url, str) else "",
        )
        run = runs_by_id.get(int(match.group(1))) if match else None
        workflow_path = run.get("path", "").split("@", 1)[0] if isinstance(run, dict) else ""
        if (not isinstance(run, dict) or run.get("head_sha") != source_sha
                or run.get("conclusion") != "success" or workflow_path not in NATIVE_WORKFLOW_PATHS):
            continue
        environment_url = status.get("environment_url", "")
        if not isinstance(environment_url, str):
            continue
        values = parse_qs(urlparse(environment_url).fragment)
        candidates = values.get("gpoint-eros-source-sha", [])
        if len(candidates) == 1 and SHA40.fullmatch(candidates[0]):
            return candidates[0]
    return ""


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
    parser.add_argument("--repository", required=True)
    parser.add_argument("--latest-successful-native-sha", action="store_true")
    args = parser.parse_args()
    try:
        if not args.latest_successful_native_sha:
            raise ValueError("only verified successful production baseline lookup is supported")
        deployments = _gh_json(
            f"repos/{args.repository}/deployments?environment=production&task=deploy&per_page=100")
        valid_rows = [row for row in deployments if isinstance(row, dict)]
        valid_rows.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        for row in valid_rows:
            identifier = row.get("id")
            if not isinstance(identifier, int) or identifier <= 0:
                continue
            statuses = _gh_json(
                f"repos/{args.repository}/deployments/{identifier}/statuses?per_page=100")
            try:
                status = latest_deployment_status(statuses)
            except ValueError:
                continue
            if status.get("state") != "success":
                continue
            log_url = status.get("log_url", "")
            match = re.fullmatch(
                rf"https://github\.com/{re.escape(args.repository)}/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?",
                log_url if isinstance(log_url, str) else "",
            )
            if not match:
                continue
            try:
                result = subprocess.run(
                    ["gh", "api", f"repos/{args.repository}/actions/runs/{match.group(1)}"],
                    check=True, text=True, capture_output=True,
                )
                run = json.loads(result.stdout)
            except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
                continue
            try:
                source_sha = latest_successful_native_source_sha(
                    [row], {identifier: statuses}, {int(match.group(1)): run},
                    repository=args.repository)
            except ValueError:
                continue
            print(f"sha={source_sha}")
            eros_sha = latest_successful_native_eros_source_sha(
                [row], {identifier: statuses}, {int(match.group(1)): run},
                repository=args.repository)
            print(f"eros_source_sha={eros_sha}")
            return 0
        raise ValueError("no successful verified Native production Deployment SHA was found")
    except ValueError as error:
        print(f"NATIVE_DEPLOYMENT=BLOCKED reason={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
