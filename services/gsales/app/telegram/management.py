"""Owner-selected existing Bot API identity; never creates a bot or credentials."""

import hmac

import httpx
from fastapi import Header, HTTPException, Request

from app.db.session import GateError
from app.workflow import Workflow, approval_binding


class ManagementBot:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def call(self, method, body):
        s = self.settings
        if not s.management_enabled or not s.management_bot_token or not s.management_bot_id:
            raise GateError("management_bot_not_configured")
        # The token stays in the HTTP URL in memory only; errors are sanitized.
        async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
            base = "https://api.telegram.org/bot" + s.management_bot_token.get_secret_value() + "/"
            me = await client.post(base + "getMe", json={})
            identity = me.json()
            if (
                not identity.get("ok")
                or identity.get("result", {}).get("id") != s.management_bot_id
            ):
                raise GateError("management_bot_identity_changed")
            result = await client.post(base + method, json=body)
            if not result.is_success or not result.json().get("ok"):
                raise GateError("management_bot_request_failed")
            return result.json()["result"]


def install(app):
    app.state.management = ManagementBot(app.state.settings)

    @app.post("/v1/telegram/management/webhook")
    async def webhook(
        request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
    ):
        s = app.state.settings
        secret = s.management_webhook_secret
        if (
            not s.management_enabled
            or not secret
            or not x_telegram_bot_api_secret_token
            or not hmac.compare_digest(secret.get_secret_value(), x_telegram_bot_api_secret_token)
        ):
            raise HTTPException(401, detail={"code": "unauthorized"})
        body = await request.json()
        message = body.get("message") or body.get("callback_query", {}).get("message", {})
        sender = (body.get("callback_query") or message).get("from", {}).get("id")
        if (
            sender != s.owner_telegram_id
            or message.get("chat", {}).get("id") != s.owner_telegram_id
            or message.get("chat", {}).get("type") != "private"
        ):
            raise HTTPException(403, detail={"code": "owner_required"})
        if not s.owner_api_token:
            raise GateError("owner_api_not_configured")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://management-local",
            headers={"Authorization": "Bearer " + s.owner_api_token.get_secret_value()},
        ) as client:
            callback = body.get("callback_query")
            if callback:
                parts = callback.get("data", "").split(":")
                if len(parts) != 3 or parts[0] not in ("a", "r", "e"):
                    raise GateError("callback_invalid")
                from uuid import UUID

                attempt_id = UUID(parts[1])
                w = Workflow(app.state.db, app.state.core, getattr(app.state, "telegram", None))
                async with app.state.db.transaction() as c:
                    row = await w.locked(c, attempt_id)
                    binding = approval_binding(row)
                    if len(parts[2]) != 24 or not hmac.compare_digest(parts[2], binding[:24]):
                        raise GateError("approval_stale")
                if parts[0] == "a":
                    response = await client.post(
                        f"/v1/attempts/{attempt_id}/approve", json={"expected_binding": binding}
                    )
                    text = (
                        "Согласовано. " + response.text
                        if response.is_error
                        else "Согласовано. Отправка выполняется отдельной командой /send "
                        + str(attempt_id)
                    )
                elif parts[0] == "r":
                    response = await client.post(f"/v1/attempts/{attempt_id}/reject")
                    text = "Отклонено." if response.is_success else response.text
                else:
                    text = (
                        "/edit "
                        + str(attempt_id)
                        + " новый текст — затем потребуется новое согласование."
                    )
                await app.state.management.call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback["id"], "text": "Обработано"},
                )
            else:
                raw = message.get("text", "").strip()
                command = raw.split(" ", 1)[0].split("@")[0]
                path = {
                    "/status": "/v1/status",
                    "/sellers": "/v1/sellers",
                    "/leads": "/v1/leads",
                    "/pending": "/v1/pending",
                }.get(command)
                if command == "/start":
                    text = "G Sales: /status /sellers /leads /pending /pause /resume. Первое сообщение требует согласования."
                elif path:
                    response = await client.get(path)
                    if command == "/pending" and response.is_success:
                        for row in response.json()[:20]:
                            token = row["id"].replace("-", "") + ":" + row["approval_binding"][:24]
                            await app.state.management.call(
                                "sendMessage",
                                {
                                    "chat_id": s.owner_telegram_id,
                                    "text": f"Получатель @{row['telegram_username']} ({row['telegram_user_id']})\nПродавец {row['seller_id']}\nАккаунт {row['telegram_account_id']}\n\n{row['text']}",
                                    "reply_markup": {
                                        "inline_keyboard": [
                                            [
                                                {"text": "Одобрить", "callback_data": "a:" + token},
                                                {"text": "Изменить", "callback_data": "e:" + token},
                                                {
                                                    "text": "Отклонить",
                                                    "callback_data": "r:" + token,
                                                },
                                            ]
                                        ]
                                    },
                                },
                            )
                        text = "Ожидает согласования: " + str(len(response.json()))
                    else:
                        text = response.text[:3500]
                elif command == "/pause":
                    await client.post("/v1/pause")
                    text = "G Sales остановлен."
                elif command == "/resume":
                    response = await client.post("/v1/resume", json={"scope": "global"})
                    text = (
                        "Глобальная пауза снята; настройки продавца и диалога проверяются отдельно."
                        if response.is_success
                        else response.text
                    )
                elif command == "/send" and len(raw.split()) == 2:
                    response = await client.post("/v1/attempts/" + raw.split()[1] + "/send")
                    text = response.text
                elif command == "/takeover" and len(raw.split()) == 2:
                    response = await client.post("/v1/leads/" + raw.split()[1] + "/takeover")
                    text = response.text
                elif command == "/edit" and len(raw.split(" ", 2)) == 3:
                    from uuid import UUID, uuid4

                    _, identifier, new_text = raw.split(" ", 2)
                    identifier = UUID(identifier)
                    async with app.state.db.transaction() as c:
                        w = Workflow(app.state.db, app.state.core, None)
                        row = await w.locked(c, identifier)
                        if row["delivery_status"] != "draft":
                            raise GateError("cannot_edit_after_claim")
                        await c.execute(
                            "UPDATE gsales.outreach_attempts SET approval_status='rejected' WHERE id=%s",
                            (identifier,),
                        )
                    response = await client.post(
                        f"/v1/leads/{row['lead_id']}/drafts",
                        json={"request_id": uuid4().hex, "text": new_text},
                    )
                    text = (
                        "Новый черновик требует согласования: /pending"
                        if response.is_success
                        else response.text
                    )
                else:
                    text = "Команда не распознана. /start"
            await app.state.management.call(
                "sendMessage", {"chat_id": s.owner_telegram_id, "text": text[:4000]}
            )
        return {"ok": True}
