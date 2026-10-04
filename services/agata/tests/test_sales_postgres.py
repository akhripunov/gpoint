"""Shared Core HTTP under a dedicated real GSALES login; only local fake providers."""

import base64
import os
from uuid import uuid4

import httpx
import psycopg
import pytest

from app.api.internal import get_embedding_provider, get_response_provider
from app.config import get_settings
from app.db.base import dispose_engine
from app.main import app
from app.memory.retrieval import DeterministicFakeEmbeddingProvider
from app.responses.pipeline import DeterministicFakeResponseProvider

pytestmark = pytest.mark.skipif(
    not os.getenv("AGATA_GSALES_TEST_DATABASE_URL"), reason="GSALES disposable runner required"
)
TENANT = "c4d67889-877e-4d1b-a8d8-597c479d638f"


@pytest.mark.asyncio
async def test_sales_runtime_http_persona_drafts_response_and_ownership(monkeypatch):
    monkeypatch.setenv("AGATA_DATABASE_URL", os.environ["AGATA_GSALES_TEST_DATABASE_URL"])
    monkeypatch.setenv("AGATA_TENANT_ID", TENANT)
    monkeypatch.setenv("AGATA_SOURCE_SYSTEM", "gsales")
    monkeypatch.setenv("AGATA_INTERNAL_API_TOKEN", "disposable-sales-test")
    monkeypatch.setenv("AGATA_LLM_PROVIDER", "local_fake")
    monkeypatch.setenv("AGATA_LLM_MODEL", "deterministic-response-v1")
    monkeypatch.setenv("AGATA_LLM_API_KEY", "disposable-local-fake")
    monkeypatch.setenv("AGATA_SALES_DRAFTS_ENABLED", "true")
    for suffix in (
        "MESSAGE_AEAD_KEY",
        "REQUEST_FINGERPRINT_HMAC_KEY",
        "RETRIEVAL_QUERY_HMAC_KEY",
        "SHADOW_CANDIDATE_HMAC_KEY",
        "RESPONSE_FINGERPRINT_HMAC_KEY",
    ):
        monkeypatch.setenv("AGATA_" + suffix, base64.b64encode(b"a" * 32).decode())
    with psycopg.connect(os.environ["AGATA_GSALES_TEST_ADMIN_DSN"]) as conn:
        conn.execute(
            """INSERT INTO agata.regional_policy_profiles(tenant_id,jurisdiction,market,version,status,effective_from,rules)
            VALUES (%s,'GLOBAL','GSALES',1,'active',now(),'{"ai_disclosure_required":true,"self_attestation_allowed":true,"adult_only_features":[]}') ON CONFLICT DO NOTHING""",
            (TENANT,),
        )
    await dispose_engine()
    get_settings.cache_clear()
    app.dependency_overrides[get_response_provider] = DeterministicFakeResponseProvider
    app.dependency_overrides[get_embedding_provider] = DeterministicFakeEmbeddingProvider
    headers = {"Authorization": "Bearer disposable-sales-test"}
    seller = "sales-seller-" + uuid4().hex
    lead = "sales-lead-" + uuid4().hex
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local-core", headers=headers
        ) as client:
            sec = (await client.get("/internal/v1/sales/runtime-security")).json()
            assert sec == {"passed": True, "tenant_id": TENANT, "source_system": "gsales", "production_inference_ready": False}
            profile = await client.put(
                f"/internal/v1/characters/{seller}",
                json={
                    "name": "G Sales RU",
                    "personality": {
                        "purpose": "Sales with exact owner approval; disclose AI; no promises"
                    },
                },
            )
            assert profile.status_code == 200, profile.text
            char_id = profile.json()["character_id"]
            body = {
                "request_id": uuid4().hex,
                "character_external_id": seller,
                "text": "Я AI-помощник GPoint. Можно рассказать о платформе? Если нет, больше не напишу.",
            }
            draft = await client.post("/internal/v1/sales/drafts", json=body)
            assert draft.status_code == 200, draft.text
            assert (
                await client.post("/internal/v1/sales/drafts", json=body)
            ).json() == draft.json()
            assert (
                await client.get(
                    "/internal/v1/sales/drafts/" + draft.json()["message_reference"],
                    params={"character_external_id": "foreign-seller"},
                )
            ).status_code == 404
            assert (
                await client.post(
                    "/internal/v1/sales/drafts", json={**body, "tenant_id": "foreign"}
                )
            ).status_code == 422
            generated = await client.post(
                "/internal/v1/sales/drafts",
                json={
                    "request_id": uuid4().hex,
                    "character_external_id": seller,
                    "verified_facts": [
                        {
                            "text": "GPoint is a creator platform",
                            "source": "owner test fixture",
                            "verified_at": "2026-10-03T12:00:00Z",
                        }
                    ],
                },
            )
            assert generated.status_code == 200, generated.text
            consent = await client.put(
                f"/internal/v1/users/{lead}/consent",
                json={
                    "chat_history": True,
                    "long_term_memory": False,
                    "age_band": "adult",
                    "age_status": "verified",
                    "age_method": "test-fixture-only",
                    "verified_at": "2026-10-03T12:00:00Z",
                    "jurisdiction": "GLOBAL",
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": uuid4().hex,
                },
            )
            assert consent.status_code == 200, consent.text
            message = {
                "request_id": uuid4().hex,
                "gpoint_user_id": lead,
                "gpoint_character_id": seller,
                "conversation_id": f"gsales-chat:{lead}:{seller}",
                "message_id": uuid4().hex,
                "text": "Расскажите о платформе",
                "locale": "ru",
            }
            response = await client.post("/internal/v1/respond", json=message)
            assert response.status_code == 200, response.text
            assert response.json()["character_id"] == char_id
            assert response.json()["relationship_id"]
            assert (
                await client.post("/internal/v1/respond", json=message)
            ).json() == response.json()
            bad = {**message, "message_id": uuid4().hex, "gpoint_character_id": "foreign-character"}
            assert (await client.post("/internal/v1/respond", json=bad)).status_code == 404
            assert (
                await client.post("/internal/v1/respond", json={**message, "tenant_id": "foreign"})
            ).status_code == 422
            seller2 = "sales-other-" + uuid4().hex
            assert (
                await client.put(
                    f"/internal/v1/characters/{seller2}", json={"name": "Independent seller"}
                )
            ).status_code == 200
            second = {
                **message,
                "request_id": uuid4().hex,
                "message_id": uuid4().hex,
                "gpoint_character_id": seller2,
                "conversation_id": f"gsales-chat:{lead}:{seller2}",
                "text": "Second seller private message",
            }
            second_response = await client.post("/internal/v1/respond", json=second)
            assert second_response.status_code == 200, second_response.text
            assert second_response.json()["relationship_id"] != response.json()["relationship_id"]
            history = await client.get(f"/internal/v1/users/{lead}/characters/{seller2}/messages")
            assert history.status_code == 200, history.text
            assert "Расскажите о платформе" not in history.text
            assert "Second seller private message" in history.text
            # Generated replies stay draft and out of history until a trusted
            # channel acknowledgement confirms the exact stored text.
            reply_draft = await client.post(
                "/internal/v1/sales/drafts",
                json={
                    "request_id": uuid4().hex,
                    "character_external_id": seller2,
                    "text": second_response.json()["text"],
                },
            )
            delivery = {
                "user_external_id": lead,
                "character_external_id": seller2,
                "draft_reference": reply_draft.json()["message_reference"],
                "provider_message_id": 456,
                "source_message_key": second["message_id"],
                "occurred_at": "2026-10-04T12:00:00Z",
            }
            ack = await client.post("/internal/v1/sales/deliveries", json=delivery)
            assert ack.status_code == 200, ack.text
            assert ack.json()["status"] == "confirmed"
            assert (
                await client.post("/internal/v1/sales/deliveries", json=delivery)
            ).json() == ack.json()
            first_ack = await client.post(
                "/internal/v1/sales/deliveries",
                json={
                    "user_external_id": lead,
                    "character_external_id": seller,
                    "draft_reference": draft.json()["message_reference"],
                    "provider_message_id": 123,
                    "occurred_at": "2026-10-04T11:00:00Z",
                },
            )
            assert first_ack.status_code == 200, first_ack.text
            first_history = await client.get(
                f"/internal/v1/users/{lead}/characters/{seller}/messages"
            )
            assert "Можно рассказать о платформе?" in first_history.text
            pending = await client.get(
                "/internal/v1/sales/replies",
                params={
                    "user_external_id": lead,
                    "character_external_id": seller2,
                    "source_message_key": second["message_id"],
                },
            )
            assert pending.status_code == 200, pending.text
            assert pending.json()["text"] == second_response.json()["text"]
            # Cold draft uses a typed, encrypted worker job anchored to the
            # seller, never a synthetic recipient Message or consent record.
            monkeypatch.setenv("AGATA_LLM_PROVIDER", "mac_worker")
            monkeypatch.setenv("AGATA_LLM_MODEL", "codex-default")
            get_settings.cache_clear()
            assert (
                await client.put(f"/internal/v1/characters/{seller}", json={"name": "G Sales RU"})
            ).status_code == 200
            request = {
                "request_id": uuid4().hex,
                "character_external_id": seller,
                "verified_facts": [
                    {
                        "text": "Owner verified platform fact",
                        "source": "test fixture",
                        "verified_at": "2026-10-03T12:00:00Z",
                    }
                ],
            }
            queued = await client.post("/internal/v1/sales/drafts", json=request)
            assert queued.status_code == 202, queued.text
            job = (await client.post("/internal/v1/worker/claim")).json()["job"]
            assert job and not job["request"]["history"] and not job["request"]["memories"]
            complete = await client.post(
                "/internal/v1/worker/complete",
                json={
                    "job_id": job["id"],
                    "lease_token": job["leaseToken"],
                    "text": "Я AI-помощник GPoint. Можно рассказать о платформе?",
                    "model": "codex-default",
                },
            )
            assert complete.status_code == 200, complete.text
            ready = await client.post("/internal/v1/sales/drafts", json=request)
            assert ready.status_code == 200, ready.text
            assert ready.json()["text"].startswith("Я AI-помощник")
    finally:
        app.dependency_overrides.clear()
        await dispose_engine()
        get_settings.cache_clear()
