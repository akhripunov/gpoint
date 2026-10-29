#!/usr/bin/env python3
"""Hash a public Mini App static tree without following links or special files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


SCHEMA = "gpoint-miniapp-static/v1"


def manifest(root: Path, *, reject_symlink_ancestors: bool = False) -> dict[str, object]:
    root = Path(os.path.abspath(root))
    if reject_symlink_ancestors:
        current = Path(root.anchor)
        for component in root.parts[1:]:
            current /= component
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError("static path contains a symlink component")
    elif root.is_symlink():
        raise ValueError("static root must not be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("static root must be a real directory")
    entries: list[dict[str, object]] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise ValueError("static tree contains a non-directory entry")
        for name in files:
            path = current_path / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("static tree contains a non-regular file")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            })
    if not entries:
        raise ValueError("static tree is empty")
    entries.sort(key=lambda item: str(item["path"]))
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": SCHEMA, "fileCount": len(entries), "sha256": hashlib.sha256(encoded).hexdigest()}


def main() -> int:
    reject_symlink_ancestors = len(sys.argv) == 3 and sys.argv[1] == "--reject-symlink-ancestors"
    if len(sys.argv) != (3 if reject_symlink_ancestors else 2):
        raise SystemExit("usage: gpoint-miniapp-static-manifest.py [--reject-symlink-ancestors] <static-root>")
    root_arg = sys.argv[2] if reject_symlink_ancestors else sys.argv[1]
    try:
        print(json.dumps(manifest(Path(root_arg), reject_symlink_ancestors=reject_symlink_ancestors),
                         sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError) as exc:
        print(f"MINIAPP_STATIC_MANIFEST=BLOCKED reason={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
