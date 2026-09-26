"""Explicit integration test for a disposable, initially empty AGATA test database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).parents[1]
DATABASE_URL = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AGATA_TEST_DATABASE_URL not set")


def test_empty_database_baseline_only_adds_agata_schema() -> None:
    assert DATABASE_URL is not None
    url = make_url(DATABASE_URL)
    assert url.database and url.database.endswith("_agata_test"), (
        "refusing migration outside an explicitly disposable *_agata_test database"
    )
    sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    engine = create_engine(sync_url)
    with engine.connect() as connection:
        before = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        )
        assert (
            connection.scalar(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name='agata'")
            )
            is None
        ), "test database must begin without schema agata"

    env = dict(os.environ)
    env["AGATA_DATABASE_URL"] = DATABASE_URL
    env.setdefault("AGATA_TENANT_ID", "7508f36f-774a-48aa-8c6b-42f6f160415d")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )

    with engine.connect() as connection:
        after = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        )
        agata_tables = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='agata'"))
        )
        assert after == before
        assert "alembic_version" in agata_tables
        assert (
            connection.scalar(text("SELECT version_num FROM agata.alembic_version")) == "agata0001"
        )
        assert connection.scalar(text("SELECT count(*) FROM agata.tenants")) == 1
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_policies WHERE schemaname='agata'"))
            == len(agata_tables) - 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='agata' AND c.relrowsecurity AND c.relforcerowsecurity"
                )
            )
            == len(agata_tables) - 1
        )
    engine.dispose()
