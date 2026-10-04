import httpx
import pytest

from app.config import Settings
from app.db.session import GateError
from app.gpoint import GPointClient


@pytest.mark.asyncio
async def test_registration_requires_authenticated_matching_identity():
    with pytest.raises(GateError, match="contract_pending"):
        await GPointClient(Settings()).registration(123)
    settings = Settings(
        gpoint_registration_contract_enabled=True,
        gpoint_url="https://gpoint.test",
        gpoint_api_token="mock",
    )

    def wrong(request):
        assert request.headers["Authorization"] == "Bearer mock"
        return httpx.Response(
            200, json={"registered": True, "telegram_user_id": 99, "creator_id": "creator"}
        )

    with pytest.raises(GateError, match="unverified"):
        await GPointClient(settings, httpx.MockTransport(wrong)).registration(123)
