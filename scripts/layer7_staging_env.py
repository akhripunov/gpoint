"""Validate an isolated Layer7 GPoint environment bundle without revealing values."""

from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import urlsplit


KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
INTERNAL_HOST = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
REQUIRED = {
    "DATABASE_URL",
    "REDIS_URL",
    "NODE_ENV",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GPOINT_TENANT_ID",
    "GPOINT_CHAT_POLICY_TENANT_ID",
}
STAGING_INGEST_URL = "https://layer7-staging.invalid/api/internal/telegram/update"
STAGING_APP_URL = "https://layer7-staging.invalid"


def parse_staging_env(payload: str) -> tuple[bytes, str]:
    if not isinstance(payload, str) or not payload or len(payload.encode("utf-8")) > 48_000:
        raise ValueError("staging environment bundle is empty or too large")
    values: dict[str, str] = {}
    for line in payload.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (not separator or not KEY.fullmatch(key) or key in values
                or "\x00" in value or "\r" in value or "\n" in value):
            raise ValueError("staging environment bundle has invalid syntax")
        values[key] = value
    if not REQUIRED.issubset(values) or values.get("NODE_ENV") != "production":
        raise ValueError("staging environment bundle is missing required runtime settings")
    if values.get("EROS_ENABLED") != "true" or values.get("EROS_DATA_MODE") != "gpoint-owned":
        raise ValueError("staging environment bundle does not select the isolated Eros route")
    if any(not values[name] for name in REQUIRED):
        raise ValueError("staging environment bundle contains an empty required setting")
    try:
        tenants = [uuid.UUID(values[name]) for name in (
            "GPOINT_TENANT_ID", "GPOINT_CHAT_POLICY_TENANT_ID")
        ]
    except (ValueError, TypeError, AttributeError):
        raise ValueError("staging tenant identity is invalid") from None
    if (any(tenant.int == 0 or str(tenant) != values[name]
            for tenant, name in zip(tenants, ("GPOINT_TENANT_ID", "GPOINT_CHAT_POLICY_TENANT_ID")))
            or tenants[0] != tenants[1]):
        raise ValueError("staging and chat-policy tenant identities must match")
    for key, expected_scheme, expected_port, expected_database in (
        ("DATABASE_URL", "postgresql", 5432, {"gpoint_staging"}),
        ("REDIS_URL", "redis", 6379, {"15"}),
    ):
        parsed = urlsplit(values[key])
        database = parsed.path.lstrip("/")
        if (parsed.scheme != expected_scheme or not parsed.hostname
                or not INTERNAL_HOST.fullmatch(parsed.hostname)
                or parsed.port not in (None, expected_port)
                or database not in expected_database
                or not parsed.username or not parsed.password):
            raise ValueError(f"{key} must use the isolated Layer7 service endpoint")
        if key == "DATABASE_URL" and parsed.username != "gpoint_staging_runtime":
            raise ValueError("DATABASE_URL must use the dedicated staging database role")
        if parsed.hostname in {"production", "gpoint-production", "maxibot"}:
            raise ValueError(f"{key} points at a forbidden service identity")
    if values.get("GPOINT_INGEST_URL", STAGING_INGEST_URL) != STAGING_INGEST_URL:
        raise ValueError("staging Bot ingest must remain on the non-routable staging placeholder")
    if values.get("PUBLIC_APP_URL", STAGING_APP_URL) != STAGING_APP_URL:
        raise ValueError("staging Bot app links must remain on the non-routable staging placeholder")
    values["GPOINT_INGEST_URL"] = STAGING_INGEST_URL
    values["PUBLIC_APP_URL"] = STAGING_APP_URL
    # These settings are mandatory for the production-mode Bot bootstrap. Keep
    # them pinned to the deliberately non-routable staging placeholder even if
    # the supplied secret bundle omitted them.
    lines = payload.rstrip("\n").splitlines()
    present = {line.partition("=")[0] for line in lines if "=" in line}
    if "GPOINT_INGEST_URL" not in present:
        lines.append(f"GPOINT_INGEST_URL={STAGING_INGEST_URL}")
    if "PUBLIC_APP_URL" not in present:
        lines.append(f"PUBLIC_APP_URL={STAGING_APP_URL}")
    encoded = ("\n".join(lines) + "\n").encode("utf-8")
    return encoded, hashlib.sha256(encoded).hexdigest()
