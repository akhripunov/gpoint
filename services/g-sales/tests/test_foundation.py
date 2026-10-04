import pytest
from fastapi.testclient import TestClient

from app.config import GSALES_TENANT_ID, Settings
from app.main import create_app


def test_safe_defaults_and_fixed_tenant():
    settings = Settings()
    assert settings.tenant_id == GSALES_TENANT_ID
    assert not settings.outreach_enabled
    assert not settings.auto_reply_enabled
    assert not settings.management_enabled
    with pytest.raises(ValueError):
        Settings(tenant_id="00000000-0000-4000-8000-000000000001")


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
