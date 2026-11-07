#!/usr/bin/env python3
"""Fail-closed verification of the stock Eros runtime identity labels."""

from __future__ import annotations

import re
import subprocess
import sys


def inspect_label(container: str, label: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{label}"}}}}',
            container,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: verify-eros-runtime-labels.py CONTAINER EXPECTED_UPSTREAM_SHA EXPECTED_PATCH_SET", file=sys.stderr)
        return 2
    container, expected_sha, expected_patch_set = argv[1:]
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        print("invalid expected Eros upstream SHA", file=sys.stderr)
        return 2
    if expected_patch_set != "none":
        print("stock Eros runtime must have patch-set label none", file=sys.stderr)
        return 2

    expected = {
        "org.opencontainers.image.revision": expected_sha,
        "com.gpoint.eros.upstream-revision": expected_sha,
        "com.gpoint.eros.patch-set.sha256": expected_patch_set,
    }
    try:
        for label, value in expected.items():
            if inspect_label(container, label) != value:
                print(f"Eros runtime identity mismatch: {label}", file=sys.stderr)
                return 1
    except (OSError, subprocess.CalledProcessError):
        print("could not inspect Eros runtime identity", file=sys.stderr)
        return 1

    print("EROS_RUNTIME_LABELS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
