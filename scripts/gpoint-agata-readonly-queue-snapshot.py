#!/usr/bin/env python3
"""Emit an aggregate-only snapshot from the running GPOINT AGATA DB runtime.

Run by piping this source to ``python -`` inside the verified AGATA API
container. The query transaction is read-only and tenant-bound; no job payloads
or identifiers are selected.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import timezone

from sqlalchemy import text

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXPECTED_GPOINT_TENANT_ID = "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
SAFE_ERRORS = {
    "runtime_gpoint_tenant_not_registered",
    "runtime_gpoint_tenant_identity_mismatch",
    "runtime_release_identity_invalid",
    "gpoint_api_agata_release_mismatch",
    "runtime_image_identity_invalid",
    "runtime_database_is_not_gpoint",
    "queue_snapshot_transaction_not_read_only_same_login",
    "queue_snapshot_database_role_is_privileged",
    "queue_snapshot_gpoint_tenant_not_visible",
}


def utc_iso(value) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


async def read_snapshot() -> dict:
    # Imports are deferred so controller/unit tests can load this module without
    # importing the AGATA application or opening any database connection.
    from app.config import get_settings
    from app.db.base import _engine, set_tenant_context

    settings = get_settings()
    source_system = getattr(settings, "source_system", "gpoint")
    # Bind the snapshot to the tenant already selected by this live runtime.
    # Never replace it with a candidate/test UUID or override the tenant GUC.
    tenant_id = str(settings.tenant_id)
    if source_system != "gpoint" or tenant_id != EXPECTED_GPOINT_TENANT_ID or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tenant_id
    ):
        raise RuntimeError("runtime_gpoint_tenant_identity_mismatch")

    release_sha = os.environ.get("SNAPSHOT_RELEASE_SHA", "")
    api_sha = os.environ.get("SNAPSHOT_API_BUILD_SHA", "")
    agata_sha = os.environ.get("SNAPSHOT_AGATA_BUILD_SHA", "")
    api_image_id = os.environ.get("SNAPSHOT_API_IMAGE_ID", "")
    agata_image_id = os.environ.get("SNAPSHOT_AGATA_IMAGE_ID", "")
    if not all(SHA_RE.fullmatch(value) for value in (release_sha, api_sha, agata_sha)):
        raise RuntimeError("runtime_release_identity_invalid")
    if release_sha != api_sha or release_sha != agata_sha:
        raise RuntimeError("gpoint_api_agata_release_mismatch")
    if not IMAGE_RE.fullmatch(api_image_id) or not IMAGE_RE.fullmatch(agata_image_id):
        raise RuntimeError("runtime_image_identity_invalid")

    async with _engine().connect() as connection:
        await connection.begin()
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await set_tenant_context(connection, tenant_id)
        result = await connection.execute(
            text(
                """
                SELECT transaction_timestamp() AT TIME ZONE 'UTC' AS snapshot_at,
                       current_database() AS database_name,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       current_user = session_user AS same_login,
                       role.rolsuper AS role_superuser,
                       role.rolbypassrls AS role_bypassrls,
                       (SELECT count(*) FROM agata.tenants WHERE id = :tenant_id AND status = 'active') AS visible_tenant_rows,
                       (SELECT count(*) FROM agata.regional_policy_profiles
                         WHERE tenant_id = :tenant_id AND market = 'GPOINT' AND status = 'active') AS active_gpoint_profiles,
                       count(*) FILTER (WHERE jobs.state = 'pending') AS pending_inference_jobs,
                       count(*) FILTER (WHERE jobs.state = 'running') AS running_inference_jobs
                  FROM pg_roles AS role
                  LEFT JOIN agata.outbox_jobs AS jobs
                    ON jobs.tenant_id = :tenant_id AND jobs.job_type = 'inference'
                 WHERE role.rolname = current_user
                 GROUP BY role.rolsuper, role.rolbypassrls
                """
            ),
            {"tenant_id": tenant_id},
        )
        row = result.mappings().one()
        await connection.rollback()

    if row["database_name"] != "gpoint":
        raise RuntimeError("runtime_database_is_not_gpoint")
    if row["transaction_read_only"] not in ("on", "true") or not row["same_login"]:
        raise RuntimeError("queue_snapshot_transaction_not_read_only_same_login")
    if row["role_superuser"] or row["role_bypassrls"]:
        raise RuntimeError("queue_snapshot_database_role_is_privileged")
    if int(row["visible_tenant_rows"]) != 1 or int(row["active_gpoint_profiles"]) < 1:
        raise RuntimeError("runtime_gpoint_tenant_not_registered")

    return {
        "snapshot_at": utc_iso(row["snapshot_at"]),
        "source_id": os.environ["SNAPSHOT_SOURCE_ID"],
        "source_system": source_system,
        "database_name": row["database_name"],
        "release_sha": release_sha,
        "api_build_sha": api_sha,
        "agata_build_sha": agata_sha,
        "api_image_id": api_image_id,
        "agata_image_id": agata_image_id,
        "tenant_identity_verified": True,
        "tenant_id": tenant_id,
        "active_gpoint_profiles": int(row["active_gpoint_profiles"]),
        "transaction_read_only": True,
        "same_login": True,
        "role_superuser": False,
        "role_bypassrls": False,
        "visible_tenant_rows": 1,
        "pending_inference_jobs": int(row["pending_inference_jobs"]),
        "running_inference_jobs": int(row["running_inference_jobs"]),
        "effective_inference_ttl_seconds": int(settings.inference_job_ttl_seconds),
    }


if __name__ == "__main__":
    try:
        result = asyncio.run(read_snapshot())
    except RuntimeError as exc:
        # Only fixed, non-sensitive diagnostic codes cross the SSH boundary.
        error = str(exc)
        result = {"preflight_error": error if error in SAFE_ERRORS else "readonly_snapshot_blocked"}
    except Exception:
        # Database/driver exceptions can embed connection data; keep them local.
        result = {"preflight_error": "readonly_snapshot_blocked"}
    print(json.dumps(result, separators=(",", ":")))
