from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.config import Settings

TABLES = (
    "sellers",
    "telegram_accounts",
    "leads",
    "lead_sources",
    "outreach_attempts",
    "dialog_controls",
)


class GateError(ValueError):
    pass


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def transaction(self):
        if self.settings.database_url is None:
            raise GateError("database_not_configured")
        async with await AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            row_factory=dict_row,
            connect_timeout=3,
        ) as conn:
            await conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (self.settings.tenant_id,)
            )
            yield conn

    async def assert_secure(self, conn):
        row = await (
            await conn.execute("""
            SELECT session_user AS login, current_user AS runtime, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,
            EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) AS memberships
            FROM pg_roles WHERE rolname=session_user
        """)
        ).fetchone()
        if (
            not row
            or row["login"] != self.settings.db_role
            or row["runtime"] != row["login"]
            or any(
                row[k]
                for k in ("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb", "memberships")
            )
        ):
            raise GateError("runtime_role_unsafe")
        bound = await (
            await conn.execute("SELECT gsales.runtime_tenant_id() AS tenant_id")
        ).fetchone()
        if not bound or str(bound["tenant_id"]) != self.settings.tenant_id:
            raise GateError("gsales_runtime_tenant_binding_mismatch")
        unsafe_schema = await (
            await conn.execute(
                "SELECT has_schema_privilege(session_user,'gsales','CREATE') AS unsafe"
            )
        ).fetchone()
        if unsafe_schema and unsafe_schema["unsafe"]:
            raise GateError("runtime_schema_create_privilege")
        rows = await (
            await conn.execute(
                """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   pg_get_userbyid(c.relowner)=session_user AS owns_table
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='gsales' AND c.relname = ANY(%s)
        """,
                (list(TABLES),),
            )
        ).fetchall()
        if len(rows) != 6 or any(
            not r["relrowsecurity"] or not r["relforcerowsecurity"] or r["owns_table"] for r in rows
        ):
            raise GateError("gsales_rls_unproven")
        policy = await (
            await conn.execute("""
            SELECT count(*) AS n FROM pg_policies WHERE schemaname='gsales'
            AND policyname='gsales_login_tenant' AND qual LIKE '%%runtime_tenant_id%%'
        """)
        ).fetchone()
        if not policy or policy["n"] != 6:
            raise GateError("gsales_login_binding_missing")
