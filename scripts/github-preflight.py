#!/usr/bin/env python3
"""Read-only GitHub connectivity, gh auth, repository and Actions preflight."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from urllib.parse import urlparse
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence


REPOSITORY = "akhripunov/gpoint"
NETWORK_URLS = ("https://github.com/", "https://api.github.com/")
TIMEOUT_SECONDS = 8


class PreflightFailure(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _probe_url(url: str, opener: Callable = urllib.request.urlopen) -> None:
    host = urlparse(url).hostname or "configured GitHub host"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "gpoint-github-preflight", "Accept": "application/vnd.github+json"},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code  # An HTTP response proves DNS, routing and TLS worked.
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError,)) or "timed out" in str(reason).lower():
            raise PreflightFailure("NETWORK_BLOCKED", f"{host} could not be reached (timeout).")
        if isinstance(reason, (ConnectionError,)):
            raise PreflightFailure("NETWORK_BLOCKED", f"{host} could not be reached (connection failure).")
        if "name or service not known" in str(reason).lower() or "nodename nor servname" in str(reason).lower() or "temporary failure in name resolution" in str(reason).lower():
            raise PreflightFailure("NETWORK_BLOCKED", f"DNS resolution failed for {host}.")
        raise PreflightFailure("NETWORK_BLOCKED", f"{host} could not be reached (DNS, TLS or routing failure).")
    if status >= 500 or status == 429:
        raise PreflightFailure("GITHUB_UNAVAILABLE", f"{host} returned HTTP {status} to a read-only connectivity check.")


def _default_runner(args: Sequence[str]) -> CommandResult:
    try:
        result = subprocess.run(
            list(args), capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(127, "", "")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _api_failure(result: CommandResult, endpoint: str) -> PreflightFailure:
    # gh's stderr can contain incidental details. Inspect it privately and emit
    # only a stable classification; never echo raw CLI output or credentials.
    error = result.stderr.lower()
    match = re.search(r"http\s+(\d{3})", error)
    status = int(match.group(1)) if match else None
    if status == 401 or "bad credentials" in error or "invalid token" in error:
        return PreflightFailure("AUTH_INVALID", "GitHub rejected the saved credential (HTTP 401).")
    if "rate limit" in error or status == 429 or status and status >= 500:
        return PreflightFailure("GITHUB_UNAVAILABLE", "GitHub API is rate-limited or temporarily unavailable.")
    if status in (403, 404):
        return PreflightFailure("PERMISSION_DENIED", f"Authenticated account cannot read {endpoint}.")
    return PreflightFailure("GITHUB_UNAVAILABLE", f"GitHub CLI could not complete the {endpoint} check.")


def run_preflight(
    opener: Callable = urllib.request.urlopen,
    runner: Callable[[Sequence[str]], CommandResult] = _default_runner,
) -> list[str]:
    for url in NETWORK_URLS:
        _probe_url(url, opener)

    # gh auth status is a local status check; a real authenticated API request
    # below is authoritative before classifying a credential as rejected.
    auth_status = runner(["gh", "auth", "status", "--hostname", "github.com"])
    whoami = runner(["gh", "api", "user", "--jq", ".login"])
    if whoami.returncode:
        raise _api_failure(whoami, "the authenticated user endpoint")
    if not whoami.stdout.strip():
        raise PreflightFailure("GITHUB_UNAVAILABLE", "GitHub returned no authenticated account identity.")

    # A successful `gh api user` is authoritative for the effective credential.
    # A failing local status can refer to an unused stored credential when an
    # environment override is active, so it must not block a working API path.

    repository = runner(["gh", "api", f"repos/{REPOSITORY}", "--jq", ".full_name"])
    if repository.returncode:
        raise _api_failure(repository, f"repository {REPOSITORY}")
    if repository.stdout.strip().lower() != REPOSITORY.lower():
        raise PreflightFailure("GITHUB_UNAVAILABLE", "GitHub returned an unexpected repository identity.")

    actions = runner(["gh", "api", f"repos/{REPOSITORY}/actions/runs?per_page=1", "--jq", ".total_count"])
    if actions.returncode:
        raise _api_failure(actions, f"Actions runs for {REPOSITORY}")
    try:
        int(actions.stdout.strip())
    except ValueError:
        raise PreflightFailure("GITHUB_UNAVAILABLE", "GitHub returned an invalid Actions response.")
    token_override = "GH_TOKEN" if os.environ.get("GH_TOKEN") else "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else None
    credential_source = f"effective credential source: {token_override} environment override" if token_override else "effective credential source: gh-managed credential"
    auth_line = "gh auth: PASS (authenticated API identity verified)"
    if auth_status.returncode:
        auth_line += "; local status differs"
    return ["network: PASS (github.com, api.github.com)", auth_line, credential_source, f"repository: PASS ({REPOSITORY})", "Actions read: PASS"]


def main() -> int:
    try:
        for line in run_preflight():
            print(line)
    except PreflightFailure as failure:
        print(f"{failure.code}: {failure.detail}", file=sys.stderr)
        return 1
    print("GITHUB_PREFLIGHT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
