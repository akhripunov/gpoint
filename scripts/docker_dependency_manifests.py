#!/usr/bin/env python3
"""Create a stable BuildKit context for dependency installation only."""
from __future__ import annotations

import json
from pathlib import Path
import sys


MANIFEST_PATHS = (
    "package.json",
    "apps/api/package.json",
    "apps/bot/package.json",
    "apps/miniapp/package.json",
    "apps/worker/package.json",
    "packages/agata-client/package.json",
    "packages/contracts/package.json",
    "packages/db/package.json",
)
# The workspace glob also contains this standalone local MCP package. Its
# manifest is intentionally absent from the production dependency-install
# projection: no production app declares it as a dependency. The later source
# copy may still include its files; this exclusion is not a packaging filter.
PRODUCTION_WORKSPACE_MANIFEST_EXCLUSIONS = (
    "packages/gpoint-codex-mcp/package.json",
)
INSTALL_LIFECYCLE_SCRIPTS = frozenset(("preinstall", "install", "postinstall", "prepare"))


def dependency_projection(manifest: object) -> dict:
    """Keep all package metadata except scripts unrelated to installation."""
    if not isinstance(manifest, dict):
        raise ValueError("package manifest must be a JSON object")
    result = dict(manifest)
    scripts = result.get("scripts")
    if scripts is not None:
        if not isinstance(scripts, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in scripts.items()):
            raise ValueError("package scripts must be a string map")
        lifecycle = {key: value for key, value in scripts.items()
                     if key in INSTALL_LIFECYCLE_SCRIPTS}
        if lifecycle:
            result["scripts"] = lifecycle
        else:
            result.pop("scripts", None)
    return result


def write_context(repo: Path, destination: Path) -> int:
    repo = Path(repo).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.is_relative_to(repo):
        raise ValueError("dependency manifest context must be outside the source repository")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("dependency manifest context must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    for relative in MANIFEST_PATHS:
        source = repo / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(repo):
            raise ValueError("package manifest must be a regular file inside the source repository")
        manifest = json.loads(source.read_text(encoding="utf-8"))
        projected = dependency_projection(manifest)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(projected, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) + "\n", encoding="utf-8")
    return len(MANIFEST_PATHS)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: docker_dependency_manifests.py <output-directory>")
    count = write_context(Path(__file__).resolve().parents[1], Path(sys.argv[1]))
    print(f"DOCKER_DEPENDENCY_MANIFESTS={count}")


if __name__ == "__main__":
    main()
