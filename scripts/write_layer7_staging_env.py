#!/usr/bin/env python3
"""Atomically install a validated staging-only GPoint environment from stdin."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import sys

from layer7_staging_env import parse_staging_env


RUN_ID = re.compile(r"^[0-9]{1,20}-[0-9]{1,8}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def install_staging_env(payload: bytes, *, root: Path, run_id: str,
                        expected_sha256: str, owner_uid: int = 0) -> str:
    if not RUN_ID.fullmatch(run_id) or not SHA256.fullmatch(expected_sha256):
        raise ValueError("staging environment install identity is invalid")
    try:
        text = payload.decode("utf-8")
        normalized, digest, _ = parse_staging_env(text)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("staging environment bundle validation failed") from None
    if digest != expected_sha256:
        raise ValueError("staging environment bundle digest mismatch")
    parent = root
    parent_info = parent.stat()
    if (parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != owner_uid or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise ValueError("staging environment directory is not protected")
    target = parent / "staging.env"
    if target.is_symlink():
        raise ValueError("staging environment target is a symlink")
    if target.exists():
        info = target.stat()
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
            raise ValueError("existing staging environment file is not protected")
    temp = parent / f".staging.env.{run_id}.pending"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as output:
            output.write(normalized)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temp, owner_uid, -1)
        os.chmod(temp, 0o600)
        os.replace(temp, target)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/gpoint/env"))
    args = parser.parse_args()
    try:
        payload = sys.stdin.buffer.read(48_001)
        if len(payload) > 48_000:
            raise ValueError("staging environment bundle is too large")
        digest = install_staging_env(payload, root=args.root,
                                     run_id=args.run_id, expected_sha256=args.sha256)
    except (OSError, ValueError) as error:
        print("LAYER7_STAGING_ENV=FAIL reason=" + str(error), file=sys.stderr)
        return 1
    print("LAYER7_STAGING_ENV=PASS sha256=" + digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
