#!/usr/bin/env python3
"""Fail-closed compatibility check for ordinary GPoint releases during Eros Pilot."""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from typing import Callable


CONTRACT_PATHS = (
    "apps/api/src/eros-adapter.ts",
    "apps/api/src/eros-worker-contract.ts",
    "apps/api/src/eros-worker-gateway.ts",
    "apps/api/src/messages.ts",
    # These are the application composition roots that expose the Eros and
    # AGATA worker gateways. A change can silently remove/register routes even
    # when the individual controller contracts remain unchanged.
    "apps/api/src/main.ts",
    "apps/api/src/agata-worker-gateway.ts",
)
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class CompatibilityError(ValueError):
    pass


def _git_file(sha: str, path: str) -> bytes | None:
    result = subprocess.run(["git", "show", f"{sha}:{path}"], check=False,
                            capture_output=True)
    return result.stdout if result.returncode == 0 else None


def contract_digest(sha: str, reader: Callable[[str, str], bytes | None] = _git_file) -> str:
    if not SHA40.fullmatch(sha):
        raise CompatibilityError("source SHA is malformed")
    digest = hashlib.sha256()
    for path in CONTRACT_PATHS:
        content = reader(sha, path)
        if not isinstance(content, bytes):
            raise CompatibilityError("Eros runtime contract source is unavailable")
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def compare_contract(pilot_sha: str, candidate_sha: str,
                     reader: Callable[[str, str], bytes | None] = _git_file) -> str:
    pilot = contract_digest(pilot_sha, reader)
    candidate = contract_digest(candidate_sha, reader)
    if pilot != candidate:
        raise CompatibilityError("Eros runtime contracts differ; coordinated approved pilot upgrade required")
    return pilot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    try:
        digest = compare_contract(args.pilot_sha, args.candidate_sha)
    except CompatibilityError as error:
        print(f"EROS_RELEASE_COMPATIBILITY=BLOCKED:{error}", file=sys.stderr)
        return 2
    print(f"EROS_RELEASE_COMPATIBILITY=PASS contract_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
