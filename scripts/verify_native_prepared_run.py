#!/usr/bin/env python3
"""Bind prepared component digests to a successful Native main push run."""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

WORKFLOW_PATH = ".github/workflows/gpoint-native-release.yml"
BUILD_JOB_NAME = "Build and publish immutable images"
ARTIFACT_PREPARE_MARKER = "GPOINT-ARTIFACT-PREPARE"
LAYER7_EROS_MARKER = "GPOINT-LAYER7-EROS"
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


def _job_log_lines(log_bytes: bytes) -> list[str]:
    if log_bytes.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(log_bytes)) as archive:
                return [line for name in archive.namelist() if not name.endswith("/")
                        for line in archive.read(name).decode("utf-8", "replace").splitlines()]
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("prepared Native Build log archive is invalid") from exc
    try:
        return log_bytes.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("prepared Native Build logs are not UTF-8 text") from exc


def _strip_github_timestamp(line: str) -> str:
    return re.sub(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+",
        "", line.strip(), count=1,
    )


def verify_prepared_run(run: Any, jobs_payload: Any, logs: bytes,
                        candidate_sha: str, digests: dict[str, str],
                        expected_run_id: int, commit_payload: Any, *,
                        layer7_eros: bool = False,
                        layer7_core_config: bool = False,
                        layer7_inventory: dict | None = None,
                        repo: Path = Path.cwd()) -> tuple[int, str]:
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
    run_attempt = run.get("run_attempt")
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        raise ValueError("prepared run attempt is missing or invalid")
    commit = commit_payload.get("commit") if isinstance(commit_payload, dict) else None
    if not isinstance(commit_payload, dict) or commit_payload.get("sha") != candidate_sha:
        raise ValueError("prepared commit payload SHA does not match the exact candidate SHA")
    if layer7_core_config and layer7_eros:
        raise ValueError("Layer7 Core/config-only scope conflicts with another prepared scope")
    message = commit.get("message") if isinstance(commit, dict) else None
    if (layer7_core_config and isinstance(message, str)
            and LAYER7_EROS_MARKER in message.splitlines()):
        raise ValueError("Layer7 Core/config-only scope conflicts with an Eros-only candidate")
    marker = LAYER7_EROS_MARKER if layer7_eros else ARTIFACT_PREPARE_MARKER
    marked = isinstance(message, str) and marker in message.splitlines()
    if not marked and not layer7_core_config:
        if layer7_eros or not isinstance(commit_payload, dict):
            raise ValueError("prepared run commit is not marked for artifact-only preparation")
        files = commit_payload.get("files")
        paths = {row.get("filename") for row in files or [] if isinstance(row, dict)}
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from native_release_intent import ARTIFACT_PREPARE_PATHS
        required = {
            "apps/api/src/eros-response-prompt.js",
            "services/eros-engine/eros-layer7-staging-model-config.toml",
        }
        if (not files or not paths <= ARTIFACT_PREPARE_PATHS or not required <= paths
                or set(digests) != {"CORE"}):
            raise ValueError("unmarked prepared candidate is not the exact Core/config-only scope")
    if layer7_core_config:
        if layer7_eros or not isinstance(commit_payload, dict):
            raise ValueError("Layer7 Core/config-only scope conflicts with another prepared scope")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        if not isinstance(layer7_inventory, dict) or set(digests) != {"CORE"}:
            raise ValueError("prepared candidate is not the exact Core/config-only scope")
        from layer7_release_scope import core_config_plan_details
        api = layer7_inventory.get("roles", {}).get("gpoint-core-api", {})
        installed_core_sha = api.get("source") if isinstance(api, dict) else None
        try:
            core_config_plan_details(repo, candidate_sha, layer7_inventory,
                                     installed_core_sha=installed_core_sha, verify_schema=True)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            raise ValueError("prepared candidate is not the exact Core/config-only scope: " + str(exc)) from None
    if not isinstance(digests, dict) or not digests or any(
            component not in REPOSITORIES or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest) for component, digest in digests.items()):
        raise ValueError("prepared image digest map is malformed")

    if layer7_eros and set(digests) != {"EROS"}:
        raise ValueError("Layer7 Eros preparation requires exactly the Eros digest")

    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("prepared run job list is malformed")
    if layer7_eros:
        deploys = [job for job in jobs if isinstance(job, dict)
                   and job.get("name") == "Kamal production Deploy and Verify"]
        if len(deploys) != 1 or deploys[0].get("conclusion") != "skipped":
            raise ValueError("prepared Eros run must have skipped production Deploy")

    builds = [job for job in jobs if isinstance(job, dict)
              and job.get("name") == BUILD_JOB_NAME]
    if len(builds) != 1:
        raise ValueError("prepared run must contain exactly one Native Build job")
    build = builds[0]
    if (build.get("status") != "completed" or build.get("conclusion") != "success"
            or not isinstance(build.get("id"), int)):
        raise ValueError("prepared Native Build job did not succeed")

    lines = _job_log_lines(logs)
    recorded: dict[str, str] = {}
    for line in lines:
        normalized = _strip_github_timestamp(line)
        match = IMAGE_LINE.fullmatch(normalized)
        if not match:
            if normalized.startswith("GPOINT_IMAGE_DIGEST"):
                raise ValueError("prepared Native Build logs contain a malformed digest record")
            continue
        component, repository, digest = match.groups()
        if repository != REPOSITORIES[component] or component in recorded:
            raise ValueError("prepared Native Build logs contain an invalid or duplicate digest line")
        recorded[component] = digest
    if layer7_core_config:
        if set(digests) != {"CORE"} or recorded.get("CORE") != digests["CORE"]:
            raise ValueError("prepared digest inputs do not match the successful Build job logs")
    elif recorded != digests:
        raise ValueError("prepared digest inputs do not match the successful Build job logs")
    return build["id"], f"{candidate_sha}-{expected_run_id}-{run_attempt}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer7-eros", action="store_true")
    parser.add_argument("--layer7-core-config", action="store_true")
    parser.add_argument("--layer7-inventory", type=Path,
                        help="Read-only Layer7 inventory collected from the protected target")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--commit", required=True, type=Path)
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--output", type=Path,
                        help="GitHub Actions output file for the verified image version")
    parser.add_argument("--digests", required=True,
                        help="JSON object mapping component names to OCI digests")
    args = parser.parse_args()
    try:
        build_job_id, image_version = verify_prepared_run(
            json.loads(args.run.read_text(encoding="utf-8")),
            json.loads(args.jobs.read_text(encoding="utf-8")),
            args.logs.read_bytes(), args.candidate_sha,
            json.loads(args.digests), args.run_id,
            json.loads(args.commit.read_text(encoding="utf-8")), layer7_eros=args.layer7_eros,
            layer7_core_config=args.layer7_core_config,
            layer7_inventory=(json.loads(args.layer7_inventory.read_text(encoding="utf-8"))
                              if args.layer7_inventory else None),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("PREPARED_RUN=FAIL reason=" + str(exc)) from None
    output = f"image_version={image_version}\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    print(f"PREPARED_RUN=PASS run_id={args.run_id} build_job_id={build_job_id} candidate_sha={args.candidate_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
