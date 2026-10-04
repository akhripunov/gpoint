"""Bind forced tenant RLS to the authenticated PostgreSQL login identity.

Revision ID: agata0011
Revises: agata0010
"""

import json
import os
import re
from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "agata0011"
down_revision = "agata0010"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # Serialize the exact tenant census with concurrent tenant provisioning and
    # keep it stable through binding/RLS installation. The release preflight
    # cannot hold a relation lock here: this migration itself changes RLS on
    # agata.tenants and PostgreSQL DDL needs ACCESS EXCLUSIVE.
    bind.execute(text("LOCK TABLE agata.tenants IN ACCESS EXCLUSIVE MODE"))
    bindings = json.loads(os.environ.get("AGATA_ROLE_TENANT_BINDINGS", "{}"))
    if not isinstance(bindings, dict) or not bindings:
        raise RuntimeError("AGATA_ROLE_TENANT_BINDINGS must be an explicit role-to-UUID object")
    try:
        bound_tenants = {str(UUID(value)) for value in bindings.values()}
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("AGATA_ROLE_TENANT_BINDINGS must map runtime logins to tenant UUIDs") from exc
    privileged = bind.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"))
    if not privileged:
        raise RuntimeError("agata0011 requires a trusted bypass migration administrator to enumerate every tenant")
    existing_tenants = {str(value) for value in bind.execute(text("SELECT id FROM agata.tenants FOR SHARE")).scalars()}
    if existing_tenants != bound_tenants:
        missing = existing_tenants - bound_tenants
        extra = bound_tenants - existing_tenants
        raise RuntimeError(
            "runtime tenant inventory must exactly match agata.tenants "
            f"(missing={len(missing)}, unknown={len(extra)})"
        )
    for role, tenant in bindings.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", role):
            raise RuntimeError("invalid runtime role binding")
        UUID(tenant)
        safe = bind.scalar(
            text(
                "SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles WHERE rolname=:role"
            ),
            {"role": role},
        )
        if safe is not True:
            raise RuntimeError("runtime binding requires an existing non-superuser/non-bypass role")
    op.execute("CREATE SCHEMA IF NOT EXISTS agata_identity")
    op.execute("REVOKE ALL ON SCHEMA agata_identity FROM PUBLIC")
    op.execute(
        "CREATE TABLE IF NOT EXISTS agata_identity.role_tenants (login_role text PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES agata.tenants(id) ON DELETE RESTRICT)"
    )
    op.execute("REVOKE ALL ON agata_identity.role_tenants FROM PUBLIC")
    for role, tenant in bindings.items():
        bind.execute(
            text(
                "INSERT INTO agata_identity.role_tenants VALUES (:role, :tenant) ON CONFLICT(login_role) DO UPDATE SET tenant_id=EXCLUDED.tenant_id"
            ),
            {"role": role, "tenant": UUID(tenant)},
        )
    op.execute("""
        CREATE FUNCTION agata.runtime_tenant_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog
        AS $$ SELECT tenant_id FROM agata_identity.role_tenants WHERE login_role=session_user $$
    """)
    # FORCE RLS cannot constrain an owner that can disable RLS or replace its
    # policy/function. Move every AGATA object behind the trusted migrator and
    # reissue only the application privileges, including the original GPoint
    # login that owned the baseline schema. This is confined to AGATA; public
    # Prisma ownership and grants are deliberately untouched.
    op.execute("ALTER SCHEMA agata OWNER TO CURRENT_USER")
    op.execute("ALTER SCHEMA agata_identity OWNER TO CURRENT_USER")
    op.execute("ALTER TABLE agata_identity.role_tenants OWNER TO CURRENT_USER")
    op.execute("REVOKE ALL ON SCHEMA agata FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA agata FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA agata FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agata FROM PUBLIC")
    op.execute("""
        DO $$ DECLARE obj record; BEGIN
          FOR obj IN SELECT c.relname,c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='agata' AND c.relkind IN ('r','p','v','m') LOOP
            EXECUTE format('ALTER %s agata.%I OWNER TO %I',
              CASE obj.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
              obj.relname,current_user);
          END LOOP;
          FOR obj IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='agata' AND c.relkind='S' LOOP
            EXECUTE format('ALTER SEQUENCE agata.%I OWNER TO %I',obj.relname,current_user);
          END LOOP;
          FOR obj IN SELECT t.typname,t.typtype FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
            WHERE n.nspname='agata' AND t.typtype IN ('e','d') LOOP
            EXECUTE format('ALTER %s agata.%I OWNER TO %I',
              CASE obj.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,obj.typname,current_user);
          END LOOP;
          FOR obj IN SELECT format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) AS identity
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='agata' AND p.prokind='f' LOOP
            EXECUTE format('ALTER FUNCTION %s OWNER TO %I',obj.identity,current_user);
          END LOOP;
        END $$
    """)
    for role in bindings:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(f"REVOKE ALL ON SCHEMA agata, agata_identity FROM {quoted}")
        op.execute(f"REVOKE ALL ON agata_identity.role_tenants FROM {quoted}")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA agata FROM {quoted}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA agata FROM {quoted}")
        op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agata FROM {quoted}")
        # Business GSales owns only its separate schema; its binding is used by
        # the GSales SECURITY DEFINER helper, never direct Core table access.
        if role == "gsales_runtime":
            continue
        op.execute(f"GRANT USAGE ON SCHEMA agata TO {quoted}")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA agata TO {quoted}")
        op.execute(f"REVOKE INSERT,UPDATE,DELETE ON agata.alembic_version FROM {quoted}")
        op.execute(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA agata TO {quoted}")
        op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA agata TO {quoted}")

    op.execute("ALTER TABLE agata.outbox_jobs DROP CONSTRAINT ck_outbox_job_type_allowlist")
    op.execute(
        "ALTER TABLE agata.outbox_jobs ADD CONSTRAINT ck_outbox_job_type_allowlist CHECK (job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','memory_graph_sync','response_retry','inference','sales_draft_inference'))"
    )
    op.execute(
        "ALTER TABLE agata.messages ADD COLUMN IF NOT EXISTS channel_delivery_status varchar(16) NOT NULL DEFAULT 'confirmed'"
    )
    op.execute(
        "ALTER TABLE agata.messages ADD COLUMN IF NOT EXISTS channel_message_id varchar(128)"
    )
    tables = bind.execute(
        text("""
        SELECT c.relname, EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped) AS has_tenant
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='agata' AND c.relkind='r' AND (c.relname='tenants' OR EXISTS(
            SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname='tenant_id' AND NOT a.attisdropped))
    """)
    ).all()
    for table, has_tenant in tables:
        if not re.fullmatch(r"[a-z_]+", table):
            raise RuntimeError("unexpected AGATA relation identifier")
        col = "tenant_id" if has_tenant else "id"
        op.execute(f'ALTER TABLE agata."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE agata."{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY runtime_login_tenant ON agata."{table}" AS RESTRICTIVE FOR ALL USING ({col}=agata.runtime_tenant_id()) WITH CHECK ({col}=agata.runtime_tenant_id())'
        )


def downgrade():
    raise RuntimeError(
        "agata0011 is forward-only: removing login binding would expose cross-tenant data"
    )
