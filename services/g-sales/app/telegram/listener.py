"""Separate opt-in MTProto update process. No full account history import."""

import asyncio

from telethon import events

from app.config import Settings
from app.core import AgataClient
from app.db.session import Database, GateError
from app.telegram.events import EventRouter
from app.telegram.transport import TelegramTransport


async def run():
    s = Settings()
    if not s.telegram_listener_enabled:
        raise GateError("telegram_listener_disabled")
    db = Database(s)
    core = AgataClient(s)
    transport = TelegramTransport(s)
    router = EventRouter(db, core, transport)
    delay = 1
    while True:
        async with db.transaction() as c:
            await db.assert_secure(c)
            await core.assert_secure()
            accounts = await (
                await c.execute(
                    "SELECT *,telegram_user_id AS account_user_id FROM gsales.telegram_accounts WHERE is_enabled"
                )
            ).fetchall()
        if len(accounts) != 1:
            raise GateError("mvp_requires_one_enabled_account")
        account = accounts[0]
        try:
            async with transport.client(account) as client:

                async def handler(event, account_id=account["id"]):
                    if (
                        not event.is_private
                        or not event.raw_text
                        or not event.chat_id
                        or event.chat_id <= 0
                    ):
                        return
                    try:
                        await router.handle(
                            account_id, event.chat_id, event.id, event.raw_text, event.out
                        )
                    except Exception:
                        # No event contents, auth values or RPC payloads in logs.
                        print("Managed Sales update deferred; check controls and Core readiness.")

                client.add_event_handler(handler, events.NewMessage())
                delay = 1

                async def drain():
                    while True:
                        try:
                            await router.drain_pending()
                        except Exception:
                            print("Sales pending reply remains recoverable; no blind send retry.")
                        await asyncio.sleep(2)

                pending_task = asyncio.create_task(drain())
                try:
                    await client.run_until_disconnected()
                finally:
                    pending_task.cancel()
                    await asyncio.gather(pending_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Sales update connection unavailable; reconnecting with bounded backoff.")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception:
        raise SystemExit(
            "Sales listener stopped; check configured account and security gates."
        ) from None
