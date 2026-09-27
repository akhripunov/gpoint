"""Internal HTTP -> encrypted message -> deterministic response -> outbox smoke."""

import base64
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.api.internal import get_embedding_provider, get_response_provider
from app.config import get_settings
from app.db.base import SessionFactory, dispose_engine, set_tenant_context
from app.db.models import CharacterProfileVersion, Message, OutboxJob, User
from app.main import app
from app.memory.retrieval import DeterministicFakeEmbeddingProvider
from app.responses.pipeline import DeterministicFakeResponseProvider

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")


@pytest.mark.asyncio
async def test_internal_api_is_channel_neutral_and_respond_is_idempotent(monkeypatch):
    assert TEST_DSN is not None
    tenant_id = "7508f36f-774a-48aa-8c6b-42f6f160415d"
    key = base64.b64encode(b"a" * 32).decode()
    monkeypatch.setenv("AGATA_DATABASE_URL", TEST_DSN.replace("+psycopg", "+asyncpg"))
    monkeypatch.setenv("AGATA_TENANT_ID", tenant_id)
    monkeypatch.setenv("AGATA_INTERNAL_API_TOKEN", "disposable-internal-test-token")
    monkeypatch.setenv("AGATA_MESSAGE_AEAD_KEY", key)
    monkeypatch.setenv("AGATA_REQUEST_FINGERPRINT_HMAC_KEY", key)
    monkeypatch.setenv("AGATA_RETRIEVAL_QUERY_HMAC_KEY", key)
    monkeypatch.setenv("AGATA_RESPONSE_FINGERPRINT_HMAC_KEY", key)
    monkeypatch.setenv("AGATA_SHADOW_CANDIDATE_HMAC_KEY", key)
    monkeypatch.setenv("AGATA_EMBEDDING_API_KEY", "disposable-embedding-test-key")
    monkeypatch.setenv("AGATA_LLM_API_KEY", "disposable-response-test-key")
    monkeypatch.setenv("AGATA_LLM_PROVIDER", "local_fake")
    monkeypatch.setenv("AGATA_LLM_MODEL", "deterministic-response-v1")
    get_settings.cache_clear()
    app.dependency_overrides[get_response_provider] = DeterministicFakeResponseProvider
    app.dependency_overrides[get_embedding_provider] = DeterministicFakeEmbeddingProvider

    user_key, character_key, message_key = (f"test-{uuid4()}" for _ in range(3))
    conversation_key = f"gpoint-chat:{user_key}:{character_key}"
    now = datetime.now(UTC).isoformat()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://agata-test") as client:
            headers = {"authorization": "Bearer disposable-internal-test-token"}
            health = await client.get("/health/live")
            assert health.status_code == 200
            ready = await client.get("/health/ready")
            assert ready.status_code == 200, ready.text
            unauthorized = await client.put(
                f"/internal/v1/characters/{character_key}",
                json={"name": "Must not be written"},
            )
            wrong_token = await client.put(
                f"/internal/v1/characters/{character_key}",
                headers={"authorization": "Bearer wrong-token"},
                json={"name": "Must not be written"},
            )
            assert unauthorized.status_code == wrong_token.status_code == 401
            initial_state = await client.get(
                f"/internal/v1/users/{user_key}/chat-state", headers=headers
            )
            assert initial_state.status_code == 200, initial_state.text
            assert initial_state.json()["ageAssuranceRequired"] is True
            profile = await client.put(
                f"/internal/v1/characters/{character_key}",
                headers=headers,
                json={"name": "Anna", "personality": {"identity_core": {"gender": "female"}}},
            )
            assert profile.status_code == 200, profile.text
            character_id = profile.json()["character_id"]
            profile_update = await client.put(
                f"/internal/v1/characters/{character_key}",
                headers=headers,
                json={
                    "name": "Anna",
                    "personality": {"identity_core": {"gender": "female"}, "traits": ["curious"]},
                    "speech_style": {"warmth": "high"},
                },
            )
            assert profile_update.status_code == 200, profile_update.text
            assert profile_update.json()["profile_version"] == 2
            consent = await client.put(
                f"/internal/v1/users/{user_key}/consent",
                headers=headers,
                json={
                    "chat_history": True,
                    "long_term_memory": True,
                    "age_band": "adult",
                    "age_status": "verified",
                    "age_method": "provider_verified",
                    "jurisdiction": "US",
                    "verified_at": now,
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": str(uuid4()),
                },
            )
            assert consent.status_code == 200, consent.text
            body = {
                "request_id": str(uuid4()),
                "gpoint_user_id": user_key,
                "gpoint_character_id": character_key,
                "conversation_id": conversation_key,
                "message_id": message_key,
                "text": "Привет",
                "locale": "ru-RU",
            }
            first = await client.post("/internal/v1/respond", headers=headers, json=body)
            second = await client.post("/internal/v1/respond", headers=headers, json=body)
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            assert first.json()["message_id"] == second.json()["message_id"]
            assert first.json()["conversation_id"] == second.json()["conversation_id"]
            state = await client.get(
                f"/internal/v1/users/{user_key}/chat-state", headers=headers
            )
            assert state.status_code == 200, state.text
            assert state.json()["chatHistory"] is True
            assert state.json()["longTermMemory"] is True
            assert state.json()["ageAssuranceRequired"] is False
            conversations = await client.get(
                f"/internal/v1/users/{user_key}/conversations", headers=headers
            )
            assert conversations.status_code == 200, conversations.text
            assert len(conversations.json()["items"]) == 1
            assert conversations.json()["items"][0]["gpointCharacterId"] == character_key
            assert conversations.json()["items"][0]["externalConversationId"] == conversation_key
            history_path = f"/internal/v1/users/{user_key}/characters/{character_key}/messages"
            history = await client.get(history_path, headers=headers)
            assert history.status_code == 200, history.text
            history_items = history.json()["items"]
            assert {message["role"] for message in history_items} == {"user", "assistant"}
            assert next(message["text"] for message in history_items if message["role"] == "user") == "Привет"
            assert next(message["text"] for message in history_items if message["role"] == "assistant") == first.json()["text"]
            wrong_user = await client.get(
                f"/internal/v1/users/{user_key}-other/characters/{character_key}/messages",
                headers=headers,
            )
            wrong_character = await client.get(
                f"/internal/v1/users/{user_key}/characters/{character_key}-other/messages",
                headers=headers,
            )
            assert wrong_user.status_code == wrong_character.status_code == 200
            assert wrong_user.json()["items"] == wrong_character.json()["items"] == []
            no_auth = await client.get(history_path)
            assert no_auth.status_code == 401
            bounded = await client.get(f"{history_path}?limit=1", headers=headers)
            assert bounded.status_code == 200, bounded.text
            assert len(bounded.json()["items"]) == 1
            assert bounded.json()["nextCursor"]
            metrics = await client.get("/health/metrics")
            assert metrics.status_code == 200, metrics.text
            assert set(metrics.json()) == {
                "status", "messages", "active_memories", "recent_failed_responses"
            }
            assert all(isinstance(value, int) for key, value in metrics.json().items() if key != "status")

        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, tenant_id)
                user = await session.scalar(
                    select(User).where(User.external_id == user_key, User.source_system == "gpoint")
                )
                assert user is not None
                profiles = list(
                    (
                        await session.scalars(
                            select(CharacterProfileVersion)
                            .where(
                                CharacterProfileVersion.tenant_id == tenant_id,
                                CharacterProfileVersion.character_id == character_id,
                            )
                            .order_by(CharacterProfileVersion.version)
                        )
                    ).all()
                )
                assert [item.version for item in profiles] == [1, 2]
                assert profiles[0].speech_style == {}
                assert profiles[1].speech_style == {"warmth": "high"}
                message_count = await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.tenant_id == tenant_id,
                        Message.user_id == user.id,
                        Message.conversation_id == first.json()["conversation_id"],
                    )
                )
                assert message_count == 2
                stored_messages = list(
                    (
                        await session.scalars(
                            select(Message).where(
                                Message.tenant_id == tenant_id,
                                Message.user_id == user.id,
                                Message.conversation_id == first.json()["conversation_id"],
                            )
                        )
                    ).all()
                )
                assert {item.role for item in stored_messages} == {"user", "assistant"}
                assert all(
                    item.content_ciphertext
                    and item.content_nonce
                    and item.content_key_version
                    and "Привет".encode() not in item.content_ciphertext
                    for item in stored_messages
                )
                user_message_id = await session.scalar(
                    select(Message.id).where(
                        Message.tenant_id == tenant_id,
                        Message.idempotency_key == message_key,
                    )
                )
                assert user_message_id is not None
                extraction_count = await session.scalar(
                    select(func.count())
                    .select_from(OutboxJob)
                    .where(
                        OutboxJob.tenant_id == tenant_id,
                        OutboxJob.job_type == "memory_extract",
                        OutboxJob.aggregate_id == user_message_id,
                    )
                )
                assert extraction_count == 1
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        await dispose_engine()
