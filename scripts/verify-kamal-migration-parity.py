#!/usr/bin/env python3
"""Fail closed before cutover unless the candidate requires no DB migrations."""
import argparse
import ast
from pathlib import Path
import re
import sys


def prisma_names(directory):
    rows = []
    for path in sorted(Path(directory).iterdir()):
        if not path.is_dir():
            continue
        if not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", path.name):
            raise ValueError("candidate Prisma migration directory name is invalid")
        if not (path / "migration.sql").is_file():
            raise ValueError("candidate Prisma migration SQL is missing")
        rows.append(path.name)
    if not rows:
        raise ValueError("candidate Prisma migration inventory is empty")
    return rows


def literal_assignments(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"revision", "down_revision"}:
                values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def alembic_head(directory):
    revisions = {}
    for path in sorted(Path(directory).glob("*.py")):
        values = literal_assignments(path)
        revision = values.get("revision")
        parents = values.get("down_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_]+", revision) or revision in revisions:
            raise ValueError("candidate Alembic revision identity is invalid or duplicated")
        if parents is None:
            parents = ()
        elif isinstance(parents, str):
            parents = (parents,)
        elif isinstance(parents, tuple) and all(isinstance(item, str) for item in parents):
            pass
        else:
            raise ValueError("candidate Alembic parent identity is unsupported")
        revisions[revision] = parents
    if not revisions:
        raise ValueError("candidate Alembic migration inventory is empty")
    for parents in revisions.values():
        if any(parent not in revisions for parent in parents):
            raise ValueError("candidate Alembic migration has an unresolved parent")
    heads = sorted(set(revisions) - {parent for parents in revisions.values() for parent in parents})
    if len(heads) != 1:
        raise ValueError("candidate Alembic migration graph must have exactly one head")
    return heads[0]


def verify(args):
    candidate_prisma = prisma_names(args.prisma_directory)
    live_prisma = args.prisma_current_migrations.split(",") if args.prisma_current_migrations else []
    if any(not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name) for name in live_prisma):
        raise ValueError("live Prisma migration inventory is malformed")
    if len(live_prisma) != len(set(live_prisma)):
        raise ValueError("live Prisma migration inventory contains duplicates")
    if set(live_prisma) != set(candidate_prisma):
        raise ValueError("PENDING_OR_UNRECONCILED_PRISMA_MIGRATIONS")
    candidate_prisma_head = candidate_prisma[-1]
    if args.prisma_current_head != candidate_prisma_head:
        raise ValueError("PENDING_PRISMA_HEAD_MISMATCH")
    candidate_agata_head = alembic_head(args.agata_directory)
    if args.agata_current_head != candidate_agata_head:
        raise ValueError("PENDING_AGATA_HEAD_MISMATCH")
    return candidate_prisma_head, candidate_agata_head


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prisma-directory", required=True)
    parser.add_argument("--prisma-current-migrations", required=True)
    parser.add_argument("--prisma-current-head", required=True)
    parser.add_argument("--agata-directory", required=True)
    parser.add_argument("--agata-current-head", required=True)
    args = parser.parse_args()
    try:
        prisma_head, agata_head = verify(args)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"GPOINT_KAMAL_MIGRATION_PARITY=BLOCKED reason={exc}", file=sys.stderr)
        return 1
    print(f"GPOINT_KAMAL_MIGRATION_PARITY=PASS PRISMA_HEAD={prisma_head} AGATA_HEAD={agata_head}")
    print("GPOINT_KAMAL_DB_MIGRATION_REQUIRED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
