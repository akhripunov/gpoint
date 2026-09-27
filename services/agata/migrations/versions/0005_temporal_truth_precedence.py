"""Add database guards/indexes for temporal truth and revision ownership.

Revision ID: agata0005
Revises: agata0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "agata0005"
down_revision = "agata0004"
branch_labels = None
depends_on = None


def _constraint_exists(bind, name: str) -> bool:
    return any(
        row.get("name") == name
        for row in (
            sa.inspect(bind).get_check_constraints("memories", schema="agata")
            + sa.inspect(bind).get_foreign_keys("memories", schema="agata")
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    self_rows = bind.scalar(sa.text(
        "SELECT count(*) FROM agata.memories WHERE supersedes_id = id"
    ))
    if self_rows:
        raise RuntimeError("agata0005 refuses existing self-supersession rows")
    cross_relationship_edges = bind.scalar(sa.text("""
        SELECT count(*) FROM agata.memories successor
        JOIN agata.memories predecessor
          ON predecessor.tenant_id=successor.tenant_id AND predecessor.id=successor.supersedes_id
        WHERE successor.relationship_id <> predecessor.relationship_id
           OR successor.user_id <> predecessor.user_id
           OR successor.character_id <> predecessor.character_id
           OR successor.scope <> predecessor.scope
           OR successor.scenario_key <> predecessor.scenario_key
           OR successor.slot_key IS DISTINCT FROM predecessor.slot_key
    """))
    if cross_relationship_edges:
        raise RuntimeError("agata0005 refuses supersession edges outside the same relationship slot")
    forked_chains = bind.scalar(sa.text("""
        SELECT count(*) FROM (
            SELECT tenant_id, relationship_id, supersedes_id
            FROM agata.memories WHERE supersedes_id IS NOT NULL
            GROUP BY tenant_id, relationship_id, supersedes_id HAVING count(*) > 1
        ) branches
    """))
    if forked_chains:
        raise RuntimeError("agata0005 refuses branched memory revision chains")
    invalid_versions = bind.scalar(sa.text("SELECT count(*) FROM agata.memories WHERE version < 1"))
    if invalid_versions:
        raise RuntimeError("agata0005 refuses non-positive memory revision versions")
    duplicate_slots = bind.scalar(sa.text("""
        SELECT count(*) FROM (
            SELECT tenant_id,relationship_id,user_id,character_id,scope,scenario_key,slot_key
            FROM agata.memories
            WHERE lifecycle_state='active' AND kind IN ('semantic_fact','episodic') AND slot_key IS NOT NULL
            GROUP BY tenant_id,relationship_id,user_id,character_id,scope,scenario_key,slot_key
            HAVING count(*) > 1
        ) conflicts
    """))
    if duplicate_slots:
        raise RuntimeError("agata0005 refuses ambiguous duplicate active memory slots")
    chain_count = bind.scalar(sa.text("""
        WITH RECURSIVE chain(tenant_id, id, version, path) AS (
            SELECT tenant_id, id, 1, ARRAY[id] FROM agata.memories WHERE supersedes_id IS NULL
            UNION ALL
            SELECT child.tenant_id, child.id, chain.version + 1, chain.path || child.id
            FROM agata.memories child
            JOIN chain ON child.tenant_id=chain.tenant_id AND child.supersedes_id=chain.id
            WHERE NOT child.id = ANY(chain.path)
        )
        SELECT count(DISTINCT id) FROM chain
    """))
    memory_count = bind.scalar(sa.text("SELECT count(*) FROM agata.memories"))
    if chain_count != memory_count:
        raise RuntimeError("agata0005 refuses cyclic or rootless memory revision chains")
    bind.execute(sa.text("""
        WITH RECURSIVE chain(tenant_id, id, version, path) AS (
            SELECT tenant_id, id, 1, ARRAY[id] FROM agata.memories WHERE supersedes_id IS NULL
            UNION ALL
            SELECT child.tenant_id, child.id, chain.version + 1, chain.path || child.id
            FROM agata.memories child
            JOIN chain ON child.tenant_id=chain.tenant_id AND child.supersedes_id=chain.id
            WHERE NOT child.id = ANY(chain.path)
        )
        UPDATE agata.memories target SET version=chain.version
        FROM chain WHERE target.tenant_id=chain.tenant_id AND target.id=chain.id
    """))
    bind.execute(sa.text("""
        UPDATE agata.response_memory_refs refs SET memory_version=memory.version
        FROM agata.memories memory
        WHERE refs.tenant_id=memory.tenant_id AND refs.memory_id=memory.id
          AND refs.memory_version IS DISTINCT FROM memory.version
    """))
    bind.execute(sa.text("""
        UPDATE agata.retrieval_candidates refs SET memory_version=memory.version
        FROM agata.memories memory
        WHERE refs.tenant_id=memory.tenant_id AND refs.memory_id=memory.id
          AND refs.memory_version IS DISTINCT FROM memory.version
    """))
    if not _constraint_exists(bind, "ck_memory_not_self_superseding"):
        op.create_check_constraint(
            "ck_memory_not_self_superseding", "memories",
            "supersedes_id IS NULL OR supersedes_id <> id", schema="agata",
        )
    if not _constraint_exists(bind, "ck_memory_version_positive"):
        op.create_check_constraint("ck_memory_version_positive", "memories", "version >= 1", schema="agata")
    op.execute("DROP INDEX IF EXISTS agata.memories_one_successor_idx")
    op.create_index(
        "memories_one_successor_idx", "memories", ["tenant_id", "relationship_id", "supersedes_id"],
        unique=True, schema="agata", postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )
    op.execute("ALTER TABLE agata.memories DROP CONSTRAINT IF EXISTS fk_memory_supersedes_owner")
    op.create_foreign_key(
        "fk_memory_supersedes_owner", "memories", "memories",
        ["tenant_id", "supersedes_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
        ["tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
        source_schema="agata", referent_schema="agata",
    )
    op.execute("DROP INDEX IF EXISTS agata.memories_one_active_slot_idx")
    op.create_index(
        "memories_one_active_slot_idx", "memories",
        ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_id", "slot_key"],
        unique=True, schema="agata", postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("lifecycle_state = 'active' AND kind IN ('semantic_fact','episodic') AND slot_key IS NOT NULL"),
    )
    op.execute("DROP INDEX IF EXISTS agata.memories_truth_resolution_idx")
    op.create_index(
        "memories_truth_resolution_idx", "memories",
        ["tenant_id", "relationship_id", "slot_key", "lifecycle_state", "valid_from", "valid_until"],
        schema="agata",
        postgresql_where=sa.text("deleted_at IS NULL AND evidence_state <> 'disputed'"),
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agata.memories_one_successor_idx")
    op.execute("DROP INDEX IF EXISTS agata.memories_truth_resolution_idx")
    op.execute("DROP INDEX IF EXISTS agata.memories_one_active_slot_idx")
    op.create_index(
        "memories_one_active_slot_idx", "memories",
        ["tenant_id", "user_id", "character_id", "scope", "scenario_id", "slot_key"],
        unique=True, schema="agata", postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("lifecycle_state = 'active' AND kind = 'semantic_fact' AND slot_key IS NOT NULL"),
    )
    op.execute("ALTER TABLE agata.memories DROP CONSTRAINT IF EXISTS fk_memory_supersedes_owner")
    op.create_foreign_key(
        "fk_memory_supersedes_owner", "memories", "memories",
        ["tenant_id", "supersedes_id", "user_id", "character_id", "scope", "scenario_key"],
        ["tenant_id", "id", "user_id", "character_id", "scope", "scenario_key"],
        source_schema="agata", referent_schema="agata",
    )
    op.execute("ALTER TABLE agata.memories DROP CONSTRAINT IF EXISTS ck_memory_not_self_superseding")
    op.execute("ALTER TABLE agata.memories DROP CONSTRAINT IF EXISTS ck_memory_version_positive")
