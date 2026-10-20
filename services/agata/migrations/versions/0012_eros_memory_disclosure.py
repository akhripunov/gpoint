"""Bump the AI disclosure so existing users see the Eros memory notice.

Revision ID: agata0012
Revises: agata0011
"""

from __future__ import annotations

import json
import os
from uuid import UUID

from alembic import op
from sqlalchemy import text

revision = "agata0012"
down_revision = "agata0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    privileged = bind.scalar(text(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ))
    if privileged:
        tenants = list(bind.scalars(text("SELECT id FROM agata.tenants ORDER BY id")))
    else:
        inventory = os.environ.get("AGATA_MIGRATION_TENANT_IDS", "")
        if not inventory.strip():
            raise RuntimeError(
                "agata0012 requires AGATA_MIGRATION_TENANT_IDS for a non-bypass migrator"
            )
        tenants = sorted({UUID(value.strip()) for value in inventory.split(",")})
    bind.execute(text("LOCK TABLE agata.regional_policy_profiles IN SHARE ROW EXCLUSIVE MODE"))

    prior_tenant = bind.scalar(text("SELECT current_setting('app.tenant_id', true)"))
    try:
        for tenant in tenants:
            bind.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant)},
            )
            if not bind.scalar(
                text("SELECT EXISTS(SELECT 1 FROM agata.tenants WHERE id=:tenant)"),
                {"tenant": tenant},
            ):
                raise RuntimeError("agata0012 tenant inventory contains an unavailable tenant")
            profiles = list(bind.execute(text("""
                SELECT id, jurisdiction, market, version, effective_from, effective_to, rules
                FROM agata.regional_policy_profiles
                WHERE tenant_id=:tenant AND status='active'
                  AND rules->>'ai_disclosure_required'='true'
                ORDER BY jurisdiction, market, version
                FOR UPDATE
            """), {"tenant": tenant}).mappings())
            for profile in profiles:
                next_version = bind.scalar(text("""
                    SELECT coalesce(max(version), 0) + 1
                    FROM agata.regional_policy_profiles
                    WHERE tenant_id=:tenant AND jurisdiction=:jurisdiction AND market=:market
                """), {
                    "tenant": tenant,
                    "jurisdiction": profile["jurisdiction"],
                    "market": profile["market"],
                })
                bind.execute(text("""
                    UPDATE agata.regional_policy_profiles
                    SET status='retired'
                    WHERE tenant_id=:tenant AND id=:id
                """), {"tenant": tenant, "id": profile["id"]})
                bind.execute(text("""
                    INSERT INTO agata.regional_policy_profiles
                        (tenant_id, jurisdiction, market, version, status, effective_from, effective_to, rules)
                    VALUES
                        (:tenant, :jurisdiction, :market, :version, 'active', :effective_from, :effective_to, cast(:rules AS jsonb))
                """), {
                    "tenant": tenant,
                    "jurisdiction": profile["jurisdiction"],
                    "market": profile["market"],
                    "version": next_version,
                    "effective_from": profile["effective_from"],
                    "effective_to": profile["effective_to"],
                    "rules": json.dumps(profile["rules"]),
                })
    finally:
        bind.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": prior_tenant or ""},
        )


def downgrade() -> None:
    raise RuntimeError("agata0012 is forward-only: downgrade would revalidate stale disclosure receipts")
