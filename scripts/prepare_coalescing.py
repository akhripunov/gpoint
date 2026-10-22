#!/usr/bin/env python3
"""GPoint adapter for the byte-identical Unified Release Standard V1 core."""
from __future__ import annotations

import errno
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

try:
    from scripts.network_retry import (NetworkUnavailable, RetryBudget,
                                       retry_network_read)
    from scripts.unified_release_v1 import core
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from network_retry import (NetworkUnavailable, RetryBudget,
                               retry_network_read)
    from unified_release_v1 import core

REPOSITORY = "akhripunov/gpoint"
QUIET_WINDOW_SECONDS = core.QUIET_WINDOW_SECONDS
SHA_RE = core.SHA_RE
CoalescingError = core.UnifiedReleaseError


def _valid_sha(value: object) -> bool:
    return core._valid_sha(value)


def _transient_transport_category(error: BaseException) -> str | None:
    """Classify only allowlisted transient transport errors from the core lookup."""
    cause = error.__context__ if isinstance(error, core.UnifiedReleaseError) else error
    if isinstance(cause, urllib.error.HTTPError):
        return None
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    if isinstance(cause, ssl.SSLCertVerificationError):
        return None
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return "TIMEOUT"
    if isinstance(cause, (ssl.SSLEOFError, ssl.SSLWantReadError, ssl.SSLWantWriteError,
                          ssl.SSLZeroReturnError, EOFError)):
        return "TLS_OR_TRANSPORT_EOF"
    if isinstance(cause, ConnectionResetError):
        return "CONNECTION_RESET"
    if isinstance(cause, OSError) and cause.errno == errno.ETIMEDOUT:
        return "TIMEOUT"
    if isinstance(cause, OSError) and cause.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE):
        return "CONNECTION_RESET"
    return None


def read_current_main_sha(
    token: str,
    opener=urllib.request.urlopen,
    *,
    retry_sleep=time.sleep,
    budget: RetryBudget | None = None,
) -> str:
    if not token:
        raise CoalescingError("GitHub token is unavailable")

    # Keep the adapter's established direct-call behavior for compatibility;
    # Prepare's preflight and coalescing entry points explicitly share a budget.
    if budget is None:
        try:
            return core.read_current_main_sha(token, REPOSITORY, opener)
        except Exception as error:
            category = _transient_transport_category(error)
            if category is None:
                raise
            print(f"PREPARE_MAIN_SHA_RETRY category={category} attempt=1/1", file=sys.stderr, flush=True)
            retry_sleep(0.25)
            try:
                return core.read_current_main_sha(token, REPOSITORY, opener)
            except Exception as retry_error:
                retry_category = _transient_transport_category(retry_error)
                if retry_category is None:
                    raise
                print(
                    f"PREPARE_MAIN_SHA_RETRY_EXHAUSTED category={retry_category} attempts=2",
                    file=sys.stderr,
                    flush=True,
                )
                raise CoalescingError("GitHub main SHA lookup failed (TRANSIENT_NETWORK)") from None

    def read() -> str:
        return core.read_current_main_sha(token, REPOSITORY, opener)

    def transient(error: BaseException) -> bool:
        return _transient_transport_category(error) is not None

    def emit(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    try:
        return retry_network_read(
            read,
            "prepare_main_sha",
            transient,
            budget=budget,
            sleep=retry_sleep,
            emit=emit,
        )
    except NetworkUnavailable:
        raise CoalescingError("GitHub main SHA lookup failed (TRANSIENT_NETWORK)") from None


def wait_for_quiet_main(
    candidate_sha: str,
    read_main_sha: Callable[[], str],
    *,
    sleep=core.time.sleep,
    window_seconds: int = QUIET_WINDOW_SECONDS,
) -> str:
    return core.wait_for_quiet_main(
        candidate_sha, read_main_sha, sleep=sleep, window_seconds=window_seconds
    )


def _publish_result(result: str, candidate: str, current_main: str, elapsed: float) -> None:
    core._publish_result("GPoint", result, candidate, current_main, elapsed)


def main() -> int:
    config_path = "scripts/unified_release_v1/project.json"
    try:
        contract = core.load_project_config("scripts/unified_release_v1/contract.json")
        core.validate_standard_contract(contract)
        config = core.load_project_config(config_path)
        core.validate_project_config(config)
        if config["repository"] != REPOSITORY or config["project"] != "GPoint":
            raise CoalescingError("GPoint Unified Release project identity mismatch")
    except CoalescingError as error:
        core._publish_result("GPoint", "BLOCKED", "", "", 0.0)
        print(f"PREPARE_COALESCING_ERROR={error}", file=core.sys.stderr, flush=True)
        return 1
    started = core.time.monotonic()
    budget = RetryBudget()
    candidate = os.environ.get("CANDIDATE_SHA", "")
    reads: list[str] = []

    def read_main() -> str:
        sha = read_current_main_sha(os.environ.get("GITHUB_TOKEN", ""), budget=budget)
        reads.append(sha)
        return sha

    try:
        result = wait_for_quiet_main(candidate, read_main, sleep=core.time.sleep)
    except CoalescingError as error:
        _publish_result("BLOCKED", candidate, reads[-1] if reads else "", core.time.monotonic() - started)
        print(f"PREPARE_COALESCING_ERROR={error}", file=sys.stderr, flush=True)
        return 1
    _publish_result(result, candidate, reads[-1], core.time.monotonic() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
