"""Durable inference leases and approved V1 self-attestation.

Revision ID: agata0007
Revises: agata0006
"""
from __future__ import annotations

from alembic import op

revision = "agata0007"
down_revision = "agata0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agata.age_assurances ADD COLUMN IF NOT EXISTS asserted_at timestamptz")
    op.execute("ALTER TABLE agata.outbox_jobs ADD COLUMN IF NOT EXISTS claim_token varchar(64)")
    op.execute("ALTER TABLE agata.outbox_jobs DROP CONSTRAINT IF EXISTS ck_outbox_job_type_allowlist")
    op.create_check_constraint(
        "ck_outbox_job_type_allowlist",
        "outbox_jobs",
        "job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','memory_graph_sync','response_retry','inference')",
        schema="agata",
    )
    op.execute(
        "ALTER TABLE agata.response_route_policies "
        "DROP CONSTRAINT IF EXISTS ck_response_route_provider"
    )
    op.execute(
        "ALTER TABLE agata.response_route_policies "
        "ADD CONSTRAINT ck_response_route_provider "
        "CHECK (provider IN ('local_fake','external_disabled','deepseek','anymodel','mac_worker','disabled'))"
    )
    op.execute(
        "UPDATE agata.regional_policy_profiles "
        "SET rules = jsonb_set(rules, ARRAY['self_attestation_allowed'], 'true'::jsonb, true) "
        "WHERE status='active' AND jurisdiction='GLOBAL' AND market='GPOINT'"
    )


def downgrade() -> None:
    raise RuntimeError(
        "agata0007 is forward-only: downgrade could revoke approved age policy or invalidate durable inference leases"
    )
