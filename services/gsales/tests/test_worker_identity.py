"""Worker recovery still requires Core's fixed identity and proven security."""
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.core import AgataClient
from app.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"passed": False}, {"tenant_id": "foreign"}, {"source_system": "gpoint"}])
async def test_worker_recovery_refuses_unproven_static_core_identity(invalid):
    settings = Settings(production_mode=True, tenant_id=str(uuid4()), agata_api_token="core-test", worker_token="worker-test")
    security = {"passed": True, "tenant_id": settings.tenant_id, "source_system": "gsales", "production_inference_ready": False, **invalid}

    def core_http(request):
        if request.url.path == "/internal/v1/sales/runtime-security":
            return httpx.Response(200, json=security)
        return httpx.Response(200, json={"job": None})

    app = create_app(settings)
    app.state.core = AgataClient(settings, transport=httpx.MockTransport(core_http))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sales") as client:
        for action in ("health", "claim"):
            assert (await client.post("/agata-worker/" + action, headers={"Authorization": "Bearer worker-test"})).status_code == 503
