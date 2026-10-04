import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.config import GSALES_TENANT_ID, Settings
from app.db.session import TABLES, Database, GateError

pytestmark = pytest.mark.skipif(
    not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL runner required"
)
GPOINT = "7508f36f-774a-48aa-8c6b-42f6f160415d"


def test_both_tenants_isolated_even_with_direct_sql_context_spoofing():
    for env, own, other in (
        ("GSALES_TEST_AGATA_DSN", GSALES_TENANT_ID, GPOINT),
        ("GSALES_TEST_GPOINT_DSN", GPOINT, GSALES_TENANT_ID),
    ):
        with psycopg.connect(os.environ[env]) as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (own,))
            assert (
                conn.execute(
                    "SELECT count(*) FROM agata.users WHERE tenant_id=%s", (own,)
                ).fetchone()[0]
                >= 1
            )
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (other,))
            assert conn.execute("SELECT count(*) FROM agata.users").fetchone()[0] == 0
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (own,))
            tables = conn.execute(
                """SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='agata' AND c.relkind='r' AND (c.relname='tenants' OR EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped))"""
            ).fetchall()
            assert len(tables) >= 33
            for table, rls, forced in tables:
                assert rls and forced
                col = "id" if table == "tenants" else "tenant_id"
                assert (
                    conn.execute(
                        sql.SQL("SELECT count(*) FROM agata.{} WHERE {}=%s").format(
                            sql.Identifier(table), sql.Identifier(col)
                        ),
                        (other,),
                    ).fetchone()[0]
                    == 0
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("SET ROLE gsales_runtime")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("SET SESSION AUTHORIZATION postgres")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("SELECT * FROM agata_identity.role_tenants")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(
                    "INSERT INTO agata.users(tenant_id,external_id) VALUES (%s,'cross-tenant')",
                    (other,),
                )


def test_gsales_rls_identity_is_bound_to_database_login_not_fixture_uuid():
    with psycopg.connect(os.environ["GSALES_TEST_RUNTIME_DSN"]) as conn:
        assert str(conn.execute("SELECT gsales.runtime_tenant_id()").fetchone()[0]) == GSALES_TENANT_ID
        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (GPOINT,))
        assert conn.execute("SELECT count(*) FROM gsales.sellers WHERE tenant_id=%s", (GPOINT,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_runtime_gsales_security_gate_and_unsafe_admin_refusal():
    for env in ("GSALES_TEST_RUNTIME_DSN", "GSALES_TEST_ADMIN_DSN"):
        db = Database(Settings(database_url=os.environ[env]))
        async with db.transaction() as conn:
            if env.endswith("ADMIN_DSN"):
                with pytest.raises(GateError, match="runtime_role_unsafe"):
                    await db.assert_secure(conn)
            else:
                await db.assert_secure(conn)


def test_schema_migration_preserves_public_and_no_cross_product_fk():
    with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as conn:
        assert conn.execute("SELECT to_regclass('public.gsales_public_probe')").fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='gsales'"
            ).fetchone()[0]
            == 6
        )
        assert (
            conn.execute(
                """SELECT count(*) FROM pg_constraint c JOIN pg_class source ON source.oid=c.conrelid JOIN pg_namespace n ON n.oid=source.relnamespace JOIN pg_class target ON target.oid=c.confrelid JOIN pg_namespace t ON t.oid=target.relnamespace WHERE n.nspname='gsales' AND c.contype='f' AND t.nspname<>'gsales'"""
            ).fetchone()[0]
            == 0
        )
    with psycopg.connect(os.environ["GSALES_TEST_RUNTIME_DSN"]) as conn:
        for table in TABLES:
            conn.execute(sql.SQL("SELECT count(*) FROM gsales.{}").format(sql.Identifier(table)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT * FROM agata.users")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("ALTER TABLE gsales.leads DISABLE ROW LEVEL SECURITY")


def test_business_cross_tenant_writes_rejected_under_spoofed_context():
    with psycopg.connect(os.environ["GSALES_TEST_RUNTIME_DSN"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (GPOINT,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO gsales.sellers(tenant_id,agata_character_id,display_name) VALUES (%s,%s,'wrong')",
                (GPOINT, uuid4()),
            )


def test_runtime_refuses_role_creation_privilege():
    with psycopg.connect(os.environ['GSALES_TEST_ADMIN_DSN']) as admin:
        admin.execute('ALTER ROLE gsales_runtime CREATEROLE')
    try:
        import asyncio
        async def check():
            db=Database(Settings(database_url=os.environ['GSALES_TEST_RUNTIME_DSN']))
            async with db.transaction() as c:
                with pytest.raises(GateError,match='runtime_role_unsafe'):
                    await db.assert_secure(c)
        asyncio.run(check())
    finally:
        with psycopg.connect(os.environ['GSALES_TEST_ADMIN_DSN']) as admin:
            admin.execute('ALTER ROLE gsales_runtime NOCREATEROLE')


@pytest.mark.asyncio
async def test_runtime_security_rejects_configured_tenant_different_from_login_binding():
    db = Database(Settings(database_url=os.environ["GSALES_TEST_RUNTIME_DSN"], tenant_id=GPOINT))
    async with db.transaction() as conn:
        with pytest.raises(GateError, match="gsales_runtime_tenant_binding_mismatch"):
            await db.assert_secure(conn)
