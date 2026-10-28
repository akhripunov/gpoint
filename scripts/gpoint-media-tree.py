#!/usr/bin/env python3
"""Create or verify a deterministic manifest of GPoint's local media tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


SCHEMA = "gpoint-media-tree-manifest/v1"
CHUNK_SIZE = 1024 * 1024


class MediaTreeError(Exception):
    pass


def _stable_file(path: Path, expected: os.stat_result) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MediaTreeError(f"cannot safely open media file {path.name!r}: {error.strerror}") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            raise MediaTreeError(f"media file changed during inventory: {path.name!r}")
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(descriptor, CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid", "st_gid")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or size != after.st_size:
            raise MediaTreeError(f"media file changed while hashing: {path.name!r}")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def build_manifest(root: Path) -> dict[str, Any]:
    try:
        root_lstat = root.lstat()
    except OSError as error:
        raise MediaTreeError(f"media root is unavailable: {error.strerror}") from None
    if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(root_lstat.st_mode):
        raise MediaTreeError("media root must be a real directory, not a symlink")

    entries: list[dict[str, Any]] = [{
        "path": ".", "type": "directory", "mode": stat.S_IMODE(root_lstat.st_mode),
        "uid": root_lstat.st_uid, "gid": root_lstat.st_gid,
    }]

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        except OSError as error:
            raise MediaTreeError(f"cannot read media directory {prefix or '.'}: {error.strerror}") from None
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as error:
                raise MediaTreeError(f"cannot inspect media entry {relative!r}: {error.strerror}") from None
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "type": "directory", "mode": mode,
                                "uid": info.st_uid, "gid": info.st_gid})
                walk(Path(child.path), relative)
            elif stat.S_ISREG(info.st_mode):
                digest, size = _stable_file(Path(child.path), info)
                entries.append({"path": relative, "type": "file", "mode": mode,
                                "uid": info.st_uid, "gid": info.st_gid,
                                "size": size, "sha256": digest})
            else:
                raise MediaTreeError(f"unsupported media filesystem entry: {relative!r}")

    walk(root, "")
    files = [entry for entry in entries if entry["type"] == "file"]
    return {
        "schema": SCHEMA,
        "fileCount": len(files),
        "directoryCount": len(entries) - len(files),
        "totalBytes": sum(entry["size"] for entry in files),
        "entries": entries,
    }


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MediaTreeError("media manifest must be a regular file")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except MediaTreeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaTreeError(f"media manifest is unavailable or invalid: {error}") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or not isinstance(manifest.get("entries"), list):
        raise MediaTreeError("media manifest schema is invalid")
    return manifest


def write_manifest(root: Path, output: Path) -> dict[str, Any]:
    manifest = build_manifest(root)
    payload = canonical_bytes(manifest)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as error:
        raise MediaTreeError(f"cannot create private media manifest: {error.strerror}") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(descriptor)
    manifest["manifestSha256"] = hashlib.sha256(payload).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("manifest")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--manifest", type=Path, required=True)
    compare.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "manifest":
            manifest = write_manifest(args.root, args.output)
            print("GPOINT_MEDIA_MANIFEST=PASS")
            print(f"GPOINT_MEDIA_FILE_COUNT={manifest['fileCount']}")
            print(f"GPOINT_MEDIA_DIRECTORY_COUNT={manifest['directoryCount']}")
            print(f"GPOINT_MEDIA_TOTAL_BYTES={manifest['totalBytes']}")
            print(f"GPOINT_MEDIA_MANIFEST_SHA256={manifest['manifestSha256']}")
            return 0
        expected = read_manifest(args.manifest)
        actual = build_manifest(args.root)
        if actual != {key: value for key, value in expected.items() if key != "manifestSha256"}:
            raise MediaTreeError("media tree does not match its backup manifest")
        print("GPOINT_MEDIA_TREE_MATCH=PASS")
        return 0
    except MediaTreeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
