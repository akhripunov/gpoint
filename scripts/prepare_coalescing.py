#!/usr/bin/env python3
"""Bounded, fail-closed quiet-window gate for main-branch Prepare runs."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

REPOSITORY = "akhripunov/gpoint"
QUIET_WINDOW_SECONDS = 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CoalescingError(RuntimeError):
    """The candidate could not be authoritatively checked against current main."""


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def read_current_main_sha(token: str, opener=urllib.request.urlopen) -> str:
    if not token:
        raise CoalescingError("GitHub token is unavailable")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main",
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
        raise CoalescingError(f"GitHub main SHA lookup failed (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise CoalescingError("GitHub main SHA lookup failed") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("object"), dict):
        raise CoalescingError("GitHub returned an invalid main reference")
    sha = payload["object"].get("sha")
    if not _valid_sha(sha):
        raise CoalescingError("GitHub returned an invalid main SHA")
    return sha


def wait_for_quiet_main(
    candidate_sha: str,
    read_main_sha: Callable[[], str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    window_seconds: int = QUIET_WINDOW_SECONDS,
) -> str:
    """Return WINNER only when candidate stays current throughout the window."""
    if not _valid_sha(candidate_sha):
        raise CoalescingError("Candidate SHA is invalid")
    if not isinstance(window_seconds, int) or not 1 <= window_seconds <= 300:
        raise CoalescingError("Quiet window is outside the bounded range")

    try:
        initial_main = read_main_sha()
    except CoalescingError:
        raise
    except Exception:
        raise CoalescingError("Current main SHA lookup failed") from None
    if not _valid_sha(initial_main):
        raise CoalescingError("Current main SHA is invalid")
    if initial_main != candidate_sha:
        return "SUPERSEDED"

    sleep(window_seconds)
    try:
        final_main = read_main_sha()
    except CoalescingError:
        raise
    except Exception:
        raise CoalescingError("Current main SHA lookup failed") from None
    if not _valid_sha(final_main):
        raise CoalescingError("Current main SHA is invalid")
    return "WINNER" if final_main == candidate_sha else "SUPERSEDED"


def _publish_result(result: str, candidate: str, current_main: str, elapsed: float) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"result={result}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                "## GPoint Prepare coalescing\n\n"
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


def main() -> int:
    started = time.monotonic()
    candidate = os.environ.get("CANDIDATE_SHA", "")
    reads: list[str] = []

    def read_main() -> str:
        sha = read_current_main_sha(os.environ.get("GITHUB_TOKEN", ""))
        reads.append(sha)
        return sha

    try:
        result = wait_for_quiet_main(candidate, read_main)
    except CoalescingError as error:
        _publish_result("BLOCKED", candidate, reads[-1] if reads else "", time.monotonic() - started)
        print(f"PREPARE_COALESCING_ERROR={error}", file=sys.stderr, flush=True)
        return 1
    _publish_result(result, candidate, reads[-1], time.monotonic() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
