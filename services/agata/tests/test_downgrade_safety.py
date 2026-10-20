"""PostgreSQL guard test: ordinary Alembic downgrade must be a no-op on the schema."""

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


def test_cli_downgrade_refuses_without_dropping_schema() -> None:
    assert DATABASE_URL is not None
    url = make_url(DATABASE_URL)
    assert url.database and url.database.endswith("_agata_test"), (
        "refusing downgrade outside an explicitly disposable *_agata_test database"
    )
    env = dict(os.environ)
    env["AGATA_DATABASE_URL"] = DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "downgrade", "base"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "AGATA downgrades are disabled" in result.stderr
    engine = create_engine(DATABASE_URL.replace("+asyncpg://", "+psycopg://", 1))
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name='agata'")
        ) == 1
        assert connection.scalar(text("SELECT version_num FROM agata.alembic_version")) == "agata0012"
    engine.dispose()
