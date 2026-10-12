"""Internal HTTP -> encrypted message -> deterministic response -> outbox smoke."""

import base64
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.api.internal import get_embedding_provider, get_response_provider
from app.config import get_settings
from app.db.base import SessionFactory, dispose_engine, set_tenant_context
from app.db.models import AIDisclosureReceipt, AgeAssurance, CharacterProfileVersion, Consent, Conversation, Message, OutboxJob, ResponseRun, User
from app.main import app
from app.memory.retrieval import DeterministicFakeEmbeddingProvider
from app.messages.crypto import AeadCrypto
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
            assert initial_state.json()["onboardingCompleted"] is False
            assert initial_state.json()["disclosureRequired"] is True
            assert initial_state.json()["disclosureVersion"]
            assert initial_state.json()["onboardingCompletedAt"] is None
            assert initial_state.json()["memoryOptIn"] is False
            age_path = f"/internal/v1/users/{user_key}/age-assurance"
            initial_age = await client.get(age_path, headers=headers)
            assert initial_age.status_code == 200
            assert initial_age.json()["required"] is True
            assert initial_age.json()["selfAttestationAllowed"] is True
            attested = await client.post(
                f"{age_path}/self-attest",
                headers=headers,
                json={"confirmed_18_plus": True},
            )
            assert attested.status_code == 200, attested.text
            assert attested.json() == {
                "eligible": True,
                "status": "self_attested",
                "method": "self_attested",
            }
            reopened_age = await client.get(age_path, headers=headers)
            assert reopened_age.json()["required"] is False
            assert reopened_age.json()["method"] == "self_attested"
            assert reopened_age.json()["assertedAt"]
            async with SessionFactory() as session:
                await set_tenant_context(session, tenant_id)
                persisted_user = await session.scalar(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.source_system == "gpoint",
                        User.external_id == user_key,
                    )
                )
                assert persisted_user is not None
                persisted_age = await session.scalar(
                    select(AgeAssurance).where(
                        AgeAssurance.tenant_id == tenant_id,
                        AgeAssurance.user_id == persisted_user.id,
                    )
                )
                assert persisted_age is not None
                assert persisted_age.status == "self_attested"
                assert persisted_age.method == "self_attested"
                assert persisted_age.asserted_at is not None
            other_user_state = await client.get(
                f"/internal/v1/users/{user_key}-other/age-assurance", headers=headers
            )
            assert other_user_state.status_code == 200
            assert other_user_state.json()["required"] is True

            profile = await client.put(
                f"/internal/v1/characters/{character_key}",
                headers=headers,
                json={"name": "Anna", "personality": {"identity_core": {"gender": "female"}}},
            )
            assert profile.status_code == 200, profile.text
            character_id = profile.json()["character_id"]

            no_age_user = f"test-no-age-{uuid4()}"
            no_age_consent = await client.put(
                f"/internal/v1/users/{no_age_user}/consent",
                headers=headers,
                json={
                    "chat_history": True,
                    "long_term_memory": False,
                    "age_band": "unknown",
                    "age_status": "pending",
                    "age_method": "unknown",
                    "jurisdiction": "GLOBAL",
                    "verified_at": None,
                    "expires_at": None,
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": str(uuid4()),
                },
            )
            assert no_age_consent.status_code == 200, no_age_consent.text
            no_age_state = await client.get(
                f"/internal/v1/users/{no_age_user}/chat-state", headers=headers
            )
            assert no_age_state.status_code == 200, no_age_state.text
            assert no_age_state.json()["onboardingCompleted"] is True
            assert no_age_state.json()["longTermMemory"] is False
            assert no_age_state.json()["memoryOptIn"] is False
            assert no_age_state.json()["memoryOptInAt"]
            async with SessionFactory() as session:
                await set_tenant_context(session, tenant_id)
                no_age_persisted_user = await session.scalar(select(User).where(
                    User.tenant_id == tenant_id, User.source_system == "gpoint",
                    User.external_id == no_age_user,
                ))
                assert no_age_persisted_user is not None
                no_age_receipts = list((await session.scalars(select(AIDisclosureReceipt).where(
                    AIDisclosureReceipt.tenant_id == tenant_id,
                    AIDisclosureReceipt.user_id == no_age_persisted_user.id,
                ))).all())
                assert len(no_age_receipts) == 1
            assert no_age_receipts[0].memory_opt_in is False
            assert no_age_receipts[0].memory_opt_in_at is not None
            repeated_opt_out = await client.put(
                f"/internal/v1/users/{no_age_user}/consent",
                headers=headers,
                json={
                    "chat_history": True,
                    "long_term_memory": False,
                    "age_band": "unknown",
                    "age_status": "pending",
                    "age_method": "unknown",
                    "jurisdiction": "GLOBAL",
                    "verified_at": None,
                    "expires_at": None,
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": str(uuid4()),
                },
            )
            assert repeated_opt_out.status_code == 200, repeated_opt_out.text
            async with SessionFactory() as session:
                await set_tenant_context(session, tenant_id)
                chat_history_consents = await session.scalar(select(func.count()).select_from(Consent).where(
                    Consent.tenant_id == tenant_id,
                    Consent.user_id == no_age_persisted_user.id,
                    Consent.purpose == "chat_history",
                    Consent.revoked_at.is_(None),
                ))
                all_no_age_receipts = await session.scalar(select(func.count()).select_from(AIDisclosureReceipt).where(
                    AIDisclosureReceipt.tenant_id == tenant_id,
                    AIDisclosureReceipt.user_id == no_age_persisted_user.id,
                ))
                assert chat_history_consents == 1
                assert all_no_age_receipts == 1
            bypass_attempt = await client.post(
                "/internal/v1/respond",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "gpoint_user_id": no_age_user,
                    "gpoint_character_id": character_key,
                    "conversation_id": f"gpoint-chat:{no_age_user}:{character_key}",
                    "message_id": f"no-age-{uuid4()}",
                    "text": "Must remain blocked",
                    "locale": "ru-RU",
                },
            )
            assert bypass_attempt.status_code == 403
            assert bypass_attempt.json()["detail"]["code"] == "age_assurance_required"
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
                    "age_status": "self_attested",
                    "age_method": "self_attested",
                    "jurisdiction": "GLOBAL",
                    "verified_at": None,
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
            assert state.json()["memoryOptIn"] is True
            assert state.json()["memoryOptInAt"]
            assert state.json()["onboardingCompleted"] is True
            assert state.json()["disclosureRequired"] is False
            assert state.json()["onboardingCompletedAt"]
            accepted_at = state.json()["onboardingCompletedAt"]
            assert state.json()["ageAssuranceRequired"] is False
            # Repeating the same accept request cannot create a second receipt
            # or reset its timestamp for this user and mandatory notice version.
            repeated_consent = await client.put(
                f"/internal/v1/users/{user_key}/consent",
                headers=headers,
                json={
                    "chat_history": True,
                    "long_term_memory": True,
                    "age_band": "adult",
                    "age_status": "self_attested",
                    "age_method": "self_attested",
                    "jurisdiction": "GLOBAL",
                    "verified_at": None,
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": str(uuid4()),
                },
            )
            assert repeated_consent.status_code == 200, repeated_consent.text
            async with SessionFactory() as session:
                await set_tenant_context(session, tenant_id)
                persisted_user = await session.scalar(select(User).where(
                    User.tenant_id == tenant_id, User.source_system == "gpoint",
                    User.external_id == user_key,
                ))
                assert persisted_user is not None
                receipts = list((await session.scalars(select(AIDisclosureReceipt).where(
                    AIDisclosureReceipt.tenant_id == tenant_id,
                    AIDisclosureReceipt.user_id == persisted_user.id,
                    AIDisclosureReceipt.policy_version == state.json()["disclosureVersion"],
                ))).all())
                assert len(receipts) == 1
                assert receipts[0].memory_opt_in is True
                assert receipts[0].memory_opt_in_at is not None
                # An elapsed reminder interval does not repeat an accepted
                # onboarding notice; only a new policy version does.
                receipts[0].displayed_at = receipts[0].displayed_at.replace(year=receipts[0].displayed_at.year - 1)
            reopened_state = await client.get(
                f"/internal/v1/users/{user_key}/chat-state", headers=headers
            )
            assert reopened_state.json()["onboardingCompleted"] is True
            assert reopened_state.json()["disclosureRequired"] is False
            assert reopened_state.json()["onboardingCompletedAt"] == accepted_at
            conversations = await client.get(
                f"/internal/v1/users/{user_key}/conversations", headers=headers
            )
            assert conversations.status_code == 200, conversations.text
            assert len(conversations.json()["items"]) == 1
            assert conversations.json()["items"][0]["gpointCharacterId"] == character_key
            assert conversations.json()["items"][0]["externalConversationId"] == conversation_key
            unread = await client.get(f"/internal/v1/users/{user_key}/unread", headers=headers)
            assert unread.status_code == 200, unread.text
            # Existing respond/replay assertions above prove duplicate ingestion;
            # both requests must still result in exactly one unread incoming row.
            assert unread.json()["items"] == [{"gpointCharacterId": character_key, "unreadCount": 1}]
            assert conversations.json()["items"][0]["unreadCount"] == 1
            history_path = f"/internal/v1/users/{user_key}/characters/{character_key}/messages"
            history = await client.get(history_path, headers=headers)
            assert history.status_code == 200, history.text
            history_items = history.json()["items"]
            assert {message["role"] for message in history_items} == {"user", "assistant"}
            assert [message["role"] for message in history_items] == ["user", "assistant"]
            assert history_items[0]["occurredAt"] < history_items[1]["occurredAt"]
            assert next(message["text"] for message in history_items if message["role"] == "user") == "Привет"
            assert next(message["text"] for message in history_items if message["role"] == "assistant") == first.json()["text"]
            persisted_user_message = next(message for message in history_items if message["role"] == "user")
            assert persisted_user_message["clientMessageId"] == message_key
            assert all("clientMessageId" not in message for message in history_items if message["role"] == "assistant")
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
            assert all(
                item.content_ciphertext
                and item.content_nonce
                and item.content_key_version
                and "Привет".encode() not in item.content_ciphertext
                for item in stored_messages
            )

        # Pending ResponseRuns are intentionally not persisted: the inference
        # outbox owns pending/running state; ResponseRun is committed atomically
        # with the final assistant message only.
        pending_message_key = f"pending-{uuid4()}"
        monkeypatch.setenv("AGATA_LLM_PROVIDER", "mac_worker")
        monkeypatch.setenv("AGATA_CODEX_LOCAL_ENABLED", "true")
        monkeypatch.setenv("AGATA_WORKER_WAIT_SECONDS", "0.5")
        get_settings.cache_clear()
        mac_character_key = f"test-mac-worker-{uuid4()}"
        async with httpx.AsyncClient(transport=transport, base_url="http://agata-test") as client:
            mac_profile = await client.put(
                f"/internal/v1/characters/{mac_character_key}",
                headers=headers,
                json={"name": "Anna Mac Worker", "personality": {"traits": ["test"]}},
            )
            assert mac_profile.status_code == 200, mac_profile.text
        app.dependency_overrides.pop(get_response_provider, None)
        pending_body = {
            "request_id": pending_message_key,
            "gpoint_user_id": user_key,
            "gpoint_character_id": mac_character_key,
            "conversation_id": f"gpoint-chat:{user_key}:{mac_character_key}",
            "message_id": pending_message_key,
            "text": "This message should enqueue one inference job",
            "locale": "en-US",
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://agata-test") as client:
            pending_first = await client.post(
                "/internal/v1/respond", headers=headers, json=pending_body
            )
            pending_retry = await client.post(
                "/internal/v1/respond", headers=headers, json=pending_body
            )
            assert pending_first.status_code == pending_retry.status_code == 200
            assert pending_first.json()["status"] == pending_retry.json()["status"] == "pending"
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, tenant_id)
                pending_user = await session.scalar(
                    select(Message).where(
                        Message.tenant_id == tenant_id,
                        Message.idempotency_key == pending_message_key,
                        Message.role == "user",
                    )
                )
                assert pending_user is not None
                pending_conversation = await session.get(Conversation, pending_user.conversation_id)
                assert pending_conversation is not None
                user_rows = await session.scalar(
                    select(func.count()).select_from(Message).where(
                        Message.tenant_id == tenant_id,
                        Message.idempotency_key == pending_message_key,
                        Message.role == "user",
                    )
                )
                inference_rows = await session.scalar(
                    select(func.count()).select_from(OutboxJob).where(
                        OutboxJob.tenant_id == tenant_id,
                        OutboxJob.job_type == "inference",
                        OutboxJob.dedupe_key == f"inference:{pending_user.id}",
                    )
                )
                response_runs = await session.scalar(
                    select(func.count()).select_from(ResponseRun).where(
                        ResponseRun.tenant_id == tenant_id,
                        ResponseRun.user_message_id == pending_user.id,
                    )
                )
                relationship_refresh_rows = await session.scalar(
                    select(func.count()).select_from(OutboxJob).where(
                        OutboxJob.tenant_id == tenant_id,
                        OutboxJob.job_type == "relationship_note_refresh",
                        OutboxJob.aggregate_id == pending_conversation.relationship_id,
                        OutboxJob.dedupe_key == f"relationship-note:{pending_conversation.relationship_id}:0",
                    )
                )
                assert user_rows == inference_rows == relationship_refresh_rows == 1
                assert response_runs == 0

        disabled_message_key = f"disabled-{uuid4()}"
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, tenant_id)
                inference_jobs_before = await session.scalar(
                    select(func.count()).select_from(OutboxJob).where(
                        OutboxJob.tenant_id == tenant_id, OutboxJob.job_type == "inference"
                    )
                )
        monkeypatch.setenv("AGATA_LLM_PROVIDER", "disabled")
        monkeypatch.delenv("AGATA_LLM_API_KEY", raising=False)
        get_settings.cache_clear()
        app.dependency_overrides[get_response_provider] = lambda: None
        async with httpx.AsyncClient(transport=transport, base_url="http://agata-test") as client:
            disabled_response = await client.post(
                "/internal/v1/respond",
                headers={"authorization": "Bearer disposable-internal-test-token"},
                json={
                    "request_id": str(uuid4()),
                    "gpoint_user_id": user_key,
                    "gpoint_character_id": character_key,
                    "conversation_id": conversation_key,
                    "message_id": disabled_message_key,
                    "text": "This message must persist without an AI reply",
                    "locale": "en-US",
                },
            )
            assert disabled_response.status_code == 503
            assert disabled_response.json()["detail"]["code"] == "ai_unavailable"
        async with SessionFactory() as session:
            async with session.begin():
                await set_tenant_context(session, tenant_id)
                persisted = await session.scalar(
                    select(Message).where(
                        Message.tenant_id == tenant_id,
                        Message.idempotency_key == disabled_message_key,
                        Message.role == "user",
                    )
                )
                assert persisted is not None and persisted.content_ciphertext
                inference_jobs_after = await session.scalar(
                    select(func.count()).select_from(OutboxJob).where(
                        OutboxJob.tenant_id == tenant_id, OutboxJob.job_type == "inference"
                    )
                )
                assert inference_jobs_after == inference_jobs_before
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        await dispose_engine()
