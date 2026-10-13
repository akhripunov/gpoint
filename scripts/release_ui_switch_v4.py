#!/usr/bin/env python3
"""Offline atomic symlink stage/switch/rollback primitives for V4 static site.

Not wired to production. A future authorized V3 executor MUST supply trusted
receipt, VPS preflight, release lock, HTTP smoke, immutable state and recovery.
The existing /opt/gpoint/app/miniapp directory must first be migrated under a
separately gated release; this module intentionally refuses non-symlink live.
"""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

try:
    from .release_ui_artifact_v4 import verify_bundle
except ImportError:
    from release_ui_artifact_v4 import verify_bundle


def stage(root: Path, archive: Path, candidate: str) -> Path:
    manifest = verify_bundle(archive, candidate)
    root = Path(root).resolve(strict=True)
    releases = root / "releases"
    if releases.is_symlink():
        raise ValueError("releases must not be a symlink")
    releases.mkdir(mode=0o755, exist_ok=True)
    target = releases / candidate
    if target.exists() or target.is_symlink():
        raise ValueError("release must be immutable")
    temp = Path(tempfile.mkdtemp(prefix=".stage-", dir=releases))
    try:
        with zipfile.ZipFile(archive, "r") as src:
            for filename in manifest["files"]:
                # Already validated: relative regular files, no traversal/symlink.
                dest = temp / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with src.open(filename) as data, dest.open("xb") as output:
                    shutil.copyfileobj(data, output, length=65536)
                dest.chmod(0o644)
        # Verify staged filesystem by rebuilding inventory before activation.
        try:
            from .release_ui_artifact_v4 import _entries, _hash_file
        except ImportError:
            from release_ui_artifact_v4 import _entries, _hash_file
        actual = _entries(temp)
        if set(actual) != set(manifest["files"]):
            raise ValueError("staged inventory mismatch")
        for path, properties in manifest["files"].items():
            if _hash_file(actual[path]) != properties["sha256"]:
                raise ValueError("staged content hash mismatch")
        os.replace(temp, target)
        return target
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _validate_target(root: Path, target: Path):
    root = Path(root).resolve(strict=True)
    target = Path(target)
    if not target.is_absolute() or not target.is_dir() or target.is_symlink():
        raise ValueError("invalid immutable target")
    real = target.resolve(strict=True)
    if real.parent != (root / "releases").resolve(strict=True):
        raise ValueError("target outside immutable releases")
    if len(real.name) != 40 or any(c not in "0123456789abcdef" for c in real.name):
        raise ValueError("target not addressed by SHA")
    return real


def _replace_link(link: Path, target: Path):
    if not link.is_symlink():
        raise ValueError("live Mini App is not yet a symlink: migration required")
    temp_link = link.with_name("." + link.name + ".switch-" + str(os.getpid()))
    if temp_link.exists() or temp_link.is_symlink():
        raise ValueError("temporary switch link collision")
    try:
        temp_link.symlink_to(target, target_is_directory=True)
        os.replace(temp_link, link)
    finally:
        temp_link.unlink(missing_ok=True)


def activate(root: Path, live: Path, target: Path):
    """Atomic local change only; owner approval and health smoke are external."""
    real = _validate_target(root, target)
    live = Path(live)
    if live.name != "miniapp" or live.parent.resolve(strict=True) != (Path(root).resolve(strict=True) / "app").resolve(strict=True):
        raise ValueError("live path is not the canonical Mini App path")
    if not live.is_symlink():
        raise ValueError("non-symlink production requires migration")
    prior = live.resolve(strict=True)
    _validate_target(root, prior)
    _replace_link(live, real)
    return prior


def rollback(root: Path, live: Path, expected: Path, previous: Path):
    """Only revert the expected version; never overwrite a concurrent switch."""
    if Path(live).name != "miniapp" or Path(live).parent.resolve(strict=True) != (Path(root).resolve(strict=True) / "app").resolve(strict=True):
        raise ValueError("live path is not the canonical Mini App path")
    old = _validate_target(root, previous)
    now = _validate_target(root, expected)
    if not live.is_symlink() or live.resolve(strict=True) != now:
        raise ValueError("runtime moved; rollback requires reconciliation")
    _replace_link(live, old)
    return old


if __name__ == "__main__":
    raise SystemExit("Offline utility only; never invoke directly as production deploy")
