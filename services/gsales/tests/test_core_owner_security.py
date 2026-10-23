"""Real PostgreSQL regression for legacy GPoint owner privilege escalation."""
import os

import psycopg
import pytest

pytestmark=pytest.mark.skipif(not os.getenv('GSALES_TEST_ADMIN_DSN'),reason='disposable PostgreSQL required')
GPOINT='7508f36f-774a-48aa-8c6b-42f6f160415d'
GSALES='c4d67889-877e-4d1b-a8d8-597c479d638f'


def test_former_core_owner_cannot_remove_rls_or_replace_identity_helpers():
    for env,own,other in [('GSALES_TEST_GPOINT_DSN',GPOINT,GSALES),('GSALES_TEST_AGATA_DSN',GSALES,GPOINT)]:
        with psycopg.connect(os.environ[env]) as conn:
            for statement in (
                'ALTER TABLE agata.users DISABLE ROW LEVEL SECURITY',
                'ALTER TABLE agata.users NO FORCE ROW LEVEL SECURITY',
                'DROP POLICY runtime_login_tenant ON agata.users',
                'DROP SCHEMA agata CASCADE',
                'CREATE FUNCTION agata.owner_escape() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$',
                "CREATE OR REPLACE FUNCTION agata.runtime_tenant_id() RETURNS uuid LANGUAGE sql AS $$ SELECT '"+other+"'::uuid $$",
                "UPDATE agata.alembic_version SET version_num='owner-bypass'",
                'TRUNCATE agata.users CASCADE',
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege),conn.transaction():
                    conn.execute(statement)
            conn.execute("SELECT set_config('app.tenant_id',%s,true)",(other,))
            assert conn.execute('SELECT count(*) FROM agata.users').fetchone()[0]==0
            conn.execute("SELECT set_config('app.tenant_id',%s,true)",(own,))
            assert conn.execute('SELECT count(*) FROM agata.users').fetchone()[0]>=1
            assert conn.execute('SELECT version_num FROM agata.alembic_version').fetchone()[0]=='agata0012'
            assert not conn.execute("SELECT has_schema_privilege(session_user,'agata','CREATE')").fetchone()[0]
