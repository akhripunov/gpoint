import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon import types

from app.config import Settings
from app.db.session import GateError
from app.main import create_app
from app.telegram.transport import TelegramTransport


def test_management_webhook_denies_unconfigured_or_foreign_owner():
    assert (
        TestClient(create_app()).post("/v1/telegram/management/webhook", json={}).status_code == 401
    )
    client = TestClient(
        create_app(
            Settings(
                management_enabled=True, management_webhook_secret="test", owner_telegram_id=42
            )
        )
    )
    assert (
        client.post(
            "/v1/telegram/management/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test"},
            json={
                "message": {
                    "from": {"id": 99},
                    "chat": {"id": 99, "type": "private"},
                    "text": "/pause",
                }
            },
        ).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_telegram_stable_identity_and_exact_random_id(tmp_path, monkeypatch):
    from app.telegram import transport as module

    monkeypatch.setattr(module, "StringSession", lambda x: x)

    class Client:
        def __init__(self, *args, **kwargs):
            assert (
                kwargs["request_retries"]
                == kwargs["connection_retries"]
                == kwargs["flood_sleep_threshold"]
                == 0
            )
            assert kwargs["auto_reconnect"] is False
            self.sent = []

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=1, bot=False)

        async def get_entity(self, name):
            return types.User(id=2, access_hash=3, username="recipient")

        async def get_input_entity(self, entity):
            return types.InputPeerUser(entity.id, entity.access_hash)

        async def __call__(self, request):
            self.sent.append(request)
            return SimpleNamespace(
                updates=[types.UpdateMessageID(id=99, random_id=request.random_id)]
            )

    settings = Settings(
        secret_dir=str(tmp_path), secret_master_key=base64.urlsafe_b64encode(b"a" * 32).decode()
    )
    t = TelegramTransport(settings, Client)
    ref = t.vault.store({"session": "mock-session", "api_id": 1, "api_hash": "mock"})
    row = {
        "session_secret_ref": ref,
        "account_user_id": 1,
        "telegram_user_id": 2,
        "telegram_username": "recipient",
        "telegram_random_id": 123,
    }
    assert await t.send(row, "test") == 99
    with pytest.raises(GateError, match="recipient_identity"):
        await t.send({**row, "telegram_user_id": 3}, "test")
    with pytest.raises(GateError, match="account_identity"):
        await t.send({**row, "account_user_id": 4}, "test")
