#!/usr/bin/env python3
"""Immutable Mini App static artifact utilities for future component releases.

This module only creates and verifies offline bundles. Production deployment
is intentionally NOT exposed until V3's ledger/approval/rollback is extended.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

SHA = re.compile(r"^[a-f0-9]{40}$")
VERSION = 1
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000


def _entries(dist: Path):
    if not dist.is_dir() or dist.is_symlink():
        raise ValueError("dist must be a real directory")
    root = dist.resolve(strict=True)
    found = {}
    for candidate in sorted(dist.rglob("*")):
        # .resolve() must stay within trusted dist, even for malicious symlinks.
        relative = candidate.relative_to(dist)
        parts = relative.parts
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise ValueError("unsafe static artifact path")
        relative_name = relative.as_posix()
        if "\\" in relative_name or "\n" in relative_name:
            raise ValueError("ambiguous static artifact name")
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("static artifact cannot contain symlinks")
        if candidate.is_dir():
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("static artifact contains non-file entry")
        if candidate.resolve(strict=True).is_relative_to(root) is not True:
            raise ValueError("static artifact escaped root")
        found[relative_name] = candidate
    if len(found) > MAX_FILES or sum(p.stat().st_size for p in found.values()) > MAX_ARTIFACT_BYTES:
        raise ValueError("static artifact inventory exceeds limits")
    if "index.html" not in found or len(found) < 2:
        raise ValueError("missing site entry or assets")
    return found


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_bundle(dist: Path, candidate: str, destination: Path):
    if not SHA.fullmatch(candidate or ""):
        raise ValueError("invalid candidate")
    files = _entries(dist)
    # The exact SHA must be embedded by Vite's build, not patched in afterwards.
    html = files["index.html"].read_text(encoding="utf8")
    if f'gpoint-build-sha" content="{candidate}' not in html:
        raise ValueError("Mini App exact SHA missing in index")
    inventory = {name: {"sha256": _hash_file(path), "size": path.stat().st_size}
                 for name, path in files.items()}
    proof = {
        "schema": VERSION, "kind": "gpoint-miniapp-static",
        "candidateSha": candidate, "files": inventory,
    }
    if destination.exists():
        raise ValueError("immutable artifact destination already exists")
    # Deterministic archive, with a protected metadata namespace.
    if ".gpoint-artifact.json" in inventory:
        raise ValueError("reserved filename")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name, path in files.items():
            zinfo = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = (0o100644 << 16)
            archive.writestr(zinfo, path.read_bytes())
        meta = zipfile.ZipInfo(".gpoint-artifact.json", date_time=(1980, 1, 1, 0, 0, 0))
        meta.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(meta, json.dumps(proof, sort_keys=True, separators=(",", ":")))
    return verify_bundle(destination, candidate)


def verify_bundle(archive: Path, candidate: str):
    if not SHA.fullmatch(candidate or "") or not archive.is_file() or archive.is_symlink() or archive.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("invalid archive or candidate")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) > MAX_FILES + 1 or len(names) != len(set(names)) or ".gpoint-artifact.json" not in names:
            raise ValueError("missing/duplicate artifact records")
        if z.getinfo(".gpoint-artifact.json").file_size > 256 * 1024 or sum(x.file_size for x in z.infolist()) > MAX_ARTIFACT_BYTES:
            raise ValueError("archive expansion limit exceeded")
        manifest = json.loads(z.read(".gpoint-artifact.json"))
        if (manifest.get("schema") != VERSION or
            manifest.get("kind") != "gpoint-miniapp-static" or
            manifest.get("candidateSha") != candidate):
            raise ValueError("bad artifact identity")
        files = manifest.get("files")
        if not isinstance(files, dict) or "index.html" not in files or len(files) < 2:
            raise ValueError("invalid artifact inventory")
        if set(files) | {".gpoint-artifact.json"} != set(names):
            raise ValueError("unmanifested artifact entry")
        for name, expected in files.items():
            parts = PurePosixPath(name).parts
            if (name.startswith("/") or "\\" in name or "\n" in name or
                any(p in ("", ".", "..") for p in parts) or name.endswith("/")):
                raise ValueError("unsafe zip filename")
            entry = z.getinfo(name)
            if entry.is_dir() or ((entry.external_attr >> 16) & 0o170000) not in (0, 0o100000):
                raise ValueError("unsafe archive entry type")
            data = z.read(name)
            if (expected.get("sha256") != hashlib.sha256(data).hexdigest() or
                expected.get("size") != len(data)):
                raise ValueError("asset hash or size mismatch")
        if f'gpoint-build-sha" content="{candidate}' not in z.read("index.html").decode("utf8"):
            raise ValueError("entrypoint candidate mismatch")
    return manifest


if __name__ == "__main__":
    raise SystemExit("This module does not dispatch production deployments")
