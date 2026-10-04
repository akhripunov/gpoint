"""Regression for policy migrations running under forced tenant RLS."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).parents[1]
DATABASE_URL = os.getenv("AGATA_RLS_TEST_DATABASE_URL")
TENANT_ID = os.getenv("AGATA_TENANT_ID", "7508f36f-774a-48aa-8c6b-42f6f160415d")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="AGATA_RLS_TEST_DATABASE_URL not set"
)


def test_self_attestation_policy_migration_sets_tenant_context_under_forced_rls() -> None:
    assert DATABASE_URL is not None
    url = make_url(DATABASE_URL)
    assert url.database and url.database.endswith("_agata_test"), (
        "refusing migration outside an explicitly disposable *_agata_test database"
    )
    env = dict(os.environ)
    env["AGATA_DATABASE_URL"] = DATABASE_URL
    env["AGATA_TENANT_ID"] = TENANT_ID
    env["AGATA_MIGRATION_TENANT_IDS"] = TENANT_ID
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "agata0007"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )
    engine = create_engine(DATABASE_URL.replace("+asyncpg://", "+psycopg://", 1))
    try:
        with engine.begin() as connection:
            role = connection.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one()
            assert role == (False, False)
            rls = connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid='agata.regional_policy_profiles'::regclass"
                )
            ).one()
            assert rls == (True, True)
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": TENANT_ID},
            )
            policy = connection.scalar(
                text(
                    "SELECT rules->>'self_attestation_allowed' "
                    "FROM agata.regional_policy_profiles WHERE tenant_id=:tenant "
                    "AND jurisdiction='GLOBAL' AND market='GPOINT' AND status='active'"
                ),
                {"tenant": TENANT_ID},
            )
            assert policy == "false", "agata0007 must reproduce the existing RLS-hidden update"

        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "agata0010"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=60,
            capture_output=True,
            text=True,
        )
        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": TENANT_ID},
            )
            policy = connection.scalar(
                text(
                    "SELECT rules->>'self_attestation_allowed' "
                    "FROM agata.regional_policy_profiles WHERE tenant_id=:tenant "
                    "AND jurisdiction='GLOBAL' AND market='GPOINT' AND status='active'"
                ),
                {"tenant": TENANT_ID},
            )
            assert policy == "true"
            assert connection.scalar(text("SELECT version_num FROM agata.alembic_version")) == "agata0010"
    finally:
        engine.dispose()
