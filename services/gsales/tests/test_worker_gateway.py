import httpx
import pytest

from app.config import Settings
from app.db.session import GateError
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


@pytest.mark.asyncio
async def test_queue_status_is_authenticated_read_only_and_rejects_wrong_lane_without_leaking_payload():
    settings = Settings(worker_token="sales-worker", agata_api_token="sales-core")
    app = create_app(settings)

    class Core:
        def __init__(self, result):
            self.result = result
            self.calls = []
            self.secure_checks = 0

        async def assert_secure(self, *, require_inference_ready=True):
            self.secure_checks += 1

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return self.result

    expected = {
        "source_id": "agata-worker-queue-status-v1:gsales", "lane": "GSALES",
        "pending_jobs": 2, "running_jobs": 1, "snapshot_at": "2026-10-14T05:00:00Z",
    }
    core = Core(expected)
    app.state.core = core
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        unauthorized = await c.get("/agata-worker/queue-status", headers={"Authorization": "Bearer gpoint-worker"})
        assert unauthorized.status_code == 401
        response = await c.get("/agata-worker/queue-status", headers={"Authorization": "Bearer sales-worker"})
        assert response.status_code == 200
        assert response.json() == expected
    assert core.calls == [("GET", "/internal/v1/worker/queue-status", {})]
    assert core.secure_checks == 1

    core.result = {
        **expected, "source_id": "agata-worker-queue-status-v1:gpoint", "lane": "GPOINT",
        "private_payload": "must-not-escape",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/agata-worker/queue-status", headers={"Authorization": "Bearer sales-worker"})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "worker_core_unavailable"}}
    assert "must-not-escape" not in response.text


@pytest.mark.asyncio
async def test_queue_status_downstream_failure_is_fail_closed():
    settings = Settings(worker_token="sales-worker", agata_api_token="sales-core")
    app = create_app(settings)

    class Core:
        async def assert_secure(self, *, require_inference_ready=True):
            return None

        async def request(self, method, path, **kwargs):
            raise RuntimeError("private downstream details")

    app.state.core = Core()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/agata-worker/queue-status", headers={"Authorization": "Bearer sales-worker"})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "worker_core_unavailable"}}
    assert "private downstream details" not in response.text


@pytest.mark.asyncio
async def test_queue_status_internal_forbidden_is_preserved_without_details():
    settings = Settings(worker_token="sales-worker", agata_api_token="sales-core")
    app = create_app(settings)

    class Core:
        async def assert_secure(self, *, require_inference_ready=True):
            raise GateError("agata_contract_rejected_403")

        async def request(self, method, path, **kwargs):
            raise AssertionError("downstream request must not happen after failed security check")

    app.state.core = Core()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/agata-worker/queue-status", headers={"Authorization": "Bearer sales-worker"})
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "worker_core_unavailable"}}


def test_gateway_refuses_reused_core_token():
    with pytest.raises(ValueError, match="worker_credentials_must_be_distinct"):
        Settings(worker_token="same", agata_api_token="same")
