#!/usr/bin/env python3
"""Return an aggregate-only queue snapshot from a verified live AGATA API image.

The release worker pipes this file into the already-running API container. The
query is tenant-bound and read-only; it returns no job ids, contents, or PII.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import timezone
from uuid import UUID

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def utc_iso(value) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


async def read_snapshot() -> dict:
    from app.config import get_settings
    from app.db.base import _engine, set_tenant_context
    from sqlalchemy import text

    settings = get_settings()
    lane = os.environ.get("SNAPSHOT_LANE", "")
    expected_source = {"GPOINT": "gpoint", "GSALES": "gsales"}.get(lane)
    source_system = getattr(settings, "source_system", "")
    tenant_id = str(getattr(settings, "tenant_id", ""))
    expected_tenant = os.environ.get("SNAPSHOT_EXPECTED_TENANT_ID", "")
    if (expected_source is None or source_system != expected_source
            or not UUID_RE.fullmatch(tenant_id) or tenant_id != expected_tenant):
        raise RuntimeError("runtime_lane_identity_mismatch")
    try:
        tenant_uuid = UUID(tenant_id)
    except ValueError as exc:
        raise RuntimeError("runtime_lane_identity_mismatch") from exc
    if tenant_uuid.int == 0 or str(tenant_uuid) != tenant_id:
        raise RuntimeError("runtime_lane_identity_mismatch")

    release_sha = os.environ.get("SNAPSHOT_RELEASE_SHA", "")
    api_sha = os.environ.get("SNAPSHOT_API_BUILD_SHA", "")
    core_sha = os.environ.get("SNAPSHOT_CORE_BUILD_SHA", "")
    api_image_id = os.environ.get("SNAPSHOT_API_IMAGE_ID", "")
    core_image_id = os.environ.get("SNAPSHOT_CORE_IMAGE_ID", "")
    if (not all(SHA_RE.fullmatch(value) for value in (release_sha, api_sha, core_sha))
            or release_sha != api_sha or release_sha != core_sha
            or not IMAGE_RE.fullmatch(api_image_id) or not IMAGE_RE.fullmatch(core_image_id)):
        raise RuntimeError("runtime_release_identity_invalid")

    if lane == "GSALES" and getattr(settings, "sales_drafts_enabled", False):
        job_type_clause = "jobs.job_type IN ('inference', 'sales_draft_inference')"
    else:
        job_type_clause = "jobs.job_type = 'inference'"

    async with _engine().connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await set_tenant_context(connection, tenant_id)
            result = await connection.execute(text(f"""
                SELECT transaction_timestamp() AT TIME ZONE 'UTC' AS snapshot_at,
                       current_database() AS database_name,
                       current_setting('transaction_read_only') AS transaction_read_only,
                       current_user = session_user AS same_login,
                       role.rolsuper AS role_superuser,
                       role.rolbypassrls AS role_bypassrls,
                       (SELECT count(*) FROM agata.tenants
                         WHERE id = :tenant_id AND status = 'active') AS visible_tenant_rows,
                       count(*) FILTER (WHERE jobs.state = 'pending') AS pending_inference_jobs,
                       count(*) FILTER (WHERE jobs.state = 'running') AS running_inference_jobs
                  FROM pg_roles AS role
                  LEFT JOIN agata.outbox_jobs AS jobs
                    ON jobs.tenant_id = :tenant_id AND {job_type_clause}
                 WHERE role.rolname = current_user
                 GROUP BY role.rolsuper, role.rolbypassrls
            """), {"tenant_id": tenant_id})
            row = result.mappings().one()

    if (row["transaction_read_only"] not in ("on", "true") or row["same_login"] is not True
            or row["role_superuser"] is not False or row["role_bypassrls"] is not False
            or int(row["visible_tenant_rows"]) != 1 or not row["database_name"]):
        raise RuntimeError("queue_snapshot_trust_boundary_invalid")

    source_id = (
        f"agata-live-queue-snapshot-v1:{lane.lower()}:release={release_sha}"
        f":api={api_image_id}:core={core_image_id}:tenant={tenant_id}"
    )
    return {
        "snapshot_version": "agata_live_queue_snapshot_v1",
        "source_id": source_id,
        "snapshot_at": utc_iso(row["snapshot_at"]),
        "lane": lane,
        "source_system": source_system,
        "database_name": row["database_name"],
        "release_sha": release_sha,
        "api_build_sha": api_sha,
        "core_build_sha": core_sha,
        "api_image_id": api_image_id,
        "core_image_id": core_image_id,
        "tenant_id": tenant_id,
        "tenant_identity_verified": True,
        "transaction_read_only": True,
        "same_login": True,
        "role_superuser": False,
        "role_bypassrls": False,
        "visible_tenant_rows": 1,
        "pending_inference_jobs": int(row["pending_inference_jobs"] or 0),
        "running_inference_jobs": int(row["running_inference_jobs"] or 0),
        "effective_inference_ttl_seconds": int(settings.inference_job_ttl_seconds),
    }


if __name__ == "__main__":
    try:
        result = asyncio.run(read_snapshot())
    except RuntimeError as exc:
        error = str(exc)
        safe_errors = {
            "runtime_lane_identity_mismatch",
            "runtime_release_identity_invalid",
            "queue_snapshot_trust_boundary_invalid",
        }
        result = {"preflight_error": error if error in safe_errors else "readonly_snapshot_blocked"}
    except Exception:
        result = {"preflight_error": "readonly_snapshot_blocked"}
    import json
    print(json.dumps(result, separators=(",", ":")))
