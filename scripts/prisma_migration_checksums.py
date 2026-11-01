#!/usr/bin/env python3
"""Build and verify SHA-256 inventories for Prisma migration history."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys


MIGRATION_NAME = re.compile(r"^[0-9]{14}_[a-z0-9_]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def build_inventory(migrations_dir: Path) -> list[tuple[str, str]]:
    migrations_dir = Path(migrations_dir).resolve(strict=True)
    if not migrations_dir.is_dir():
        raise ValueError("Prisma migrations path must be a directory")
    inventory: list[tuple[str, str]] = []
    for migration_dir in sorted(migrations_dir.iterdir(), key=lambda path: path.name):
        sql_path = migration_dir / "migration.sql"
        if not sql_path.exists():
            continue
        if (not MIGRATION_NAME.fullmatch(migration_dir.name)
                or migration_dir.is_symlink() or not migration_dir.is_dir()
                or sql_path.is_symlink() or not sql_path.is_file()):
            raise ValueError("invalid Prisma migration source inventory")
        digest = hashlib.sha256(sql_path.read_bytes()).hexdigest()
        inventory.append((migration_dir.name, digest))
    if not inventory:
        raise ValueError("Prisma migration source inventory is empty")
    return inventory


def _parse_lines(lines: list[str], *, label: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in lines:
        if not line or line.count("=") != 1:
            raise ValueError(f"invalid {label} checksum entry")
        name, digest = line.split("=", 1)
        if not MIGRATION_NAME.fullmatch(name) or not SHA256.fullmatch(digest):
            raise ValueError(f"invalid {label} checksum entry")
        if name in entries:
            raise ValueError(f"duplicate {label} migration")
        entries[name] = digest
    if not entries:
        raise ValueError(f"{label} checksum inventory is empty")
    return entries


def verify_applied_migrations(source_lines: list[str], applied_lines: list[str], *,
                              expected_count: int) -> int:
    source = _parse_lines(source_lines, label="source")
    applied = _parse_lines(applied_lines, label="database")
    if len(applied) != expected_count:
        raise ValueError("database migration inventory count differs from baseline")
    if len(source) < len(applied):
        raise ValueError("source migration inventory is incomplete")
    for name, recorded_digest in applied.items():
        if source.get(name) != recorded_digest:
            raise ValueError("database migration checksum differs from exact source SHA")
    return len(applied)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--migrations-dir", required=True, type=Path)
    write_parser.add_argument("--output", required=True, type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--applied", required=True, type=Path)
    verify_parser.add_argument("--expected-count", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command == "write":
            inventory = build_inventory(args.migrations_dir)
            payload = "".join(f"{name}={digest}\n" for name, digest in inventory)
            with args.output.open("x", encoding="ascii") as output:
                output.write(payload)
            args.output.chmod(0o600)
            print(f"GPOINT_PRISMA_SOURCE_MIGRATIONS={len(inventory)}")
            return 0
        count = verify_applied_migrations(
            args.manifest.read_text(encoding="ascii").splitlines(),
            args.applied.read_text(encoding="ascii").splitlines(),
            expected_count=args.expected_count,
        )
        print(f"GPOINT_APPLIED_PRISMA_CHECKSUMS=PASS ({count})")
        return 0
    except (OSError, ValueError) as exc:
        print(f"PRISMA_MIGRATION_CHECKSUMS_BLOCKED={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
