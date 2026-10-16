"""Shared, versioned primitives for the Unified Release Standard V1.

This file is intentionally byte-identical in GPoint and MAXIBOT until a
separately reviewed shared GitHub repository/package is authorized.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Union

STANDARD_VERSION = "1.0.0"
QUIET_WINDOW_SECONDS = 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_TRANSPORTS = {"ghcr-oci", "archive"}
REQUIRED_LIFECYCLE = [
    "COALESCE", "PREPARE", "AUTHORIZE", "PREFLIGHT", "PULL", "APPLY", "VERIFY", "EVIDENCE"
]
REQUIRED_PROJECT_GATES = {
    "exact_sha", "immutable_provenance", "owner_authorization", "migration_safety",
    "worker_compatibility", "rollback_readiness", "health_verification",
}
REQUIRED_IMAGE_TRANSPORTS = {"ghcr-oci", "archive"}
REQUIRED_EVIDENCE = {
    "candidate_sha", "prepare_run_id", "prepare_attempt", "image_digests",
    "provenance", "baseline", "rollback", "verification",
}
REQUIRED_COMPLETION_EVIDENCE = {
    "apply_verified", "runtime_identity_verified", "health_verified", "evidence_published",
}


class UnifiedReleaseError(RuntimeError):
    """The project adapter or release evidence does not meet standard V1."""


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def load_project_config(path: Union[str, Path]) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UnifiedReleaseError("Unified Release project configuration is unavailable or invalid") from None
    if not isinstance(value, dict):
        raise UnifiedReleaseError("Unified Release project configuration must be an object")
    return value


def validate_project_config(config: dict) -> None:
    if config.get("standard_version") != STANDARD_VERSION:
        raise UnifiedReleaseError("Unsupported Unified Release Standard version")
    if config.get("lifecycle") != REQUIRED_LIFECYCLE:
        raise UnifiedReleaseError("Project lifecycle differs from Unified Release Standard V1")
    if not isinstance(config.get("project"), str) or not config["project"].strip():
        raise UnifiedReleaseError("Project identity is missing")
    if not isinstance(config.get("repository"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]
    ):
        raise UnifiedReleaseError("GitHub repository identity is invalid")
    if config.get("prepare_executor") != "github-hosted" or config.get("deploy_executor") != "mac-self-hosted":
        raise UnifiedReleaseError("Release executor roles differ from Unified Release Standard V1")
    if not isinstance(config.get("project_gates"), list) or not REQUIRED_PROJECT_GATES.issubset(
        set(config["project_gates"])
    ):
        raise UnifiedReleaseError("Required safety gates are missing from project configuration")
    transports = config.get("image_transports")
    if not isinstance(transports, list) or not IMAGE_TRANSPORTS.issubset(set(transports)):
        raise UnifiedReleaseError("GHCR and transition archive transports must both be configured")
    if config.get("archive_fallback_required_until_e2e_pass") is not True:
        raise UnifiedReleaseError("Archive fallback cannot be removed before end-to-end proof")
    if config.get("production_state_scope") != "project-local":
        raise UnifiedReleaseError("Production state must remain project-local")


def validate_standard_contract(contract: dict) -> None:
    if contract.get("standard_version") != STANDARD_VERSION:
        raise UnifiedReleaseError("Unsupported Unified Release Standard contract version")
    if contract.get("lifecycle") != REQUIRED_LIFECYCLE:
        raise UnifiedReleaseError("Unified Release lifecycle contract is incomplete")
    expected_coalescing = {
        "executor": "mac-self-hosted",
        "quiet_window_seconds": QUIET_WINDOW_SECONDS,
        "max_window_seconds": 300,
        "main_sha_lookup_required": True,
        "failure_result": "BLOCKED",
        "superseded_result": "SUPERSEDED",
        "prepare_requires": "WINNER",
    }
    if contract.get("coalescing") != expected_coalescing:
        raise UnifiedReleaseError("Unified Release coalescing contract is invalid")
    if contract.get("executors") != {"prepare": "github-hosted", "deploy": "mac-self-hosted"}:
        raise UnifiedReleaseError("Unified Release executor contract is invalid")
    if contract.get("deploy_stages") != ["PREFLIGHT", "PULL", "APPLY", "VERIFY"]:
        raise UnifiedReleaseError("Unified Release Deploy stages are invalid")
    if contract.get("separate_preflight_required_in_automatic_path") is not False:
        raise UnifiedReleaseError("Automatic Deploy must own its preflight")
    if contract.get("authorization") != {
        "task_scoped": True, "one_shot": True, "exact_sha": True, "scope_and_baseline_bound": True
    }:
        raise UnifiedReleaseError("Unified Release authorization contract is invalid")
    if contract.get("statuses") != {
        "coalesce": ["WINNER", "SUPERSEDED", "BLOCKED"],
        "prepare": ["READY", "BLOCKED"],
        "deploy": ["PREFLIGHTING", "PULLING", "APPLYING", "VERIFYING", "COMPLETE", "BLOCKED", "RECOVERY_REQUIRED"],
    }:
        raise UnifiedReleaseError("Unified Release status contract is invalid")
    if set(contract.get("required_safety_gates", [])) != REQUIRED_PROJECT_GATES:
        raise UnifiedReleaseError("Unified Release required safety gates are invalid")
    if set(contract.get("image_transports", [])) != REQUIRED_IMAGE_TRANSPORTS:
        raise UnifiedReleaseError("Unified Release image transport contract is invalid")
    if set(contract.get("required_evidence", [])) != REQUIRED_EVIDENCE:
        raise UnifiedReleaseError("Unified Release required evidence contract is invalid")
    if set(contract.get("completion_requires", [])) != REQUIRED_COMPLETION_EVIDENCE:
        raise UnifiedReleaseError("Unified Release completion evidence contract is invalid")
    if contract.get("production_state_scope") != "project-local":
        raise UnifiedReleaseError("Unified Release production state scope is invalid")
    if contract.get("recovery_required_on_ambiguous_apply") is not True:
        raise UnifiedReleaseError("Ambiguous Apply must require recovery")


def read_current_main_sha(token: str, repository: str, opener=urllib.request.urlopen) -> str:
    if not token:
        raise UnifiedReleaseError("GitHub token is unavailable")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise UnifiedReleaseError("GitHub repository identity is invalid")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/git/ref/heads/main",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise UnifiedReleaseError(f"GitHub main SHA lookup failed (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise UnifiedReleaseError("GitHub main SHA lookup failed") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("object"), dict):
        raise UnifiedReleaseError("GitHub returned an invalid main reference")
    sha = payload["object"].get("sha")
    if not _valid_sha(sha):
        raise UnifiedReleaseError("GitHub returned an invalid main SHA")
    return sha


def wait_for_quiet_main(
    candidate_sha: str,
    read_main_sha: Callable[[], str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    window_seconds: int = QUIET_WINDOW_SECONDS,
) -> str:
    """Return WINNER only if exact candidate stays current for the bounded window."""
    if not _valid_sha(candidate_sha):
        raise UnifiedReleaseError("Candidate SHA is invalid")
    if not isinstance(window_seconds, int) or not 1 <= window_seconds <= 300:
        raise UnifiedReleaseError("Quiet window is outside the bounded range")
    try:
        initial_main = read_main_sha()
    except UnifiedReleaseError:
        raise
    except Exception:
        raise UnifiedReleaseError("Current main SHA lookup failed") from None
    if not _valid_sha(initial_main):
        raise UnifiedReleaseError("Current main SHA is invalid")
    if initial_main != candidate_sha:
        return "SUPERSEDED"
    sleep(window_seconds)
    try:
        final_main = read_main_sha()
    except UnifiedReleaseError:
        raise
    except Exception:
        raise UnifiedReleaseError("Current main SHA lookup failed") from None
    if not _valid_sha(final_main):
        raise UnifiedReleaseError("Current main SHA is invalid")
    return "WINNER" if final_main == candidate_sha else "SUPERSEDED"


def _publish_result(project: str, result: str, candidate: str, current_main: str, elapsed: float) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"result={result}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                f"## {project} Prepare coalescing\n\n"
                f"- Result: {result}\n"
                f"- Candidate SHA: {candidate}\n"
                f"- Current main SHA: {current_main or 'UNKNOWN'}\n"
                f"- Bounded quiet window: {QUIET_WINDOW_SECONDS}s\n"
                f"- Gate runtime: {elapsed:.3f}s\n"
                f"- Expensive Prepare eligible: {'YES' if result == 'WINNER' else 'NO'}\n"
            )
    print(
        f"PREPARE_COALESCING={result} CANDIDATE_SHA={candidate} "
        f"CURRENT_MAIN_SHA={current_main or 'UNKNOWN'} "
        f"QUIET_WINDOW_SECONDS={QUIET_WINDOW_SECONDS} ELAPSED_SECONDS={elapsed:.3f}",
        flush=True,
    )


def run_coalescing(project: str, repository: str) -> int:
    started = time.monotonic()
    candidate = os.environ.get("CANDIDATE_SHA", "")
    reads: list[str] = []

    def read_main() -> str:
        sha = read_current_main_sha(os.environ.get("GITHUB_TOKEN", ""), repository)
        reads.append(sha)
        return sha

    try:
        result = wait_for_quiet_main(candidate, read_main)
    except UnifiedReleaseError as error:
        _publish_result(project, "BLOCKED", candidate, reads[-1] if reads else "", time.monotonic() - started)
        print(f"PREPARE_COALESCING_ERROR={error}", file=sys.stderr, flush=True)
        return 1
    _publish_result(project, result, candidate, reads[-1], time.monotonic() - started)
    return 0
