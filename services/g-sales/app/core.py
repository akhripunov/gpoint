"""The only G Sales boundary to AGATA is its authenticated HTTP API."""

import httpx

from app.config import Settings
from app.db.session import GateError


class AgataClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def request(self, method, path, **kwargs):
        if not self.settings.agata_api_token:
            raise GateError("agata_not_configured")
        async with httpx.AsyncClient(
            base_url=self.settings.agata_url,
            transport=self.transport,
            timeout=20,
            headers={"Authorization": "Bearer " + self.settings.agata_api_token.get_secret_value()},
        ) as client:
            response = await client.request(method, path, **kwargs)
            if response.is_error:
                raise GateError("agata_contract_rejected_" + str(response.status_code))
            result = response.json()
            if response.status_code == 202:
                raise GateError("agata_draft_pending:" + str(result.get("message_reference", "")))
            return result

    async def assert_secure(self):
        result = await self.request("GET", "/internal/v1/sales/runtime-security")
        if any(result.get(k)!=v for k,v in {
            "passed": True,
            "tenant_id": self.settings.tenant_id,
            "source_system": "gsales",
        }.items()):
            raise GateError("agata_security_unproven")
        if self.settings.production_mode and not result.get("production_inference_ready"):
            raise GateError("sales_always_on_inference_not_approved")

    async def sync_seller(self, seller_id, name, personality):
        return await self.request(
            "PUT",
            f"/internal/v1/characters/{seller_id}",
            json={"name": name, "personality": personality},
        )

    async def draft(self, seller_id, request_id, text, facts):
        body = {
            "request_id": request_id,
            "character_external_id": seller_id,
            "verified_facts": facts,
        }
        if text is not None:
            body["text"] = text
        return await self.request("POST", "/internal/v1/sales/drafts", json=body)

    async def read_draft(self, reference, seller_id):
        return await self.request(
            "GET",
            f"/internal/v1/sales/drafts/{reference}",
            params={"character_external_id": seller_id},
        )

    async def respond(self, lead_id, seller_id, message_id, text, locale="ru"):
        return await self.request(
            "POST",
            "/internal/v1/respond",
            json={
                "request_id": f"gsales:{seller_id}:{lead_id}:{message_id}",
                "gpoint_user_id": str(lead_id),
                "gpoint_character_id": str(seller_id),
                "conversation_id": f"gsales-chat:{lead_id}:{seller_id}",
                "message_id": f"gsales:{seller_id}:{lead_id}:{message_id}",
                "text": text,
                "locale": locale,
            },
        )

    async def poll_reply(self, lead_id, seller_id, message_id):
        return await self.request(
            "GET",
            "/internal/v1/sales/replies",
            params={
                "user_external_id": str(lead_id),
                "character_external_id": str(seller_id),
                "source_message_key": f"gsales:{seller_id}:{lead_id}:{message_id}",
            },
        )

    async def record_delivery(self, row):
        return await self.request(
            "POST",
            "/internal/v1/sales/deliveries",
            json={
                "user_external_id": str(row["lead_id"]),
                "character_external_id": str(row["seller_id"]),
                "draft_reference": str(row["message_reference"]),
                "provider_message_id": row["provider_message_id"],
                "occurred_at": row["sent_at"].isoformat(),
                "source_message_key": f"gsales:{row['seller_id']}:{row['lead_id']}:{row['inbound_message_id']}"
                if row["attempt_type"] == "reply"
                else None,
            },
        )
