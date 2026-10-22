#!/usr/bin/env python3
"""Bounded, read-only GitHub API preflight for the trusted Prepare runner."""
from __future__ import annotations

import os
import re
import sys

try:
    from scripts.network_retry import RetryBudget
    from scripts.prepare_coalescing import CoalescingError, read_current_main_sha
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from network_retry import RetryBudget
    from prepare_coalescing import CoalescingError, read_current_main_sha


REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not REPOSITORY_RE.fullmatch(repository) or repository != "akhripunov/gpoint":
        print("GITHUB_API_PREFLIGHT=BLOCKED category=AUTH_OR_CONFIG", flush=True)
        return 1

    try:
        sha = read_current_main_sha(token, budget=RetryBudget())
    except CoalescingError as error:
        message = str(error)
        if "HTTP 401" in message:
            category = "AUTH"
            detail = " httpStatus=401"
        elif "HTTP 403" in message:
            category = "PERMISSION"
            detail = " httpStatus=403"
        elif "HTTP " in message:
            category = "HTTP"
            match = re.search(r"HTTP ([0-9]{3})", message)
            detail = f" httpStatus={match.group(1)}" if match else ""
        elif "TRANSIENT_NETWORK" in message:
            category = "NETWORK"
            detail = " reason=RETRY_BUDGET_EXHAUSTED"
        else:
            category = "INVALID_RESPONSE"
            detail = ""
        print(f"GITHUB_API_PREFLIGHT=BLOCKED category={category}{detail}", flush=True)
        return 1

    print(f"GITHUB_API_PREFLIGHT=PASS currentMainSha={sha}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
