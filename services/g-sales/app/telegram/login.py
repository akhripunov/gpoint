"""Explicit interactive account setup; never called by startup or test runners."""

import asyncio
import getpass
from uuid import uuid4

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from app.config import Settings
from app.db.session import Database, GateError
from app.telegram.vault import SessionVault


async def login():
    settings = Settings()
    vault = SessionVault(settings)
    db = Database(settings)
    async with db.transaction() as c:
        await db.assert_secure(c)
        active = await (
            await c.execute("SELECT count(*) AS n FROM gsales.telegram_accounts WHERE is_enabled")
        ).fetchone()
        if active["n"]:
            raise GateError("mvp_account_already_connected")
    api_id = int(getpass.getpass("Telegram API ID: "))
    api_hash = getpass.getpass("Telegram API hash: ")
    phone = getpass.getpass("Telegram phone: ")
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        request_retries=0,
        connection_retries=0,
        flood_sleep_threshold=0,
    )
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        code = getpass.getpass("Telegram login code: ")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Telegram 2FA password: "))
        me = await client.get_me()
        if not me or me.bot:
            raise GateError("personal_account_required")
        reference = vault.store(
            {"session": StringSession.save(client.session), "api_id": api_id, "api_hash": api_hash}
        )
        async with db.transaction() as c:
            await c.execute("SELECT pg_advisory_xact_lock(723802)")
            active = await (
                await c.execute(
                    "SELECT count(*) AS n FROM gsales.telegram_accounts WHERE is_enabled"
                )
            ).fetchone()
            if active["n"]:
                raise GateError("mvp_account_already_connected")
            await c.execute(
                """INSERT INTO gsales.telegram_accounts(id,tenant_id,display_name,telegram_user_id,telegram_username,session_secret_ref,connection_status,is_enabled,last_connected_at)
                VALUES(%s,%s,'Owner-approved Sales account',%s,%s,%s,'authorized',true,now())""",
                (uuid4(), settings.tenant_id, me.id, me.username, reference),
            )
        print(
            "Telegram account connected; outreach remains governed by OFF defaults and owner approval."
        )
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(login())
    except Exception:
        # Avoid exception payloads containing a phone number/API secret.
        raise SystemExit("Telegram setup failed; no sensitive error details printed.") from None
