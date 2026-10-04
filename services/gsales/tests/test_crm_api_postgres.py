import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import GSALES_TENANT_ID, Settings
from app.main import create_app
from app.workflow import content_hash

pytestmark = pytest.mark.skipif(
    not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL required"
)


def test_owner_crm_dedup_and_resume_controls():
    settings = Settings(
        database_url=os.environ["GSALES_TEST_RUNTIME_DSN"],
        owner_api_token="test",
        owner_telegram_id=42,
    )
    app = create_app(settings)

    class Core:
        async def assert_secure(self):
            pass

        async def sync_seller(self, *args):
            return {"character_id": str(uuid4())}

        async def draft(self, *args):
            return {
                "message_reference": str(uuid4()),
                "text": "AI proposal",
                "content_hash": content_hash("AI proposal"),
            }

    app.state.core = Core()
    client = TestClient(app)
    headers = {"Authorization": "Bearer test"}
    with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as c:
        account = uuid4()
        c.execute(
            "INSERT INTO gsales.telegram_accounts(id,tenant_id,display_name,telegram_user_id,session_secret_ref) VALUES(%s,%s,'mock',%s,%s)",
            (account, GSALES_TENANT_ID, int(uuid4().hex[:12], 16), uuid4()),
        )
        c.execute("UPDATE gsales.sellers SET status='paused'")
    seller = client.post(
        "/v1/sellers",
        headers=headers,
        json={
            "display_name": "G Sales RU",
            "personality": {"purpose": "test"},
            "telegram_account_id": str(account),
        },
    )
    assert seller.status_code == 200, seller.text
    sid = seller.json()["id"]
    body = {
        "seller_id": sid,
        "telegram_user_id": int(uuid4().hex[:12], 16),
        "telegram_username": "test_recipient",
        "display_name": "test",
        "source_type": "manual",
        "source_reference": "synthetic test",
    }
    lead = client.post("/v1/leads", headers=headers, json=body)
    assert lead.status_code == 200, lead.text
    assert client.post("/v1/leads", headers=headers, json=body).json()["id"] == lead.json()["id"]
    assert (
        client.post("/v1/leads", headers=headers, json={**body, "tenant_id": "foreign"}).status_code
        == 422
    )
    resume = client.post(
        "/v1/resume",
        headers=headers,
        json={"scope": "seller", "target_id": sid, "auto_reply": True},
    )
    assert resume.status_code == 200, resume.text
    sellers = client.get("/v1/sellers", headers=headers).json()
    assert next(s for s in sellers if s["id"] == sid)["auto_reply_enabled"] is True
