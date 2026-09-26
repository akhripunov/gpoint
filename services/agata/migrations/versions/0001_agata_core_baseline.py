"""Fresh, schema-isolated baseline for the GPoint AGATA core.

Revision ID: agata0001
Revises:
"""

from __future__ import annotations

import os
import re
from uuid import UUID

from alembic import op
from sqlalchemy import text

from app.db.models import Base

revision = "agata0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = tuple(sorted(table.name for table in Base.metadata.tables.values()))


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE SCHEMA IF NOT EXISTS agata")
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)

    tenant_value = os.environ.get("AGATA_TENANT_ID")
    if not tenant_value:
        raise RuntimeError("AGATA_TENANT_ID is required for the isolated AGATA tenant bootstrap")
    tenant_id = UUID(tenant_value)
    op.get_bind().execute(
        text("INSERT INTO agata.tenants(id) VALUES (:tenant) ON CONFLICT (id) DO NOTHING"),
        {"tenant": tenant_id},
    )
    op.get_bind().execute(
        text(
            "INSERT INTO agata.regional_policy_profiles "
            "(tenant_id,jurisdiction,market,version,status,effective_from,rules) "
            "VALUES (:tenant,'GLOBAL','GPOINT',1,'active',now(), "
            "jsonb_build_object('ai_disclosure_required',true,"
            "'self_attestation_allowed',false,'adult_only_features','[]'::jsonb,"
            "'disclosure_repeat_hours',24)) "
            "ON CONFLICT (tenant_id,jurisdiction,market,version) DO NOTHING"
        ),
        {"tenant": tenant_id},
    )

    op.execute(
        "CREATE OR REPLACE FUNCTION agata.enforce_response_ref_supplied() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.response_run_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM agata.response_runs r JOIN agata.retrieval_candidates c "
        "ON c.tenant_id=r.tenant_id AND c.run_id=r.retrieval_run_id "
        "AND c.memory_id=NEW.memory_id AND c.memory_version=NEW.memory_version AND c.selected "
        "WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.response_run_id "
        "AND r.user_id=NEW.user_id AND r.character_id=NEW.character_id "
        "AND r.scope=NEW.scope AND r.scenario_key=NEW.scenario_key) THEN "
        "RAISE EXCEPTION 'response reference is not a supplied retrieval candidate'; "
        "END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER response_ref_supplied_guard BEFORE INSERT OR UPDATE "
        "ON agata.response_memory_refs FOR EACH ROW "
        "EXECUTE FUNCTION agata.enforce_response_ref_supplied()"
    )
    for table in TABLES:
        op.execute(f'ALTER TABLE agata."{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE agata."{table}" FORCE ROW LEVEL SECURITY')
        if table == "tenants":
            expression = "id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        else:
            expression = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON agata."{table}" '
            f"USING ({expression}) WITH CHECK ({expression})"
        )

    runtime_role = os.environ.get("AGATA_RUNTIME_DB_ROLE")
    if runtime_role:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role):
            raise RuntimeError("AGATA_RUNTIME_DB_ROLE must be a simple PostgreSQL role name")
        op.execute(f'GRANT USAGE ON SCHEMA agata TO "{runtime_role}"')
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agata TO "{runtime_role}"'
        )
        op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA agata TO "{runtime_role}"')
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA agata GRANT SELECT, INSERT, UPDATE, DELETE "
            f'ON TABLES TO "{runtime_role}"'
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA agata GRANT USAGE, SELECT "
            f'ON SEQUENCES TO "{runtime_role}"'
        )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS agata CASCADE")
