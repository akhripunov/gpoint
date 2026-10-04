from contextlib import asynccontextmanager

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from app.db.session import GateError
from app.telegram.vault import SessionVault
from app.workflow import content_hash


class TelegramTransport:
    def __init__(self, settings, client_factory=TelegramClient):
        self.vault = SessionVault(settings)
        self.client_factory = client_factory

    @asynccontextmanager
    async def client(self, row):
        credentials = self.vault.load(row["session_secret_ref"])
        client = self.client_factory(
            StringSession(credentials["session"]),
            credentials["api_id"],
            credentials["api_hash"],
            request_retries=0,
            connection_retries=0,
            auto_reconnect=False,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise GateError("telegram_login_required")
            me = await client.get_me()
            if me.id != row["account_user_id"] or me.bot:
                raise GateError("telegram_account_identity_changed")
            yield client
        finally:
            await client.disconnect()

    async def peer(self, client, row):
        # Resolve public username, then compare stable approved user ID.
        entity = await client.get_entity(row["telegram_username"])
        if (
            not isinstance(entity, types.User)
            or entity.id != row["telegram_user_id"]
            or entity.bot
            or entity.deleted
        ):
            raise GateError("telegram_recipient_identity_changed")
        return await client.get_input_entity(entity)

    async def send(self, row, text):
        async with self.client(row) as client:
            peer = await self.peer(client, row)
            result = await client(
                functions.messages.SendMessageRequest(
                    peer=peer, message=text, random_id=row["telegram_random_id"], no_webpage=True
                )
            )
            if isinstance(result, types.UpdateShortSentMessage):
                return result.id
            for update in getattr(result, "updates", []):
                if (
                    isinstance(update, types.UpdateMessageID)
                    and update.random_id == row["telegram_random_id"]
                ):
                    return update.id
            # A response without an exact random_id mapping is uncertain.
            raise RuntimeError("telegram_send_unconfirmed")

    async def reconcile(self, row):
        # Only this explicitly managed private dialog, bounded since dispatch.
        async with self.client(row) as client:
            peer = await self.peer(client, row)
            matches = []
            async for message in client.iter_messages(peer, limit=50):
                if row["attempted_at"] and message.date < row["attempted_at"]:
                    break
                if message.out and content_hash(message.message or "") == row["content_hash"]:
                    matches.append(message.id)
            return matches[0] if len(matches) == 1 else None

    async def read_inbound(self, row, message_id):
        async with self.client(row) as client:
            peer = await self.peer(client, row)
            message = await client.get_messages(peer, ids=message_id)
            if not message or message.out or message.id != message_id:
                return None
            return message.message
