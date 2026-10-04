import httpx
import pytest

from app.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_gateway_credentials_and_fixed_identity():
    settings = Settings(worker_token="sales-worker", agata_api_token="sales-core")
    app = create_app(settings)

    class Core:
        calls = []

        async def assert_secure(self, *, require_inference_ready=True):
            pass

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"job": {"id": "fixture", "target": "GPOINT", "tenant_id": "foreign"}}

    core = Core()
    app.state.core = core
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        for token in ("", "owner", "gpoint-worker", "sales-core"):
            response = await c.post("/agata-worker/claim", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401
        headers = {"Authorization": "Bearer sales-worker"}
        assert (await c.post("/agata-worker/claim", headers=headers)).status_code == 503
        assert (await c.post("/agata-worker/complete", headers=headers, json={"target": "GPOINT"})).status_code == 422
    assert core.calls[0][1] == "/internal/v1/worker/claim"


def test_gateway_refuses_reused_core_token():
    with pytest.raises(ValueError, match="worker_credentials_must_be_distinct"):
        Settings(worker_token="same", agata_api_token="same")
