from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import GSALES_TENANT_ID, Settings
from app.main import create_app


def test_safe_defaults_and_configurable_tenant():
    settings = Settings()
    assert settings.tenant_id == GSALES_TENANT_ID
    assert not settings.outreach_enabled
    assert not settings.auto_reply_enabled
    assert not settings.management_enabled
    assert Settings(tenant_id="00000000-0000-4000-8000-000000000001").tenant_id == "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ValueError):
        Settings(tenant_id="not-a-uuid")


def test_production_rejects_the_disposable_default_tenant():
    with pytest.raises(ValueError, match="production_tenant_must_be_provisioned"):
        Settings(production_mode=True)


def test_independent_health_and_owner_auth():
    with TestClient(create_app(Settings())) as client:
        assert client.get("/health/live").json() == {"status": "ok", "service": "g-sales"}
        assert client.get("/v1/status").status_code == 401
        assert (
            client.get("/v1/status", headers={"Authorization": "Bearer anything"}).status_code
            == 401
        )


def test_missing_database_fails_closed():
    with TestClient(create_app(Settings())) as client:
        assert client.get("/health/ready").status_code == 503


def test_production_readiness_reports_stale_shared_worker_as_degraded():
    app = create_app(Settings(production_mode=True, owner_api_token="owner-test", tenant_id="00000000-0000-4000-8000-000000000001"))

    class SecureDatabase:
        @asynccontextmanager
        async def transaction(self):
            yield object()

        async def assert_secure(self, _conn):
            return None

    class StaleCore:
        async def assert_secure(self):
            raise ValueError("inference_provider_not_ready")

    app.state.db = SecureDatabase()
    app.state.core = StaleCore()
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["detail"]["code"] == "inference_provider_degraded"
        status = client.get("/v1/status", headers={"Authorization": "Bearer owner-test"})
        assert status.status_code == 200
        assert status.json()["inference_status"] == "degraded"
