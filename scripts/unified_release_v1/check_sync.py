#!/usr/bin/env python3
"""Read-only check that two repos vendor the same versioned release contract."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

FILES = ("core.py", "contract.json", "README.md")


def file_hash(root: Path, relative: str) -> str:
    try:
        content = (root / relative).read_bytes()
    except OSError as error:
        raise ValueError(f"required standard file is unavailable: {relative}") from error
    return hashlib.sha256(content).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--peer-repo", required=True, type=Path)
    args = parser.parse_args()
    local = Path(__file__).resolve().parent
    peer = args.peer_repo.resolve() / "scripts" / "unified_release_v1"
    try:
        for relative in FILES:
            left, right = file_hash(local, relative), file_hash(peer, relative)
            print(f"UNIFIED_RELEASE_V1_{relative.upper().replace('.', '_')}="
                  f"{'PASS' if left == right else 'MISMATCH'} local={left} peer={right}")
            if left != right:
                return 1
    except ValueError as error:
        print(f"UNIFIED_RELEASE_V1_SYNC=BLOCKED reason={error}", file=sys.stderr)
        return 1
    print("UNIFIED_RELEASE_V1_SYNC=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
