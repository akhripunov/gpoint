#!/usr/bin/env python3
"""Classify the changed surface of a candidate for CI and release scoping.

A thin adapter over the existing path classification in
``native_release_intent.py`` so Tests, Build and Deploy share one source of
truth instead of each re-deriving component scope.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from native_release_intent import _path_kind  # noqa: E402

EROS_PREFIXES = (
    "services/eros-engine/",
    "docker/",
    "patches/",
    "infra/production/eros",
    "config/deploy.eros",
    "config/deploy.layer7-staging.eros",
)
MINIAPP_PREFIXES = ("apps/miniapp/",)
AGATA_PREFIXES = ("services/agata/",)
CORE_PREFIXES = ("apps/api/", "apps/bot/", "apps/worker/", "packages/")
DOCS_PREFIXES = ("docs/", "design/")
DOCS_ROOT_FILES = frozenset({"README.md", "AGENTS.md"})
DATABASE_KINDS = frozenset({"schema", "prisma_schema", "prisma_migration"})
RELEASE_INFRA_PREFIXES = (".github/", "scripts/", ".kamal/", "systemd/")
FLAGS = ("eros", "miniapp", "agata", "core", "database", "docs_only",
         "release_infra", "full_ci", "app_backend")


def _matches(path: str, prefixes) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def classify(paths) -> dict:
    files = sorted({path.strip() for path in paths if path.strip()})
    kinds = {_path_kind(path) for path in files}
    surface = {
        "files": len(files),
        "unknown_paths": sorted(path for path in files if _path_kind(path) == "unknown"),
        "eros": any(_matches(path, EROS_PREFIXES) for path in files),
        "miniapp": any(_matches(path, MINIAPP_PREFIXES) for path in files),
        "agata": any(_matches(path, AGATA_PREFIXES) for path in files),
        "core": any(
            _matches(path, CORE_PREFIXES) and not _matches(path, MINIAPP_PREFIXES)
            for path in files
        ),
        "database": bool(kinds & DATABASE_KINDS),
        "docs_only": bool(files) and all(
            path in DOCS_ROOT_FILES or _matches(path, DOCS_PREFIXES) for path in files
        ),
    }
    # Fail closed: unknown paths, database changes and release infrastructure
    # always select the full suite.
    surface["release_infra"] = any(
        _matches(path, RELEASE_INFRA_PREFIXES) for path in files
    )
    surface["full_ci"] = bool(
        surface["unknown_paths"]
        or surface["database"]
        or surface["release_infra"]
        or not files
    )
    surface["app_backend"] = bool(surface["core"] or surface["agata"])
    return surface


def changed_paths(base: str, head: str) -> list:
    output = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if not args.base:
        surface = classify([])
        surface["reason"] = "no base revision: full suite"
    else:
        try:
            surface = classify(changed_paths(args.base, args.head))
            surface["reason"] = "diff"
        except subprocess.CalledProcessError as error:
            print(f"native_change_surface: {error}", file=sys.stderr)
            surface = classify([])
            surface["reason"] = "diff failed: full suite"

    if args.output:
        with open(args.output, "a", encoding="utf-8") as stream:
            for key in FLAGS:
                stream.write(f"{key}={str(surface[key]).lower()}\n")
    print(json.dumps(surface, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
