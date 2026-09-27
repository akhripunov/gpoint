"""Backfill durable relationship owners and constrain tuple consistency.

Revision ID: agata0003
Revises: agata0002
"""

from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "agata0003"
down_revision = "agata0002"
branch_labels = None
depends_on = None


OWNED_TABLES = (
    "conversations",
    "memory_control_state",
    "memories",
    "suppression_rules",
    "retrieval_runs",
    "response_runs",
    "derived_memory_views",
    "shared_history_episodes",
    "shadow_memory_candidates",
)


def _owned_tables() -> tuple[str, ...]:
    inspector = sa.inspect(op.get_bind())
    return tuple(
        table for table in OWNED_TABLES
        if inspector.has_table(table, schema="agata")
    )


def _validate_backfill(owned_tables: tuple[str, ...]) -> None:
    bind = op.get_bind()
    checks = {
        "orphan relationship-owned row": """
            SELECT count(*) FROM (
                {table} t LEFT JOIN agata.relationships r ON
                    r.tenant_id=t.tenant_id AND r.id=t.relationship_id AND
                    r.user_id=t.user_id AND r.character_id=t.character_id AND
                    r.scope=t.scope AND r.scenario_key=t.scenario_key
            ) WHERE r.id IS NULL
        """,
        "relationship tuple duplicate": """
            SELECT count(*) FROM (
                SELECT tenant_id,user_id,character_id,scope,scenario_key
                FROM agata.relationships GROUP BY 1,2,3,4,5 HAVING count(*) <> 1
            ) duplicates
        """,
        "invalid relationship scope/scenario": """
            SELECT count(*) FROM agata.relationships
            WHERE NOT ((scope='real_world' AND scenario_id IS NULL) OR
                       (scope='roleplay' AND scenario_id IS NOT NULL))
        """,
    }
    for table in owned_tables:
        count = bind.scalar(text(checks["orphan relationship-owned row"].format(table=f"agata.{table}")))
        if count:
            raise RuntimeError(f"agata0003 backfill validation failed: {table} has {count} orphan rows")
    for label, query in list(checks.items())[1:]:
        count = bind.scalar(text(query))
        if count:
            raise RuntimeError(f"agata0003 backfill validation failed: {label}: {count}")


def _column_exists(table: str, column: str) -> bool:
    return any(
        item["name"] == column
        for item in sa.inspect(op.get_bind()).get_columns(table, schema="agata")
    )


def _constraint_exists(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    constraints = (
        inspector.get_unique_constraints(table, schema="agata")
        + inspector.get_foreign_keys(table, schema="agata")
        + inspector.get_check_constraints(table, schema="agata")
    )
    return any(item.get("name") == name for item in constraints)


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _column_exists(table, column.name):
        op.add_column(table, column, schema="agata")


def _add_unique_if_missing(table: str, name: str, columns: list[str]) -> None:
    if not _constraint_exists(table, name):
        op.create_unique_constraint(name, table, columns, schema="agata")


def _add_fk_if_missing(table: str, name: str, referent: str, local: list[str], remote: list[str]) -> None:
    if not _constraint_exists(table, name):
        op.create_foreign_key(
            name, table, referent, local, remote, source_schema="agata", referent_schema="agata"
        )


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("relationships", schema="agata"):
        op.execute("""
        CREATE TABLE agata.relationships (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES agata.tenants(id),
            user_id uuid NOT NULL,
            character_id uuid NOT NULL,
            scope public.scope_enum NOT NULL,
            scenario_id uuid NULL,
            scenario_key uuid GENERATED ALWAYS AS
                (coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)) STORED,
            status varchar(16) NOT NULL DEFAULT 'ACTIVE',
            started_at timestamptz NOT NULL,
            last_interaction_at timestamptz NULL,
            ended_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_relationship_context
                UNIQUE (tenant_id,user_id,character_id,scope,scenario_key),
            CONSTRAINT uq_relationship_owner_scope
                UNIQUE (tenant_id,id,user_id,character_id,scope,scenario_key),
            CONSTRAINT fk_relationship_user
                FOREIGN KEY (tenant_id,user_id) REFERENCES agata.users(tenant_id,id),
            CONSTRAINT fk_relationship_character
                FOREIGN KEY (tenant_id,character_id) REFERENCES agata.characters(tenant_id,id),
            CONSTRAINT fk_relationship_scenario_owner
                FOREIGN KEY (tenant_id,scenario_id,user_id,character_id)
                REFERENCES agata.roleplay_scenarios(tenant_id,id,user_id,character_id),
            CONSTRAINT ck_relationship_scope CHECK
                ((scope='real_world' AND scenario_id IS NULL) OR
                 (scope='roleplay' AND scenario_id IS NOT NULL)),
            CONSTRAINT ck_relationship_status CHECK (status IN ('ACTIVE','PAUSED','ENDED')),
            CONSTRAINT ck_relationship_ended_at CHECK
                ((status='ENDED' AND ended_at IS NOT NULL) OR
                 (status<>'ENDED' AND ended_at IS NULL)),
            CONSTRAINT ck_relationship_interaction_interval
                CHECK (last_interaction_at IS NULL OR last_interaction_at >= started_at)
        )
        """)

    owned_tables = _owned_tables()
    for table in owned_tables:
        _add_column_if_missing(table, sa.Column("relationship_id", sa.UUID(), nullable=True))
    _add_column_if_missing(
        "suppression_rules",
        sa.Column(
            "scenario_key",
            sa.UUID(),
            sa.Computed(
                "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)",
                persisted=True,
            ),
        ),
    )
    if "derived_memory_views" in owned_tables:
        _add_column_if_missing(
            "derived_memory_views",
            sa.Column(
                "scenario_key",
                sa.UUID(),
                sa.Computed(
                    "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)",
                    persisted=True,
                ),
            ),
        )
    _add_column_if_missing(
        "retrieval_candidates", sa.Column("relationship_id", sa.UUID(), nullable=True)
    )

    # Every existing owner tuple from each relationship-bearing aggregate is included.
    legacy_view_owners = (
        "UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at "
        "FROM agata.derived_memory_views"
        if "derived_memory_views" in owned_tables
        else ""
    )
    op.execute(f"""
        WITH owners AS (
            SELECT tenant_id,user_id,character_id,scope,scenario_id,
                   started_at AS conversation_started, created_at AS seen_at
            FROM agata.conversations
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.memory_control_state
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.memories
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.suppression_rules
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.retrieval_runs
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.response_runs
            {legacy_view_owners}
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.shared_history_episodes
            UNION ALL SELECT tenant_id,user_id,character_id,scope,scenario_id,NULL,created_at
                FROM agata.shadow_memory_candidates
        ), grouped AS (
            SELECT tenant_id,user_id,character_id,scope,scenario_id,
                   min(conversation_started) AS earliest_conversation,
                   min(seen_at) AS earliest_seen
            FROM owners GROUP BY tenant_id,user_id,character_id,scope,scenario_id
        ), interactions AS (
            SELECT c.tenant_id,c.user_id,c.character_id,c.scope,c.scenario_id,
                   max(m.occurred_at) AS last_interaction
            FROM agata.messages m
            JOIN agata.conversations c ON c.tenant_id=m.tenant_id AND c.id=m.conversation_id
            WHERE m.role='user' AND m.deleted_at IS NULL
            GROUP BY c.tenant_id,c.user_id,c.character_id,c.scope,c.scenario_id
        )
        INSERT INTO agata.relationships
            (tenant_id,user_id,character_id,scope,scenario_id,status,started_at,last_interaction_at)
        SELECT g.tenant_id,g.user_id,g.character_id,g.scope,g.scenario_id,'ACTIVE',
               coalesce(g.earliest_conversation,g.earliest_seen),
               greatest(i.last_interaction,coalesce(g.earliest_conversation,g.earliest_seen))
        FROM grouped g LEFT JOIN interactions i USING
            (tenant_id,user_id,character_id,scope,scenario_id)
        ON CONFLICT (tenant_id,user_id,character_id,scope,scenario_key) DO NOTHING
    """)

    for table in owned_tables:
        op.execute(f"""
            UPDATE agata.{table} t SET relationship_id=r.id
            FROM agata.relationships r
            WHERE r.tenant_id=t.tenant_id AND r.user_id=t.user_id
              AND r.character_id=t.character_id AND r.scope=t.scope
              AND r.scenario_key=t.scenario_key
        """)
    op.execute("""
        UPDATE agata.retrieval_candidates c SET relationship_id=r.relationship_id
        FROM agata.retrieval_runs r
        WHERE r.tenant_id=c.tenant_id AND r.id=c.run_id
    """)

    _validate_backfill(owned_tables)

    # Unique target keys required by composite owner constraints.
    _add_unique_if_missing(
        "conversations", "uq_conversation_relationship_owner_scope",
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"]
    )
    _add_unique_if_missing(
        "memories", "uq_memory_relationship_owner_scope",
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"]
    )
    _add_unique_if_missing(
        "retrieval_runs", "uq_retrieval_run_relationship", ["tenant_id","id","relationship_id"]
    )
    _add_unique_if_missing(
        "response_runs", "uq_response_run_relationship_owner_scope",
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"]
    )
    _add_unique_if_missing(
        "memory_control_state", "uq_memory_control_relationship", ["tenant_id","relationship_id"]
    )
    _add_unique_if_missing(
        "memory_control_state", "uq_control_relationship_owner_scope_key",
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"]
    )
    if "derived_memory_views" in owned_tables:
        _add_unique_if_missing(
            "derived_memory_views", "uq_derived_view_relationship",
            ["tenant_id","relationship_id","view_type"]
        )

    for table in owned_tables + ("retrieval_candidates",):
        if any(
            item["name"] == "relationship_id" and item["nullable"]
            for item in sa.inspect(op.get_bind()).get_columns(table, schema="agata")
        ):
            op.alter_column(table, "relationship_id", schema="agata", nullable=False)

    owner_fk_names = {
        "conversations": "fk_conversation_relationship_owner",
        "memory_control_state": "fk_memory_control_relationship_owner",
        "memories": "fk_memory_relationship_owner",
        "suppression_rules": "fk_suppression_relationship_owner",
        "retrieval_runs": "fk_retrieval_run_relationship_owner",
        "response_runs": "fk_response_run_relationship_owner",
        "derived_memory_views": "fk_derived_view_relationship_owner",
        "shared_history_episodes": "fk_shared_history_relationship_owner",
        "shadow_memory_candidates": "fk_shadow_candidate_relationship_owner",
    }
    for table, constraint_name in owner_fk_names.items():
        if table not in owned_tables:
            continue
        _add_fk_if_missing(
            table, constraint_name, "relationships",
            ["tenant_id","relationship_id","user_id","character_id","scope","scenario_key"],
            ["tenant_id","id","user_id","character_id","scope","scenario_key"],
        )

    _add_fk_if_missing(
        "retrieval_candidates", "fk_retrieval_candidate_run_relationship", "retrieval_runs",
        ["tenant_id","run_id","relationship_id"],
        ["tenant_id","id","relationship_id"],
    )
    _add_fk_if_missing(
        "retrieval_candidates", "fk_retrieval_candidate_memory_relationship", "memories",
        ["tenant_id","memory_id","relationship_id","user_id","character_id","scope","scenario_key"],
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"],
    )
    _add_fk_if_missing(
        "response_runs", "fk_response_run_conversation_relationship", "conversations",
        ["tenant_id","conversation_id","relationship_id","user_id","character_id","scope","scenario_key"],
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"],
    )
    _add_fk_if_missing(
        "response_runs", "fk_response_run_retrieval_relationship", "retrieval_runs",
        ["tenant_id","retrieval_run_id","relationship_id"],
        ["tenant_id","id","relationship_id"],
    )

    for name, table in (
        ("memory_relationship_idx", "memories"),
        ("retrieval_relationship_time_idx", "retrieval_runs"),
        ("shared_relationship_active_idx", "shared_history_episodes"),
    ):
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON agata.{table} (tenant_id,relationship_id)")

    # Fail closed on any data mismatch before enabling mandatory ownership.
    _validate_backfill(owned_tables)
    op.execute("ALTER TABLE agata.relationships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agata.relationships FORCE ROW LEVEL SECURITY")
    op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='agata' AND tablename='relationships' AND policyname='relationships_tenant_isolation') THEN CREATE POLICY relationships_tenant_isolation ON agata.relationships "
               "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
               "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid); END IF; END $$")
    runtime_role = os.environ.get("AGATA_RUNTIME_DB_ROLE")
    if runtime_role:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role):
            raise RuntimeError("AGATA_RUNTIME_DB_ROLE must be a simple PostgreSQL role name")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON agata.relationships TO "{runtime_role}"')


def downgrade() -> None:
    raise RuntimeError("agata0003 is irreversible: relationship ownership is authoritative")
