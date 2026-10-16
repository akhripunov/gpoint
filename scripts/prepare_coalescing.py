#!/usr/bin/env python3
"""GPoint adapter for the byte-identical Unified Release Standard V1 core."""
from __future__ import annotations

import urllib.request
from typing import Callable

try:
    from scripts.unified_release_v1 import core
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from unified_release_v1 import core

REPOSITORY = "akhripunov/gpoint"
QUIET_WINDOW_SECONDS = core.QUIET_WINDOW_SECONDS
SHA_RE = core.SHA_RE
CoalescingError = core.UnifiedReleaseError


def _valid_sha(value: object) -> bool:
    return core._valid_sha(value)


def read_current_main_sha(token: str, opener=urllib.request.urlopen) -> str:
    return core.read_current_main_sha(token, REPOSITORY, opener)


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
    return core.run_coalescing("GPoint", REPOSITORY)


if __name__ == "__main__":
    raise SystemExit(main())
