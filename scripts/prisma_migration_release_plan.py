#!/usr/bin/env python3
"""Fail-closed plan for the exact Prisma migrations a Native Deploy may apply."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


MIGRATION_NAME = re.compile(r"^[0-9]{14}_[a-z0-9_]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWLIST_SCHEMA = "gpoint-prisma-production-apply/v1"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def release_scope_migrations(repo: Path, baseline_sha: str, candidate_sha: str) -> set[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", baseline_sha or "") or not re.fullmatch(
        r"[0-9a-f]{40}", candidate_sha or ""
    ):
        raise ValueError("production baseline or candidate SHA is invalid")
    raw = subprocess.check_output(
        ["git", "diff", "--name-status", "-z", "--no-renames",
         f"{baseline_sha}..{candidate_sha}", "--", "packages/db/prisma/migrations"],
        cwd=repo,
    )
    parts = raw.decode("utf-8").split("\x00")
    if not parts or parts.pop() != "" or len(parts) % 2:
        raise ValueError("production migration scope diff is malformed")
    names = set()
    for status, path in zip(parts[::2], parts[1::2]):
        match = re.fullmatch(
            r"packages/db/prisma/migrations/([0-9]{14}_[a-z0-9_]+)/migration\.sql", path
        )
        if status != "A" or not match:
            raise ValueError("production migration scope contains a modified or unmapped path")
        names.add(match.group(1))
    return names


def migration_inventory(directory: Path) -> list[tuple[str, str]]:
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Prisma migrations path is not a directory")
    rows: list[tuple[str, str]] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        sql = entry / "migration.sql"
        if not sql.exists():
            if entry.is_dir() and MIGRATION_NAME.fullmatch(entry.name):
                raise ValueError(f"Prisma migration is missing migration.sql: {entry.name}")
            continue
        if (entry.is_symlink() or not entry.is_dir() or sql.is_symlink() or not sql.is_file()
                or not MIGRATION_NAME.fullmatch(entry.name)):
            raise ValueError("invalid Prisma migration source inventory")
        rows.append((entry.name, sha256(sql.read_bytes())))
    if not rows:
        raise ValueError("Prisma migration source inventory is empty")
    return rows


def validate_allowlist(value: Any, source: dict[str, str] | None) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != {"schema", "migrations"}
            or value.get("schema") != ALLOWLIST_SCHEMA or not isinstance(value.get("migrations"), dict)):
        raise ValueError("allowlist schema is invalid")
    approved: dict[str, str] = {}
    for name, digest in value["migrations"].items():
        if not isinstance(name, str) or not MIGRATION_NAME.fullmatch(name) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("allowlist entry is malformed")
        if source is not None:
            if name not in source:
                raise ValueError(f"allowlist contains an unknown migration: {name}")
            if digest != source[name]:
                raise ValueError(f"allowlist checksum differs from candidate source: {name}")
        approved[name] = digest
    return approved


def load_allowlist(path: Path) -> dict[str, str]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("production migration allowlist is unavailable or invalid JSON") from None
    return validate_allowlist(value, None)


def build_release_plan(
    migrations_dir: Path,
    applied_migrations: list[str],
    applied_checksums: dict[str, str],
    allowlist: dict[str, str],
    release_scope: set[str],
) -> dict[str, Any]:
    source_rows = migration_inventory(migrations_dir)
    source = dict(source_rows)
    if not applied_migrations or len(applied_migrations) != len(set(applied_migrations)):
        raise ValueError("applied migration inventory is empty or duplicated")
    if source_rows[:len(applied_migrations)] and [name for name, _ in source_rows[:len(applied_migrations)]] != applied_migrations:
        raise ValueError("applied migration history is not a candidate prefix")
    if len(applied_migrations) > len(source_rows):
        raise ValueError("applied migration history is not a candidate prefix")
    if set(applied_checksums) != set(applied_migrations):
        raise ValueError("applied migration checksums do not cover the live inventory")
    for name in applied_migrations:
        if name not in source or applied_checksums[name] != source[name]:
            raise ValueError(f"applied migration checksum differs from candidate source: {name}")
    approved = validate_allowlist(
        {"schema": ALLOWLIST_SCHEMA, "migrations": allowlist}, source
    )
    pending_rows = source_rows[len(applied_migrations):]
    pending: list[dict[str, str]] = []
    for name, digest in pending_rows:
        if name not in release_scope:
            raise ValueError(f"pending migration is outside the confirmed release scope: {name}")
        if approved.get(name) != digest:
            raise ValueError(f"pending migration is not approved with its exact checksum: {name}")
        pending.append({"name": name, "sha256": digest})
    return {"apply": bool(pending), "pending": pending, "source_head": source_rows[-1][0]}


def parse_inventory(value: str, *, label: str) -> dict[str, str] | list[str]:
    if label == "applied migrations":
        names = value.split(",") if value else []
        if any(not MIGRATION_NAME.fullmatch(name) for name in names):
            raise ValueError("applied migration inventory is malformed")
        return names
    result: dict[str, str] = {}
    for item in value.split(",") if value else []:
        name, separator, digest = item.partition("=")
        if (not separator or not MIGRATION_NAME.fullmatch(name) or not SHA256.fullmatch(digest)
                or name in result):
            raise ValueError(f"{label} inventory is malformed")
        result[name] = digest
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrations-dir", type=Path, required=True)
    parser.add_argument("--applied-migrations", required=True)
    parser.add_argument("--applied-checksums", required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    try:
        migrations = parse_inventory(args.applied_migrations, label="applied migrations")
        checksums = parse_inventory(args.applied_checksums, label="applied checksums")
        allowlist = load_allowlist(args.allowlist)
        scope = release_scope_migrations(Path.cwd(), args.baseline_sha, args.candidate_sha)
        plan = build_release_plan(args.migrations_dir, migrations, checksums, allowlist, scope)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"PRISMA_RELEASE_PLAN=BLOCKED reason={error}", file=sys.stderr)
        return 1
    pending_ids = ",".join(item["name"] for item in plan["pending"])
    pending_checksums = ",".join(f'{item["name"]}={item["sha256"]}' for item in plan["pending"])
    print("PRISMA_RELEASE_PLAN=PASS")
    print("apply=" + ("true" if plan["apply"] else "false"))
    print("pending=" + pending_ids)
    print("pending_checksums=" + pending_checksums)
    print("source_head=" + plan["source_head"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
