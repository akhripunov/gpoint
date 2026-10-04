"""Registration requires a verified GPoint service contract, never a chat inference."""

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.db.session import GateError


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_user_id: int = Field(gt=0)
    creator_id: str = Field(min_length=1, max_length=255)
    registered: bool


class GPointClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def registration(self, user_id):
        s = self.settings
        # Existing GPoint endpoints are user-scoped; no service lookup contract
        # has been approved. This adapter remains explicitly fail-closed.
        if not s.gpoint_registration_contract_enabled or not s.gpoint_url or not s.gpoint_api_token:
            raise GateError("gpoint_registration_contract_pending")
        async with httpx.AsyncClient(
            base_url=s.gpoint_url,
            transport=self.transport,
            timeout=5,
            headers={"Authorization": "Bearer " + s.gpoint_api_token.get_secret_value()},
        ) as client:
            response = await client.get(f"/internal/v1/sales/registrations/{user_id}")
            if response.status_code == 404:
                return None
            if response.is_error:
                raise GateError("gpoint_registration_unavailable")
            record = Registration.model_validate(response.json())
            if record.telegram_user_id != user_id or not record.registered:
                raise GateError("gpoint_registration_unverified")
            return record
