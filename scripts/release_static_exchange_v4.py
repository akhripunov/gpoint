#!/usr/bin/env python3
"""Atomic directory exchange for existing Caddy static site.

Supports macOS renamex_np for local tests; production Linux requires
renameat2 RENAME_EXCHANGE, never the non-atomic rename-old/rename-new pair.
No standalone production apply entrypoint: exact-SHA approval, lock,
readiness and durable recovery belong to canonical orchestrator.
"""
from __future__ import annotations
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import stat

SHA = re.compile(r"^[0-9a-f]{40}$")
EXCHANGE = 2
AT_FDCWD = -100


def _atomic_exchange(source: Path, destination: Path) -> None:
    system = platform.system()
    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    target_raw = os.fsencode(destination)
    if system == "Linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        code = rename(AT_FDCWD, source_raw, AT_FDCWD, target_raw, EXCHANGE)
    elif system == "Darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        code = rename(source_raw, target_raw, EXCHANGE)
    else:
        raise RuntimeError("atomic directory exchange unavailable on this platform")
    if code:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _entry_sha(directory: Path) -> str:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("live/staged static path must be a real directory")
    index = directory / "index.html"
    if index.is_symlink() or not index.is_file():
        raise ValueError("missing static entrypoint")
    match = re.search(r'gpoint-build-sha" content="([0-9a-f]{40})', index.read_text("utf8"))
    if not match:
        raise ValueError("static entrypoint contains no exact build SHA")
    return match.group(1)


def exchange_site(site_root: Path, baseline: str, candidate: str) -> dict:
    """Exchange live site with immutable staged folder, returning rollback facts.

    Call only after canonical V3 preflight, application lock and OWNER approval.
    The staging dir is kept as the prior site for rollback.
    """
    if not SHA.fullmatch(baseline or "") or not SHA.fullmatch(candidate or "") or candidate == baseline:
        raise ValueError("exact nonidentical release SHAs required")
    root = site_root.resolve(strict=True)
    app = root / "app"
    if app.is_symlink() or not app.is_dir() or app.resolve(strict=True) != app:
        raise ValueError("unexpected app directory")
    live = app / "miniapp"
    staged = app / ("static-v4-" + candidate)
    if live.is_symlink() or staged.is_symlink():
        raise ValueError("refuse symlink or missing regular directory")
    if _entry_sha(live) != baseline or _entry_sha(staged) != candidate:
        raise ValueError("live/staged SHA drift")
    if os.stat(live).st_dev != os.stat(staged).st_dev:
        raise ValueError("staging and live must be on same filesystem")
    _atomic_exchange(live, staged)
    if _entry_sha(live) != candidate or _entry_sha(staged) != baseline:
        raise RuntimeError("atomic exchange result needs recovery")
    return {
        "candidateSha": candidate, "previousSourceSha": baseline,
        "sitePath": str(live), "rollbackPath": str(staged), "mode": "RENAME_EXCHANGE",
    }


def restore_site(site_root: Path, candidate: str, baseline: str) -> None:
    if not SHA.fullmatch(baseline or "") or not SHA.fullmatch(candidate or ""):
        raise ValueError("bad rollback SHAs")
    root = site_root.resolve(strict=True)
    live = root / "app" / "miniapp"
    staged = root / "app" / ("static-v4-" + candidate)
    if live.is_symlink() or staged.is_symlink() or _entry_sha(live) != candidate or _entry_sha(staged) != baseline:
        raise ValueError("rollback runtime drift: manual recovery needed")
    _atomic_exchange(live, staged)
    if _entry_sha(live) != baseline:
        raise RuntimeError("rollback recovery required")


if __name__ == "__main__":
    raise SystemExit("This is an import-only atomic primitive, not a production deploy CLI")
