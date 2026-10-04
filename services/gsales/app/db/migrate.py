"""Explicit G Sales migrator; runtime startup never runs DDL."""

import argparse
import hashlib
import os
from pathlib import Path

import psycopg

from app.db.session import GateError


def apply(conn, directory: Path):
    conn.execute("SELECT pg_advisory_xact_lock(723803)")
    role = conn.execute("SELECT session_user").fetchone()[0]
    if role == "gsales_runtime":
        raise GateError("runtime_cannot_migrate")
    conn.execute("CREATE SCHEMA IF NOT EXISTS gsales_meta")
    conn.execute("REVOKE ALL ON SCHEMA gsales_meta FROM PUBLIC")
    conn.execute("""CREATE TABLE IF NOT EXISTS gsales_meta.migrations
        (version text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())""")
    for path in sorted(directory.glob("*.sql")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = conn.execute(
            "SELECT sha256 FROM gsales_meta.migrations WHERE version=%s", (path.stem,)
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise GateError("migration_checksum_changed")
            continue
        conn.execute(path.read_text())
        conn.execute(
            "INSERT INTO gsales_meta.migrations(version,sha256) VALUES(%s,%s)", (path.stem, digest)
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--owner-approved-sha", required=True)
    args = parser.parse_args()
    import re

    if (
        not args.apply
        or not re.fullmatch("[0-9a-f]{40}", args.candidate)
        or args.owner_approved_sha != args.candidate
    ):
        raise GateError("exact_sha_migration_approval_required")
    dsn = os.environ.get("GSALES_MIGRATION_DATABASE_URL")
    if not dsn:
        raise GateError("migration_database_not_configured")
    directory = Path(__file__).resolve().parents[2] / "migrations"
    with psycopg.connect(dsn) as conn:
        apply(conn, directory)
    print("G Sales migrations applied; runtime rollout remains a separate release gate.")


if __name__ == "__main__":
    main()
