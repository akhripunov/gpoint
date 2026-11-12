#!/usr/bin/env python3
"""Resolve a fail-closed Native Build plan from the latest verified Deployment."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from native_release_intent import (
    EXACT_SHA,
    classify_component_build_plan,
)


REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([1-9][0-9]*)(?:/attempts/([1-9][0-9]*)|/job/([1-9][0-9]*))?$"
)
MANIFEST_SHA = re.compile(r"^[0-9a-f]{64}$")
COMPONENT_SOURCE_KEYS = {
    "CORE": "gpoint-core-source-sha",
    "AGATA": "gpoint-agata-source-sha",
    "MINIAPP": "gpoint-miniapp-source-sha",
    "EROS": "gpoint-eros-source-sha",
}
REQUIRED_COMPONENTS = ("CORE", "AGATA", "MINIAPP")
SUPPORTED_TASKS = {"deploy", "rollback"}
ALL_COMPONENTS = ["CORE", "AGATA", "MINIAPP", "EROS"]
BOOTSTRAP_BUILD_COMPONENTS = ["CORE", "AGATA", "MINIAPP", "EROS"]


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("production Deployment timestamp is invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("production Deployment timestamp is invalid") from None
    if stamp.tzinfo is None:
        raise ValueError("production Deployment timestamp has no timezone")
    return stamp


def _latest_status(statuses: list[dict]) -> dict:
    if not isinstance(statuses, list) or not statuses:
        raise ValueError("latest production Deployment has no statuses")
    try:
        rows = [(_timestamp(row.get("created_at")), row) for row in statuses]
    except AttributeError:
        raise ValueError("production Deployment status is malformed") from None
    newest = max(stamp for stamp, _row in rows)
    matches = [row for stamp, row in rows if stamp == newest]
    if len(matches) != 1:
        raise ValueError("latest production Deployment status is ambiguous")
    return matches[0]


def _is_github_environment_job_record(deployment: dict) -> bool:
    """Recognize GitHub's native Environment record, including successful releases."""
    return (
        deployment.get("environment") == "production"
        and deployment.get("production_environment") is False
        and deployment.get("transient_environment") is False
        and deployment.get("task") == "deploy"
        and deployment.get("payload") == {}
    )


def _production_deployments(deployments: list[dict], statuses_by_id: dict | None = None) -> list[dict]:
    return [row for row in deployments
            if row.get("environment") == "production"
            and (not _is_github_environment_job_record(row)
                 or statuses_by_id is None
                 or statuses_by_id.get(row.get("id")))]


def _manifest_baseline(environment_url: object, *, deployment_task: str,
                        deployment_sha: str) -> dict | None:
    if not isinstance(environment_url, str) or not environment_url:
        return None
    parsed = urlparse(environment_url)
    if parsed.scheme != "https" or parsed.netloc != "gpoint.one" or parsed.path != "/":
        raise ValueError("production Deployment URL is not the verified GPoint manifest URL")
    pairs = parse_qsl(parsed.fragment, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("production Deployment URL contains duplicate manifest fields")
        values[key] = value
    digest = values.get("gpoint-manifest-sha256", "")
    if not MANIFEST_SHA.fullmatch(digest):
        raise ValueError("production Deployment manifest digest is invalid")
    source_keys = set(COMPONENT_SOURCE_KEYS.values())
    required_source_keys = {COMPONENT_SOURCE_KEYS[name] for name in REQUIRED_COMPONENTS}
    supported_keys = source_keys | {"gpoint-manifest-sha256", "gpoint-runtime-target-sha"}
    if set(values) - supported_keys:
        raise ValueError("production Deployment URL contains unsupported manifest fields")
    present_sources = source_keys.intersection(values)
    if not present_sources:
        return None
    if not required_source_keys.issubset(present_sources):
        raise ValueError("production Deployment component source evidence is incomplete")
    sources = {component: values[key] for component, key in COMPONENT_SOURCE_KEYS.items()
               if key in values}
    # Existing V3 manifests predate Eros. Until the first Eros deployment, use
    # the Core baseline to classify the introduction of the Eros component.
    sources.setdefault("EROS", sources["CORE"])
    if any(not EXACT_SHA.fullmatch(source) for source in sources.values()):
        raise ValueError("production Deployment component source SHA is invalid")
    runtime_target_sha = values.get("gpoint-runtime-target-sha")
    if runtime_target_sha is not None and not EXACT_SHA.fullmatch(runtime_target_sha):
        raise ValueError("production Deployment runtime target SHA is invalid")
    if deployment_task == "rollback" and set(sources.values()) != {deployment_sha}:
        raise ValueError("rollback Deployment component sources differ from its exact target SHA")
    result = {"manifest_sha256": digest, "component_sources": sources}
    if runtime_target_sha is not None:
        result["runtime_target_sha"] = runtime_target_sha
    return result


def resolve_production_component_sources(deployments: list[dict], statuses_by_id: dict,
                                         runs_by_id: dict, *, repository: str,
                                         jobs_by_id: dict | None = None) -> dict:
    """Validate latest Deployment/Verify identity and extract source SHAs."""
    if not REPOSITORY.fullmatch(repository or ""):
        raise ValueError("repository identity is invalid")
    if not isinstance(deployments, list):
        raise ValueError("production Deployment collection is malformed")
    if any(not isinstance(row, dict) for row in deployments):
        raise ValueError("production Deployment collection is malformed")
    if not deployments:
        return {"status": "BOOTSTRAP_FULL", "reason": "production_component_baseline_unavailable"}
    eligible = _production_deployments(deployments, statuses_by_id)
    if not eligible:
        raise ValueError("production Deployment collection has no production record")
    newest = max(_timestamp(row.get("created_at")) for row in eligible)
    latest_rows = [row for row in eligible if _timestamp(row.get("created_at")) == newest]
    if len(latest_rows) != 1:
        raise ValueError("latest production Deployment is ambiguous")
    deployment = latest_rows[0]
    identifier = deployment.get("id")
    target_sha = deployment.get("sha")
    task = deployment.get("task")
    if (not isinstance(identifier, int) or identifier <= 0
            or not isinstance(target_sha, str) or not EXACT_SHA.fullmatch(target_sha)
            or (deployment.get("production_environment") is not True
                and not _is_github_environment_job_record(deployment))
            or deployment.get("transient_environment") is not False
            or task not in SUPPORTED_TASKS):
        raise ValueError("latest production Deployment identity is unsupported or malformed")
    status = _latest_status(statuses_by_id.get(identifier, []))
    failed_native = _is_github_environment_job_record(deployment) and status.get("state") in {"failure", "error"}
    if (status.get("state") != "success" and not failed_native) or status.get("environment") != "production":
        raise ValueError("latest production Deployment is not successful")
    expected_conclusion = "failure" if failed_native else "success"
    match = RUN_URL.fullmatch(status.get("log_url", ""))
    if not match or match.group(1) != repository:
        raise ValueError("latest production Deployment is not linked to a GPoint Actions run")
    run_id, url_attempt = match.group(2), match.group(3)
    run = runs_by_id.get(run_id, runs_by_id.get(int(run_id)))
    if not isinstance(run, dict):
        raise ValueError("linked production Actions run evidence is missing")
    run_attempt = str(run.get("run_attempt", ""))
    if (run.get("path") != ".github/workflows/gpoint-native-release.yml"
            or run.get("status") != "completed" or run.get("conclusion") != expected_conclusion
            or run.get("head_branch") != "main"
            or not re.fullmatch(r"[1-9][0-9]*", run_attempt)
            or (url_attempt is not None and url_attempt != run_attempt)):
        raise ValueError("linked production run did not pass the canonical Native workflow")
    job_id = match.group(4)
    if job_id is not None:
        job = (jobs_by_id or {}).get(int(job_id))
        if (not isinstance(job, dict) or str(job.get("run_id")) != run_id
                or str(job.get("run_attempt")) != run_attempt
                or job.get("status") != "completed" or job.get("conclusion") != expected_conclusion):
            raise ValueError("linked production job does not match its successful run attempt")
    if task == "deploy":
        if (run.get("head_sha") != target_sha
                or run.get("event") not in {"push", "workflow_dispatch"}):
            raise ValueError("Native Deployment SHA differs from its successful workflow run")
    elif task == "rollback":
        if run.get("event") != "workflow_dispatch":
            raise ValueError("rollback Deployment is not linked to an exact manual run")

    if failed_native:
        return {"status": "BOOTSTRAP_FULL", "deployment_id": identifier,
                "reason": "failed_native_attempt_requires_live_reconciliation"}

    baseline = _manifest_baseline(status.get("environment_url"),
                                  deployment_task=task, deployment_sha=target_sha)
    if baseline is None:
        return {"status": "BOOTSTRAP_FULL", "deployment_id": identifier,
                "reason": "successful_legacy_deployment_has_no_component_source_manifest"}
    return {"status": "VERIFIED", "deployment_id": identifier, **baseline}


def resolve_build_plan(repo: Path, candidate_sha: str, deployments: list[dict],
                       statuses_by_id: dict, runs_by_id: dict, *, repository: str,
                       jobs_by_id: dict | None = None) -> dict:
    if not EXACT_SHA.fullmatch(candidate_sha or ""):
        raise ValueError("candidate SHA must be exact")
    repo = repo.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                               text=True).strip() != candidate_sha:
        raise ValueError("candidate SHA must equal the checked-out source")
    if subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                               cwd=repo, text=True).strip():
        raise ValueError("build candidate worktree must be clean")
    baseline = resolve_production_component_sources(
        deployments, statuses_by_id, runs_by_id, repository=repository, jobs_by_id=jobs_by_id)
    if baseline["status"] == "BOOTSTRAP_FULL":
        # Without a proven component baseline, no image identity can be assumed.
        # Build every runtime image: the Deploy job's fresh live inventory then
        # narrows the Kamal scope to the components that must actually change.
        return {
            "status": "BOOTSTRAP_FULL",
            "deploy_scope": "UNRESOLVED",
            "build_components": BOOTSTRAP_BUILD_COMPONENTS,
            "reason": baseline["reason"],
        }
    plan = classify_component_build_plan(repo, candidate_sha,
                                         baseline["component_sources"])
    return {
        "status": "VERIFIED",
        "deployment_id": baseline["deployment_id"],
        "manifest_sha256": baseline["manifest_sha256"],
        "baseline_sources": baseline["component_sources"],
        **plan,
    }


def _gh_json(*args: str):
    try:
        result = subprocess.run(["gh", "api", "--paginate", "--slurp", *args],
                                check=True, text=True, capture_output=True)
        pages = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        raise ValueError("GitHub read-only production Deployment lookup failed") from None
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub production Deployment response is malformed")
    return [row for page in pages for row in page]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        deployments = _gh_json(
            f"repos/{args.repository}/deployments?environment=production&per_page=100")
        statuses_by_id = {}
        if any(not isinstance(row, dict) for row in deployments):
            raise ValueError("production Deployment collection is malformed")
        eligible = _production_deployments(deployments)
        # A failed canonical attempt invalidates the older manifest baseline.
        # Build conservatively; fresh live inventory still controls Kamal scope.
        baseline_time = None
        for row in sorted(eligible, key=lambda row: _timestamp(row.get("created_at")), reverse=True):
            row_time = _timestamp(row.get("created_at"))
            if baseline_time is not None and row_time < baseline_time:
                break
            identifier = row.get("id")
            if not isinstance(identifier, int) or identifier <= 0:
                raise ValueError("latest production Deployment identity is unsupported or malformed")
            statuses_by_id[identifier] = _gh_json(
                f"repos/{args.repository}/deployments/{identifier}/statuses?per_page=100")
            baseline_time = row_time
        runs_by_id = {}
        jobs_by_id = {}
        for statuses in statuses_by_id.values():
            status = _latest_status(statuses)
            if status.get("state") not in {"success", "failure", "error"}:
                continue
            match = RUN_URL.fullmatch(status.get("log_url", ""))
            if match:
                run_id = match.group(2)
                if run_id not in runs_by_id:
                    result = subprocess.run(
                        ["gh", "api", f"repos/{args.repository}/actions/runs/{run_id}"],
                        check=True, text=True, capture_output=True)
                    runs_by_id[run_id] = json.loads(result.stdout)
                job_id = match.group(4)
                if job_id is not None and int(job_id) not in jobs_by_id:
                    result = subprocess.run(
                        ["gh", "api", f"repos/{args.repository}/actions/jobs/{job_id}"],
                        check=True, text=True, capture_output=True)
                    jobs_by_id[int(job_id)] = json.loads(result.stdout)
        plan = resolve_build_plan(args.repo, args.candidate_sha, deployments,
                                  statuses_by_id, runs_by_id,
                                  repository=args.repository,
                                  jobs_by_id=jobs_by_id)
    except (ValueError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        reason = str(error) if isinstance(error, ValueError) else "GitHub production baseline lookup failed"
        print(json.dumps({"status": "BLOCK", "reason": reason}, sort_keys=True))
        return 2
    print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
