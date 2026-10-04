"""Production-mode recovery through Sales gateway, real Core HTTP and PostgreSQL."""
import os
from uuid import uuid4

import httpx
import psycopg
import pytest
from test_http_e2e import core_runtime

from app.config import GSALES_TENANT_ID, Settings
from app.db.session import GateError
from app.main import create_app

pytestmark = pytest.mark.skipif(not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL required")


@pytest.mark.asyncio
async def test_production_gateway_recovers_cold_start_and_stale_worker_poll(tmp_path):
    tenant = str(uuid4())
    with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as admin:
        admin.execute("INSERT INTO agata.tenants(id) VALUES (%s)", (tenant,))
        admin.execute("UPDATE agata_identity.role_tenants SET tenant_id=%s WHERE login_role IN ('gsales_runtime','gsales_agata_runtime')", (tenant,))
    try:
        with core_runtime(tmp_path, env_overrides={"AGATA_TENANT_ID": tenant, "AGATA_SALES_PRODUCTION_MODE": "true"}) as url:
            app = create_app(Settings(
                production_mode=True, tenant_id=tenant,
                database_url=os.environ["GSALES_TEST_RUNTIME_DSN"],
                agata_url=url, agata_api_token="disposable-e2e-core",
                owner_api_token="owner-test", worker_token="sales-worker-test",
            ))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sales") as client:
                headers = {"Authorization": "Bearer sales-worker-test"}
                owner = {"Authorization": "Bearer owner-test"}
                for state in ("cold", "stale"):
                    if state == "stale":
                        async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer disposable-e2e-core"}) as core:
                            assert (await core.post("/test/stale-worker-poll")).status_code == 200
                    # API readiness and the default Core gate must keep reporting the
                    # real provider outage even though the worker can recover it.
                    assert (await client.get("/health/ready")).status_code == 503
                    assert (await client.get("/v1/status", headers=owner)).json()["inference_status"] == "degraded"
                    with pytest.raises(GateError, match="inference_provider_not_ready"):
                        await app.state.core.assert_secure()
                    assert (await client.post("/agata-worker/health", headers=headers)).status_code == 200
                    claimed = await client.post("/agata-worker/claim", headers=headers)
                    assert claimed.status_code == 200, claimed.text
                    # A successful DB-backed empty claim renews the real live poll.
                    assert claimed.json() == {"job": None}
                    assert (await client.get("/health/ready")).status_code == 200
                    status = (await client.get("/v1/status", headers=owner)).json()
                    assert status["inference_status"] == "ready"
                    assert status["outreach_enabled"] is False and status["auto_reply_enabled"] is False
    finally:
        with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as admin:
            admin.execute("UPDATE agata_identity.role_tenants SET tenant_id=%s WHERE login_role IN ('gsales_runtime','gsales_agata_runtime')", (GSALES_TENANT_ID,))
