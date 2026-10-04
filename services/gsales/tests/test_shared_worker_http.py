"""Shared worker → Sales gateway → real isolated Core HTTP/PostgreSQL.

The GPOINT gateway HTTP contract is bridged to the second disposable Core;
installed LaunchAgent and production gateway are never called.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from test_http_e2e import core_runtime

from app.config import Settings
from app.main import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agata/worker"))
import gpoint_mac_worker as worker

pytestmark = pytest.mark.skipif(not os.getenv("GSALES_TEST_ADMIN_DSN"), reason="disposable PostgreSQL required")


def test_two_targets_route_through_shared_worker_without_context_mix(tmp_path, monkeypatch):
    real_codex = os.getenv("GSALES_QA_REAL_CODEX") == "true"
    original_inference = worker.run_inference
    outputs = {}
    point_tenant_id = str(uuid4())
    sales_tenant_id = Settings().tenant_id
    with core_runtime(tmp_path / "sales") as sales_url, core_runtime(tmp_path / "point", "gpoint", point_tenant_id) as point_url:
        monkeypatch.setenv("GPOINT_AGATA_WORKER_URL", "https://gpoint.amiora.one/api/agata-worker")
        monkeypatch.setenv("GSALES_AGATA_WORKER_URL", "https://sales.test/agata-worker")
        monkeypatch.setenv("GPOINT_AGATA_WORKER_TOKEN", "point-worker-test")
        monkeypatch.setenv("GSALES_AGATA_WORKER_TOKEN", "sales-worker-test")
        monkeypatch.setenv("GPOINT_AGATA_TENANT_ID", point_tenant_id)
        monkeypatch.setenv("GSALES_AGATA_TENANT_ID", sales_tenant_id)
        monkeypatch.setattr(worker, "SUPPORT", Path.home() / "Library/Application Support/GPoint/agata-codex-worker" if real_codex else tmp_path / "worker")
        monkeypatch.setattr(worker, "STATUS", tmp_path / "worker/status.json")
        worker._last_heartbeat_attempt = float("-inf")
        worker._target_heartbeats.clear()
        sales = create_app(Settings(database_url=os.environ["GSALES_TEST_RUNTIME_DSN"], owner_api_token="owner-test", owner_telegram_id=42, agata_url=sales_url, agata_api_token="disposable-e2e-core", worker_token="sales-worker-test"))
        original_client = httpx.AsyncClient
        observed = []

        async def gateway(request):
            target = "GSALES" if request.url.host == "sales.test" else "GPOINT"
            assert request.headers["authorization"] == "Bearer " + ("sales-worker-test" if target == "GSALES" else "point-worker-test")
            observed.append((target, request.url.path))
            if target == "GSALES":
                async with original_client(transport=httpx.ASGITransport(app=sales), base_url="http://sales") as c:
                    return await c.post(request.url.path, content=request.content, headers=dict(request.headers))
            action = request.url.path.rsplit("/", 1)[-1]
            async with original_client(base_url=point_url, headers={"Authorization": "Bearer disposable-point-core"}) as c:
                if action == "health":
                    return await c.get("/health/ready")
                return await c.post("/internal/v1/worker/" + action, content=request.content, headers={"Content-Type": "application/json"})

        def worker_client(**kwargs):
            if "base_url" in kwargs:
                return original_client(**kwargs)
            return original_client(transport=httpx.MockTransport(gateway))

        account = uuid4()
        with psycopg.connect(os.environ["GSALES_TEST_ADMIN_DSN"]) as c:
            c.execute("INSERT INTO gsales.telegram_accounts(id,tenant_id,display_name,telegram_user_id,session_secret_ref,connection_status,is_enabled) VALUES(%s,%s,'synthetic',%s,%s,'authorized',true)", (account, worker.configured_tenant_bindings()["GSALES"], int(uuid4().hex[:12], 16), uuid4()))
        facts = [{"text": "GPoint — платформа для моделей и креаторов", "source": "synthetic owner-verified product fixture", "verified_at": "2026-10-04T06:00:00Z"}]
        request_id = uuid4().hex
        async def prepare_sales():
            async with original_client(transport=httpx.ASGITransport(app=sales), base_url="http://sales", headers={"Authorization": "Bearer owner-test"}) as c:
                created = await c.post("/v1/sellers", json={"display_name": "G Sales RU", "personality": {"purpose": "Вежливый, естественный русскоязычный AI-представитель GPoint. Помогать разобраться в платформе и рассмотреть регистрацию; не обещать доход, комиссии или неподтверждённые условия."}, "telegram_account_id": str(account)})
                assert created.status_code == 200, created.text
                seller = created.json()["id"]
                lead = await c.post("/v1/leads", json={"seller_id": seller, "telegram_user_id": int(uuid4().hex[:12], 16), "telegram_username": "synthetic_lead", "display_name": "Synthetic lead", "source_type": "manual", "source_reference": "owner-approved QA fixture"})
                assert lead.status_code == 200, lead.text
                lid = lead.json()["id"]
                queued = await c.post(f"/v1/leads/{lid}/drafts", json={"request_id": request_id, "verified_facts": facts})
                assert queued.status_code == 409 and "agata_draft_pending" in queued.text, queued.text
                return seller, lid
        seller, lid = asyncio.run(prepare_sales())
        draft = {"request_id": request_id, "character_external_id": seller, "verified_facts": facts}

        with httpx.Client(base_url=point_url, headers={"Authorization": "Bearer disposable-point-core"}) as c:
            user, character, mid = [uuid4().hex for _ in range(3)]
            assert c.put("/internal/v1/characters/" + character, json={"name": "GPoint QA Anna", "personality": {"traits": ["GPOINT-CONTEXT-ONLY"]}}).status_code == 200
            consent = {"chat_history": True, "long_term_memory": False, "age_band": "adult", "age_status": "self_attested", "age_method": "self_attested", "disclosure_receipt_id": "synthetic-qa", "jurisdiction": "GLOBAL", "disclosure_accepted": True}
            consent_response = c.put("/internal/v1/users/" + user + "/consent", json=consent)
            assert consent_response.status_code == 200, consent_response.text
            assert c.post("/internal/v1/users/" + user + "/age-assurance/self-attest", json={"confirmed_18_plus": True}).status_code == 200
            response_body = {"request_id": mid, "gpoint_user_id": user, "gpoint_character_id": character, "conversation_id": f"gpoint-chat:{user}:{character}", "message_id": mid, "text": "GPOINT-CONTEXT-ONLY: привет", "locale": "ru"}
            assert c.post("/internal/v1/respond", json=response_body).json()["status"] == "pending"

        seen = []
        def inference(job):
            seen.append(job)
            assert job["tenant_id"] == worker.configured_tenant_bindings()[job["target"]]
            if job["target"] == "GSALES":
                assert not job["request"]["history"] and not job["request"]["memories"]
                assert "GPOINT-CONTEXT-ONLY" not in str(job["request"])
                outputs["GSALES"] = original_inference(job) if real_codex else "Я AI-помощник GPoint. Можно рассказать о платформе?"
                return outputs["GSALES"]
            assert "GPOINT-CONTEXT-ONLY" in str(job["request"])
            outputs["GPOINT"] = original_inference(job) if real_codex else "Привет! Я Анна."
            return outputs["GPOINT"]

        monkeypatch.setattr(worker.httpx, "AsyncClient", worker_client)
        monkeypatch.setattr(worker, "run_inference", inference)
        scheduler = worker.TargetScheduler()
        scheduler.cursor = 1  # Generate Sales first, then prove GPOINT still responds.
        lock = worker.acquire_worker_lock()
        try:
            assert scheduler.poll() is True
            assert scheduler.poll() is True
        finally:
            lock.close()
        assert [job["target"] for job in seen] == ["GSALES", "GPOINT"]
        sales_job, point_job = seen
        # Cross-tenant job IDs and wrong leases cannot acknowledge a job.
        async def negatives():
            async with original_client(transport=httpx.ASGITransport(app=sales), base_url="http://sales", headers={"Authorization": "Bearer sales-worker-test"}) as c:
                payload = {"job_id": point_job["id"], "lease_token": point_job["leaseToken"], "text": "foreign", "model": "codex-default"}
                assert (await c.post("/agata-worker/complete", json=payload)).status_code == 404
                payload.update(job_id=sales_job["id"], lease_token="0"*32)
                assert (await c.post("/agata-worker/complete", json=payload)).status_code == 409
                payload.update(lease_token=sales_job["leaseToken"])
                assert (await c.post("/agata-worker/complete", json=payload)).json()["duplicate"] is True
        asyncio.run(negatives())
        with httpx.Client(base_url=sales_url, headers={"Authorization": "Bearer disposable-e2e-core"}) as c:
            assert c.post("/internal/v1/sales/drafts", json=draft).json()["text"] == outputs["GSALES"]
        with httpx.Client(base_url=point_url, headers={"Authorization": "Bearer disposable-point-core"}) as c:
            assert c.post("/test/worker-cycle").status_code == 200
            result = c.post("/internal/v1/respond", json=response_body)
            assert result.json()["text"] == outputs["GPOINT"], result.text
        assert any(t == "GSALES" and p.endswith("/complete") for t, p in observed)

        async def owner_review():
            async with original_client(transport=httpx.ASGITransport(app=sales), base_url="http://sales", headers={"Authorization": "Bearer owner-test"}) as c:
                result = await c.post(f"/v1/leads/{lid}/drafts", json={"request_id": request_id, "verified_facts": facts})
                assert result.status_code == 200, result.text
                attempt = result.json()["id"]
                pending = (await c.get("/v1/pending")).json()
                chosen = next(row for row in pending if row["id"] == attempt)
                assert chosen["text"] == outputs["GSALES"]
                assert (await c.post(f"/v1/attempts/{attempt}/send")).status_code == 409
                return {"seller": seller, "lead": lid, "attempt": attempt, "text": chosen["text"], "approval_status": result.json()["approval_status"]}
        evidence = asyncio.run(owner_review())
        assert evidence["approval_status"] == "pending"
        if real_codex:
            out = Path(__file__).parents[1] / "test-evidence/real-codex.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps({"provider": "existing Codex Local", "production_touched": False, "telegram_sent": False, "outputs": outputs, "pending_owner_review": evidence}, ensure_ascii=False, indent=2) + "\n")
