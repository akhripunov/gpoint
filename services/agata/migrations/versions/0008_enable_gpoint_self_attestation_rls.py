"""Enable approved GPoint self-attestation under tenant row-level security.

Revision ID: agata0008
Revises: agata0007
"""
from __future__ import annotations

import os
from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "agata0008"
down_revision = "agata0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tenant_value = os.environ.get("AGATA_TENANT_ID")
    if not tenant_value:
        raise RuntimeError("AGATA_TENANT_ID is required to enable GPoint self-attestation")
    tenant_id = str(UUID(tenant_value))
    bind = op.get_bind()
    bind.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )
    result = bind.execute(
        text(
            "UPDATE agata.regional_policy_profiles "
            "SET rules = jsonb_set(rules, ARRAY['self_attestation_allowed'], 'true'::jsonb, true) "
            "WHERE tenant_id = :tenant_id AND jurisdiction = 'GLOBAL' AND market = 'GPOINT' "
            "AND status = 'active' AND effective_from <= now() "
            "AND (effective_to IS NULL OR effective_to > now())"
        ),
        {"tenant_id": tenant_id},
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "expected exactly one active GLOBAL/GPOINT policy for self-attestation"
        )


def downgrade() -> None:
    raise RuntimeError("agata0008 is forward-only: downgrade would revoke approved age policy")
