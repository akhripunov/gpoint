"""Actual shared Core HTTP + actual PostgreSQL + synthetic Telegram delivery."""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

from app.config import GSALES_TENANT_ID, Settings
from app.main import create_app
from app.telegram.events import EventRouter

pytestmark = pytest.mark.skipif(
    not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL required"
)
ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures"


@contextmanager
def core_runtime(tmp_path, source="gsales", tenant_id=None, env_overrides=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGATA_", "GSALES_"))}
    key = base64.b64encode(b"a" * 32).decode()
    env.update(
        PYTHONPATH=str(ROOT / "services/agata"),
        AGATA_DATABASE_URL=os.environ["GSALES_TEST_AGATA_DSN" if source == "gsales" else "GSALES_TEST_GPOINT_DSN"].replace(
            "postgresql://", "postgresql+asyncpg://"
        ),
        AGATA_TENANT_ID=GSALES_TENANT_ID if source == "gsales" else (tenant_id or "7508f36f-774a-48aa-8c6b-42f6f160415d"),
        AGATA_SOURCE_SYSTEM=source,
        AGATA_INTERNAL_API_TOKEN="disposable-e2e-core" if source == "gsales" else "disposable-point-core",
        AGATA_LLM_PROVIDER="mac_worker",
        AGATA_LLM_MODEL="codex-default",
        AGATA_CODEX_LOCAL_ENABLED="true",
        AGATA_WORKER_WAIT_SECONDS="0.5",
        AGATA_SALES_DRAFTS_ENABLED="true",
    )
    for suffix in (
        "MESSAGE_AEAD_KEY",
        "REQUEST_FINGERPRINT_HMAC_KEY",
        "RETRIEVAL_QUERY_HMAC_KEY",
        "SHADOW_CANDIDATE_HMAC_KEY",
        "RESPONSE_FINGERPRINT_HMAC_KEY",
    ):
        env["AGATA_" + suffix] = key
    env.update(env_overrides or {})
    with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as c:
        c.execute("UPDATE gsales.sellers SET status='paused'")
        if source == "gpoint":
            point_tenant = env["AGATA_TENANT_ID"]
            c.execute("INSERT INTO agata.tenants(id) VALUES(%s) ON CONFLICT DO NOTHING", (point_tenant,))
            c.execute(
                """INSERT INTO agata.regional_policy_profiles(tenant_id,jurisdiction,market,version,status,effective_from,rules)
                VALUES(%s,'GLOBAL','GPOINT',1,'active',now(),
                  '{"ai_disclosure_required":true,"self_attestation_allowed":true,"adult_only_features":[],"disclosure_repeat_hours":24}')
                ON CONFLICT DO NOTHING""",
                (point_tenant,),
            )
        if source == "gpoint" and tenant_id:
            c.execute(
                "UPDATE agata_identity.role_tenants SET tenant_id=%s WHERE login_role='gpoint_test_runtime'",
                (point_tenant,),
            )
        c.execute(
            """INSERT INTO agata.regional_policy_profiles(tenant_id,jurisdiction,market,version,status,effective_from,rules)
            VALUES(%s,'GLOBAL','GSALES',1,'active',now(),'{"ai_disclosure_required":true,"self_attestation_allowed":true,"adult_only_features":[]}') ON CONFLICT DO NOTHING""",
            (GSALES_TENANT_ID,),
        )
    tmp_path.mkdir(parents=True, exist_ok=True)
    with (tmp_path / "core.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(FIXTURES / "core_server.py"), str(port)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(
                        "test Core server stopped: " + (tmp_path / "core.log").read_text()[-2000:]
                    )
                try:
                    if httpx.get(url + "/health/live", timeout=0.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("test Core server did not become ready")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def core_server(tmp_path):
    with core_runtime(tmp_path) as url:
        yield url


def worker_rpc(url, action, payload=None):
    env = {**os.environ, "GSALES_E2E_CORE_URL": url}
    result = subprocess.run(
        [sys.executable, str(FIXTURES / "worker_rpc.py"), action],
        env=env,
        input=json.dumps(payload) if payload else None,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


class Telegram:
    def __init__(self):
        self.messages = []
        self.timeout = False

    async def send(self, row, text):
        self.messages.append((row["lead_id"], row["telegram_random_id"], text))
        if self.timeout:
            raise TimeoutError("synthetic lost acknowledgement")
        return 1000 + len(self.messages)


@pytest.mark.asyncio
async def test_real_http_pending_worker_restart_delivery_and_context(core_server):
    settings = Settings(
        database_url=os.environ["GSALES_TEST_RUNTIME_DSN"],
        agata_url=core_server,
        agata_api_token="disposable-e2e-core",
        owner_api_token="owner-test",
        owner_telegram_id=42,
        outreach_enabled=True,
        auto_reply_enabled=True,
    )
    app = create_app(settings)
    telegram = Telegram()
    app.state.telegram = telegram
    headers = {"Authorization": "Bearer owner-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sales", headers=headers
    ) as client:
        account = uuid4()
        with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as c:
            c.execute(
                "INSERT INTO gsales.telegram_accounts(id,tenant_id,display_name,telegram_user_id,session_secret_ref,connection_status,is_enabled) VALUES(%s,%s,'synthetic',%s,%s,'authorized',true)",
                (account, GSALES_TENANT_ID, int(uuid4().hex[:12], 16), uuid4()),
            )
        seller = await client.post(
            "/v1/sellers",
            json={
                "display_name": "G Sales RU",
                "personality": {"purpose": "Disclose AI, use verified facts only"},
                "telegram_account_id": str(account),
            },
        )
        assert seller.status_code == 200, seller.text
        sid = seller.json()["id"]
        uid = int(uuid4().hex[:12], 16)
        lead = await client.post(
            "/v1/leads",
            json={
                "seller_id": sid,
                "telegram_user_id": uid,
                "telegram_username": "synthetic_recipient",
                "display_name": "Synthetic",
                "source_type": "manual",
                "source_reference": "approved E2E fixture",
            },
        )
        assert lead.status_code == 200, lead.text
        lid = lead.json()["id"]
        assert (
            await client.put(
                f"/v1/leads/{lid}/eligibility",
                json={"allowed": True, "evidence": "synthetic approved recipient"},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/resume", json={"scope": "seller", "target_id": sid, "auto_reply": True}
            )
        ).status_code == 200
        assert (await client.post("/v1/resume", json={"scope": "global"})).status_code == 200
        first = await client.post(
            f"/v1/leads/{lid}/drafts",
            json={
                "request_id": uuid4().hex,
                "text": "Я AI-помощник GPoint. Можно рассказать? /stop для отказа.",
            },
        )
        assert first.status_code == 200, first.text
        aid = first.json()["id"]
        denied = await client.post(f"/v1/attempts/{aid}/send")
        assert denied.status_code == 409 and not telegram.messages
        pending = (await client.get("/v1/pending")).json()
        binding = next(r["approval_binding"] for r in pending if r["id"] == aid)
        assert (
            await client.post(f"/v1/attempts/{aid}/approve", json={"expected_binding": binding})
        ).status_code == 200
        assert (await client.post(f"/v1/attempts/{aid}/send")).json()["status"] == "sent"
        router = EventRouter(app.state.db, app.state.core, telegram)
        async with httpx.AsyncClient(
            base_url=core_server, headers={"Authorization": "Bearer disposable-e2e-core"}
        ) as diagnostic:
            consent_state = await diagnostic.get(f"/internal/v1/users/{lid}/chat-state")
            assert consent_state.status_code == 200, consent_state.text
            disclosure_version = consent_state.json()["disclosureVersion"]
        assert (await router.handle(account, uid, 101, f"/gsales_consent 18+ history disclosure={disclosure_version}"))[
            "status"
        ] == "explicit_consent_received"
        assert (
            await client.post(
                "/v1/resume", json={"scope": "dialog", "target_id": lid, "auto_reply": True}
            )
        ).status_code == 200
        result = await router.handle(account, uid, 102, "Расскажи о платформе")
        if result != {"status": "core_pending"}:
            async with httpx.AsyncClient(
                base_url=core_server, headers={"Authorization": "Bearer disposable-e2e-core"}
            ) as diagnostic:
                probe = await diagnostic.post(
                    "/internal/v1/respond",
                    json={
                        "request_id": f"gsales:{sid}:{lid}:102",
                        "gpoint_user_id": lid,
                        "gpoint_character_id": sid,
                        "conversation_id": f"gsales-chat:{lid}:{sid}",
                        "message_id": f"gsales:{sid}:{lid}:102",
                        "text": "Расскажи о платформе",
                        "locale": "ru",
                    },
                )
                pytest.fail(f"Core response gate: {probe.status_code} {probe.text}")
        assert result == {"status": "core_pending"}
        async with app.state.db.transaction() as c:
            assert (
                await (
                    await c.execute(
                        "SELECT pending_inbound_message_id FROM gsales.dialog_controls WHERE lead_id=%s",
                        (lid,),
                    )
                ).fetchone()
            )["pending_inbound_message_id"] == 102
        # The first worker process claims and exits; a distinct process later
        # completes its persistent lease, demonstrating restart recovery.
        claimed = await asyncio.to_thread(worker_rpc, core_server, "claim")
        job = claimed["job"]
        assert job and job["request"]["user_message"] == "Расскажи о платформе"
        await asyncio.to_thread(
            worker_rpc,
            core_server,
            "complete",
            {
                "job_id": job["id"],
                "lease_token": job["leaseToken"],
                "text": "CONFIRMED-SALES-REPLY",
                "model": "codex-default",
            },
        )
        async with httpx.AsyncClient(
            base_url=core_server, headers={"Authorization": "Bearer disposable-e2e-core"}
        ) as core:
            assert (await core.post("/test/worker-cycle")).status_code == 200
            # New Sales router instance represents a listener process restart.
            restarted = EventRouter(app.state.db, app.state.core, telegram)
            await restarted.drain_pending()
            await restarted.drain_pending()
            assert len(telegram.messages) == 2
            async with app.state.db.transaction() as c:
                replies = await (
                    await c.execute(
                        "SELECT delivery_status,history_confirmed_at FROM gsales.outreach_attempts WHERE lead_id=%s AND attempt_type='reply'",
                        (lid,),
                    )
                ).fetchall()
                assert (
                    len(replies) == 1
                    and replies[0]["delivery_status"] == "sent"
                    and replies[0]["history_confirmed_at"]
                )
            # Generate another draft, then pause before dispatch. Its text must
            # never enter the context used for the next actual inference.
            assert (await restarted.handle(account, uid, 103, "Второй вопрос"))[
                "status"
            ] == "core_pending"
            second = (await asyncio.to_thread(worker_rpc, core_server, "claim"))["job"]
            await asyncio.to_thread(
                worker_rpc,
                core_server,
                "complete",
                {
                    "job_id": second["id"],
                    "lease_token": second["leaseToken"],
                    "text": "UNDELIVERED-PRIVATE-DRAFT",
                    "model": "codex-default",
                },
            )
            await core.post("/test/worker-cycle")
            await client.post("/v1/pause", params={"lead_id": lid})
            await restarted.drain_pending()
            assert len(telegram.messages) == 2
            await client.post(
                "/v1/resume", json={"scope": "dialog", "target_id": lid, "auto_reply": True}
            )
            assert (await restarted.handle(account, uid, 104, "Третий вопрос"))[
                "status"
            ] == "core_pending"
            third = (await asyncio.to_thread(worker_rpc, core_server, "claim"))["job"]
            encoded = json.dumps(third["request"], ensure_ascii=False)
            assert "UNDELIVERED-PRIVATE-DRAFT" not in encoded
            assert "CONFIRMED-SALES-REPLY" in encoded
            await asyncio.to_thread(
                worker_rpc,
                core_server,
                "complete",
                {
                    "job_id": third["id"],
                    "lease_token": third["leaseToken"],
                    "text": "TIMEOUT-REPLY",
                    "model": "codex-default",
                },
            )
            await core.post("/test/worker-cycle")
            telegram.timeout = True
            await restarted.drain_pending()
            assert len(telegram.messages) == 3
            again = EventRouter(app.state.db, app.state.core, telegram)
            await again.drain_pending()
            assert len(telegram.messages) == 3
            async with app.state.db.transaction() as c:
                row = await (
                    await c.execute(
                        "SELECT id,delivery_status,history_confirmed_at FROM gsales.outreach_attempts WHERE lead_id=%s AND inbound_message_id=104",
                        (lid,),
                    )
                ).fetchone()
                assert row["delivery_status"] == "unknown" and row["history_confirmed_at"] is None
                uncertain_id = row["id"]
            assert (await client.post(f"/v1/attempts/{uncertain_id}/send")).status_code == 409
            assert len(telegram.messages) == 3
