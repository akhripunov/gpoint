"""Persist versioned GPoint chat onboarding and the explicit memory choice.

Revision ID: agata0009
Revises: agata0008
"""
from __future__ import annotations

import os
from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = "agata0009"
down_revision = "agata0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tenant_value = os.environ.get("AGATA_TENANT_ID")
    if not tenant_value:
        raise RuntimeError("AGATA_TENANT_ID is required for the chat onboarding migration")
    tenant_id = str(UUID(tenant_value))
    bind = op.get_bind()
    bind.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id})
    columns = {column["name"] for column in inspect(bind).get_columns("ai_disclosure_receipts", schema="agata")}
    if "memory_opt_in" not in columns:
        op.add_column(
            "ai_disclosure_receipts",
            sa.Column("memory_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
            schema="agata",
        )
    if "memory_opt_in_at" not in columns:
        op.add_column(
            "ai_disclosure_receipts",
            sa.Column("memory_opt_in_at", sa.DateTime(timezone=True), nullable=True),
            schema="agata",
        )
    # Preserve only affirmative memory choices proven by the separate active
    # long_term_memory consent. Chat history by itself never grants memory.
    bind.execute(text("""
        WITH latest_receipts AS (
            SELECT DISTINCT ON (r.tenant_id, r.user_id)
                r.id, r.tenant_id, r.user_id
            FROM agata.ai_disclosure_receipts AS r
            WHERE r.tenant_id = :tenant_id
            ORDER BY r.tenant_id, r.user_id, r.displayed_at DESC, r.id DESC
        ), active_memory AS (
            SELECT c.tenant_id, c.user_id, max(c.granted_at) AS granted_at
            FROM agata.consents AS c
            WHERE c.tenant_id = :tenant_id AND c.purpose = 'long_term_memory'
              AND c.revoked_at IS NULL
            GROUP BY c.tenant_id, c.user_id
        )
        UPDATE agata.ai_disclosure_receipts AS r
        SET memory_opt_in = true, memory_opt_in_at = m.granted_at
        FROM latest_receipts AS latest
        JOIN active_memory AS m USING (tenant_id, user_id)
        WHERE r.id = latest.id
    """), {"tenant_id": tenant_id})


def downgrade() -> None:
    raise RuntimeError("agata0009 is forward-only: downgrade would discard explicit consent state")
