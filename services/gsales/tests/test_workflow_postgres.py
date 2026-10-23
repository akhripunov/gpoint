import asyncio
import os
from uuid import uuid4

import psycopg
import pytest

from app.config import GSALES_TENANT_ID, Settings
from app.db.session import Database, GateError
from app.workflow import Workflow, approval_binding, content_hash

pytestmark = pytest.mark.skipif(
    not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL required"
)


class Core:
    async def assert_secure(self):
        return None

    async def read_draft(self, reference, seller):
        return {"text": "AI test proposal", "content_hash": content_hash("AI test proposal")}


class Transport:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def send(self, row, text):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.fail:
            raise TimeoutError("test transport")
        return 987


async def fixture(fail=False):
    ids = [uuid4() for _ in range(4)]
    account, seller, lead, attempt = ids
    uid = int(uuid4().hex[:12], 16)
    with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as c:
        c.execute(
            """INSERT INTO gsales.telegram_accounts(id,tenant_id,display_name,telegram_user_id,session_secret_ref,connection_status,is_enabled)
            VALUES(%s,%s,'test',%s,%s,'authorized',true)""",
            (account, GSALES_TENANT_ID, uid, uuid4()),
        )
        c.execute(
            """INSERT INTO gsales.sellers(id,tenant_id,agata_character_id,telegram_account_id,display_name,status,global_paused)
            VALUES(%s,%s,%s,%s,'test','active',false)""",
            (seller, GSALES_TENANT_ID, uuid4(), account),
        )
        c.execute(
            """INSERT INTO gsales.leads(id,tenant_id,assigned_seller_id,telegram_user_id,telegram_username,display_name,contact_eligibility,contact_eligibility_checked_at,eligibility_evidence)
            VALUES(%s,%s,%s,%s,'recipient','test','allowed',now(),'synthetic owner fixture')""",
            (lead, GSALES_TENANT_ID, seller, uid + 1),
        )
        c.execute(
            """INSERT INTO gsales.dialog_controls(tenant_id,lead_id,seller_id,telegram_account_id,ai_mode)
            VALUES(%s,%s,%s,%s,'awaiting_approval')""",
            (GSALES_TENANT_ID, lead, seller, account),
        )
        c.execute(
            """INSERT INTO gsales.outreach_attempts(id,tenant_id,lead_id,seller_id,telegram_account_id,attempt_type,message_reference,content_hash,telegram_random_id,idempotency_key)
            VALUES(%s,%s,%s,%s,%s,'first','fixture',%s,123,%s)""",
            (
                attempt,
                GSALES_TENANT_ID,
                lead,
                seller,
                account,
                content_hash("AI test proposal"),
                str(attempt),
            ),
        )
    settings = Settings(
        database_url=os.environ["GSALES_TEST_RUNTIME_DSN"],
        owner_telegram_id=42,
        outreach_enabled=True,
    )
    transport = Transport(fail)
    workflow = Workflow(Database(settings), Core(), transport)
    async with workflow.db.transaction() as conn:
        row = await workflow.locked(conn, attempt)
        binding = approval_binding(row)
    await workflow.approve(attempt, binding, 42)
    return workflow, ids


@pytest.mark.asyncio
async def test_concurrent_send_dispatches_exactly_once():
    w, ids = await fixture()
    results = await asyncio.gather(w.send(ids[3]), w.send(ids[3]), return_exceptions=True)
    assert w.transport.calls == 1
    assert sum(isinstance(r, dict) and r["status"] == "sent" for r in results) == 1
    assert any(isinstance(r, GateError) for r in results)


@pytest.mark.asyncio
async def test_unknown_never_blindly_retries():
    w, ids = await fixture(True)
    assert await w.send(ids[3]) == {"status": "unknown"}
    with pytest.raises(GateError, match="reconciliation"):
        await w.send(ids[3])
    assert w.transport.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["global", "seller", "dialog", "takeover", "optout", "username", "content", "assignment"],
)
async def test_send_gate_honors_mutations_after_approval(action):
    w, ids = await fixture()
    _, seller, lead, attempt = ids
    if action == "global":
        await w.pause()
    elif action == "seller":
        await w.pause(seller_id=seller)
    elif action == "dialog":
        await w.pause(lead_id=lead)
    elif action == "takeover":
        await w.takeover(lead)
    elif action == "optout":
        await w.opt_out(lead)
    else:
        async with w.db.transaction() as conn:
            if action == "username":
                await conn.execute(
                    "UPDATE gsales.leads SET telegram_username='changed' WHERE id=%s", (lead,)
                )
            elif action == "content":
                await conn.execute(
                    "UPDATE gsales.outreach_attempts SET content_hash='changed' WHERE id=%s",
                    (attempt,),
                )
            else:
                await conn.execute(
                    "UPDATE gsales.sellers SET telegram_account_id=NULL WHERE id=%s", (seller,)
                )
    with pytest.raises(GateError):
        await w.send(attempt)
    assert w.transport.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after_commit", [False, True])
async def test_pending_reply_recovers_and_sends_once(crash_after_commit):
    from app.telegram.events import EventRouter

    w, ids = await fixture()
    await w.send(ids[3])
    w.settings.auto_reply_enabled = True
    async with w.db.transaction() as c:
        await c.execute("UPDATE gsales.sellers SET auto_reply_enabled=true WHERE id=%s", (ids[1],))
        await c.execute(
            "UPDATE gsales.dialog_controls SET ai_mode='auto_reply' WHERE lead_id=%s", (ids[2],)
        )
        lead = await (
            await c.execute("SELECT telegram_user_id FROM gsales.leads WHERE id=%s", (ids[2],))
        ).fetchone()

    class PendingCore(Core):
        async def respond(self, *args):
            return {"status": "pending"}

        async def poll_reply(self, *args):
            return {
                "status": "completed",
                "text": "AI test proposal",
                "user_id": str(uuid4()),
                "relationship_id": str(uuid4()),
            }

        async def draft(self, *args):
            return {
                "message_reference": str(uuid4()),
                "text": "AI test proposal",
                "content_hash": content_hash("AI test proposal"),
            }

    router = EventRouter(w.db, PendingCore(), w.transport)
    assert await router.handle(ids[0], lead["telegram_user_id"], 100, "test inbound") == {
        "status": "core_pending"
    }
    if crash_after_commit:
        async def crash_before_claim(*args):
            raise RuntimeError("simulated_process_exit_before_claim")
        router.w.send = crash_before_claim
        with pytest.raises(RuntimeError, match="simulated_process_exit"):
            await router.drain_pending()
        assert w.transport.calls == 1
        router = EventRouter(w.db, PendingCore(), w.transport)
    await router.drain_pending()
    await router.drain_pending()
    assert w.transport.calls == 2  # one first contact plus exactly one reply
    async with w.db.transaction() as c:
        state = await (
            await c.execute(
                "SELECT pending_inbound_message_id,auto_reply_count FROM gsales.dialog_controls WHERE lead_id=%s",
                (ids[2],),
            )
        ).fetchone()
        assert state == {"pending_inbound_message_id": None, "auto_reply_count": 1}


@pytest.mark.asyncio
async def test_explicit_consent_accepted_while_paused_without_enabling_sends():
    from app.telegram.events import EventRouter

    w, ids = await fixture()
    await w.pause(lead_id=ids[2])
    async with w.db.transaction() as c:
        lead = await (
            await c.execute("SELECT telegram_user_id FROM gsales.leads WHERE id=%s", (ids[2],))
        ).fetchone()

    class ConsentCore(Core):
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json")))
            if method == "GET":
                return {"disclosureVersion": 7, "disclosureRequired": True}
            return {"ok": True}

    core = ConsentCore()
    router = EventRouter(w.db, core, w.transport)
    assert await router.handle(ids[0], lead["telegram_user_id"], 100, "/gsales_consent 18+ history disclosure=6") == {
        "status": "disclosure_version_mismatch"
    }
    assert not any(method == "PUT" for method, _, _ in core.calls)
    assert await router.handle(ids[0], lead["telegram_user_id"], 101, "/gsales_consent 18+ history disclosure=7") == {
        "status": "explicit_consent_received"
    }
    consent = next(payload for method, path, payload in core.calls if method == "PUT")
    assert consent["long_term_memory"] is True
    assert consent["disclosure_version"] == 7
    assert consent["disclosure_receipt_id"].endswith(":101")
    assert w.transport.calls == 0


@pytest.mark.asyncio
async def test_legacy_consent_command_without_disclosure_version_fails_closed():
    from app.telegram.events import EventRouter

    w, ids = await fixture()
    async with w.db.transaction() as c:
        lead = await (
            await c.execute("SELECT telegram_user_id FROM gsales.leads WHERE id=%s", (ids[2],))
        ).fetchone()

    class UnusedCore(Core):
        calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("stale disclosure command must not reach Core")

    core = UnusedCore()
    router = EventRouter(w.db, core, w.transport)
    result = await router.handle(
        ids[0], lead["telegram_user_id"], 101, "/gsales_consent 18+ history memory_off"
    )
    assert result == {"status": "disclosure_version_required"}
    assert core.calls == 0
    assert w.transport.calls == 0
