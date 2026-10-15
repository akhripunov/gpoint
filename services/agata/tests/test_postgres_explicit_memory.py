"""PostgreSQL end-to-end coverage for explicit remember/correct/forget commands."""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import ensure_test_relationship
from sqlalchemy import select

from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    CharacterProfileVersion,
    Consent,
    Conversation,
    LifecycleState,
    Memory,
    MemoryControlState,
    Message,
    Scope,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.memory.explicit import Correct, ExplicitMemoryService, Forget, Remember
from app.memory.precedence import TemporalMode
from app.memory.retrieval import (
    DeterministicFakeEmbeddingProvider,
    HybridRetrievalService,
    RetrievalRequest,
)
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import decrypt_text_from_storage, memory_value_aad
from app.messages.service import MessageIngestionService
from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.mac_worker import _worker_request
from app.responses.pipeline import ContextBuilder, QueryPlanner, ResponseOrchestrator

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("7508f36f-774a-48aa-8c6b-42f6f160415d")


@pytest.mark.asyncio(loop_scope="module")
async def test_explicit_memory_commands_encrypt_idempotently_correct_and_forget():
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    hmac_key = base64.b64decode(settings.request_fingerprint_hmac_key.get_secret_value())
    suffix = uuid4().hex
    user_id, character_id, conversation_id, message_id = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            assert await session.get(Tenant, TENANT_ID) is not None
            session.add_all(
                [
                    User(id=user_id, tenant_id=TENANT_ID, external_id=f"explicit-{suffix}"),
                    Character(
                        id=character_id,
                        tenant_id=TENANT_ID,
                        external_id=f"explicit-{suffix}",
                        name="Synthetic character",
                    ),
                ]
            )
            await session.flush()
            relationship = await ensure_test_relationship(
                session, TENANT_ID, user_id, character_id, Scope.real_world, None
            )
            session.add_all(
                [
                    Consent(
                        tenant_id=TENANT_ID,
                        user_id=user_id,
                        purpose="long_term_memory",
                        granted_at=now,
                        policy_version="test-v1",
                    ),
                    Conversation(
                        id=conversation_id,
                        tenant_id=TENANT_ID,
                        relationship_id=relationship.id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                        started_at=now,
                    ),
                    MemoryControlState(
                        tenant_id=TENANT_ID,
                        relationship_id=relationship.id,
                        user_id=user_id,
                        character_id=character_id,
                        scope=Scope.real_world,
                        scenario_id=None,
                    ),
                ]
            )
            await session.flush()
            aad = MessageIngestionService._aad_for(
                TENANT_ID, user_id, conversation_id, message_id
            )
            envelope = crypto.encrypt("Наша встреча 22 октября 2026 в 18:40.", aad)
            session.add(
                Message(
                    id=message_id,
                    tenant_id=TENANT_ID,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    idempotency_key=f"explicit-source-{suffix}",
                    role="user",
                    content_ciphertext=envelope.ciphertext,
                    content_nonce=envelope.nonce,
                    content_key_version=envelope.key_version,
                    occurred_at=now,
                )
            )

    principal = PrincipalContext(TENANT_ID, user_id, user_id, "internal")
    remember = Remember(
        command_id=f"remember-{suffix}",
        character_id=character_id,
        scope=Scope.real_world,
        scenario_id=None,
        source_message_id=message_id,
        slot_key="user.event.meeting",
        predicate="meeting_time",
        value_text="2026-10-22 18:40",
    )
    async with SessionFactory() as session:
        service = ExplicitMemoryService(
            session, hmac_key=hmac_key, hmac_key_version="test-v1",
            slot_identity_hmac_key=base64.b64decode(settings.shadow_candidate_hmac_key.get_secret_value()),
        )
        memory_id = await service.remember(principal, remember)
        assert await service.remember(principal, remember) == memory_id

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            memory = await session.get(Memory, memory_id)
            assert memory is not None and memory.lifecycle_state == LifecycleState.active
            assert memory.value_text is not None and "synthetic value" not in memory.value_text
            assert decrypt_text_from_storage(memory.value_text, memory_value_aad(memory), crypto=crypto) == "2026-10-22 18:40"

    correction_message_id = uuid4()
    correction_aad = MessageIngestionService._aad_for(
        TENANT_ID, user_id, conversation_id, correction_message_id
    )
    correction_envelope = crypto.encrypt(
        "Встречу перенесли на 23 октября 2026 в 19:15. Старое время больше не действует.",
        correction_aad,
    )
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add(
                Message(
                    id=correction_message_id,
                    tenant_id=TENANT_ID,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    idempotency_key=f"correction-source-{suffix}",
                    role="user",
                    content_ciphertext=correction_envelope.ciphertext,
                    content_nonce=correction_envelope.nonce,
                    content_key_version=correction_envelope.key_version,
                    occurred_at=now + timedelta(seconds=1),
                )
            )

    async with SessionFactory() as session:
        service = ExplicitMemoryService(
            session, hmac_key=hmac_key, hmac_key_version="test-v1",
            slot_identity_hmac_key=base64.b64decode(settings.shadow_candidate_hmac_key.get_secret_value()),
        )
        replacement_id = await service.correct(
            principal,
            Correct(
                command_id=f"correct-{suffix}",
                memory_id=memory_id,
                source_message_id=correction_message_id,
                predicate="meeting_time",
                value_text="2026-10-23 19:15",
            ),
        )
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            old = await session.get(Memory, memory_id)
            replacement = await session.get(Memory, replacement_id)
            assert old is not None and old.lifecycle_state == LifecycleState.historical
            assert replacement is not None and replacement.supersedes_id == memory_id
            assert replacement.value_text is not None
            assert (
                decrypt_text_from_storage(
                    replacement.value_text, memory_value_aad(replacement), crypto=crypto
                )
                == "2026-10-23 19:15"
            )

    retrieval_at = datetime.now(UTC) + timedelta(seconds=1)
    question = "Когда наша встреча?"
    profile = CharacterProfileVersion(
        tenant_id=TENANT_ID,
        character_id=character_id,
        version=1,
        personality={},
        speech_style={},
        backstory={},
        boundaries={},
        effective_at=retrieval_at,
    )
    tail_messages = []
    for index in range(14):
        tail_id = uuid4()
        tail_text = f"Смена темы, синтетическая реплика {index:02d}."
        tail_envelope = crypto.encrypt(
            tail_text,
            MessageIngestionService._aad_for(TENANT_ID, user_id, conversation_id, tail_id),
        )
        tail_messages.append(
            Message(
                id=tail_id,
                tenant_id=TENANT_ID,
                conversation_id=conversation_id,
                user_id=user_id,
                character_id=character_id,
                scope=Scope.real_world,
                scenario_id=None,
                idempotency_key=f"topic-shift-{suffix}-{index}",
                role="user",
                content_ciphertext=tail_envelope.ciphertext,
                content_nonce=tail_envelope.nonce,
                content_key_version=tail_envelope.key_version,
                occurred_at=now + timedelta(seconds=index + 2),
            )
        )
    current_question_id = uuid4()
    current_question_envelope = crypto.encrypt(
        question,
        MessageIngestionService._aad_for(
            TENANT_ID, user_id, conversation_id, current_question_id
        ),
    )
    current_question_time = now + timedelta(seconds=17)
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add_all(tail_messages)
            session.add(
                Message(
                    id=current_question_id,
                    tenant_id=TENANT_ID,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    idempotency_key=f"current-question-{suffix}",
                    role="user",
                    content_ciphertext=current_question_envelope.ciphertext,
                    content_nonce=current_question_envelope.nonce,
                    content_key_version=current_question_envelope.key_version,
                    occurred_at=current_question_time,
                )
            )
        orchestrator = ResponseOrchestrator.__new__(ResponseOrchestrator)
        orchestrator.session = session
        orchestrator.crypto = crypto
        recent_history = await orchestrator._recent_messages(
            principal,
            SimpleNamespace(
                id=conversation_id, character_id=character_id, scope=Scope.real_world
            ),
            current_message_id=current_question_id,
        )
    assert len(recent_history) == 16
    assert recent_history[0].content == "Наша встреча 22 октября 2026 в 18:40."
    assert recent_history[1].content.startswith("Встречу перенесли на 23 октября 2026")

    async with SessionFactory() as session:
        retrieval_service = HybridRetrievalService(
            session,
            DeterministicFakeEmbeddingProvider(),
            hmac_key=b"r" * 32,
        )
        current_result = await retrieval_service.retrieve(
            principal,
            RetrievalRequest(
                character_id=character_id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                scenario_id=None,
                query="meeting_time",
                as_of=retrieval_at,
            ),
        )
        historical_result = await retrieval_service.retrieve(
            principal,
            RetrievalRequest(
                character_id=character_id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                scenario_id=None,
                query="meeting_time",
                as_of=retrieval_at,
                temporal_mode=TemporalMode.HISTORICAL,
            ),
        )

    current_context = ContextBuilder.build(
        profile,
        question,
        current_result.results,
        QueryPlanner().plan(question, retrieval_at),
        conversation_history=recent_history,
        as_of=current_question_time,
    )
    assert [entry.content for entry in current_context.conversation_history] == [
        f"Смена темы, синтетическая реплика {index:02d}." for index in range(2, 14)
    ]
    current_payload = _worker_request(current_context)
    assert [item["value"] for item in current_payload["memories"]] == [
        "2026-10-23 19:15"
    ]
    assert current_payload["memories"][0]["truth_role"] == "current"

    historical_context = ContextBuilder.build(
        profile,
        "Что было раньше?",
        historical_result.results,
        QueryPlanner().plan("Что было раньше?", retrieval_at),
        as_of=retrieval_at,
    )
    historical_payload = _worker_request(historical_context)
    history_by_value = {
        item["value"]: item["truth_role"] for item in historical_payload["memories"]
    }
    assert history_by_value == {
        "2026-10-22 18:40": "historical",
        "2026-10-23 19:15": "current",
    }

    deepseek_payloads = []

    async def capture_deepseek(request: httpx.Request) -> httpx.Response:
        deepseek_payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"id": "offline", "choices": [{"message": {"content": "ответ"}}]}
        )

    async def check_deepseek(context):
        provider = DeepSeekResponseProvider(
            api_key="offline-test-key",
            base_url="https://anymodel.org/v1",
            timeout_seconds=1,
            transport=httpx.MockTransport(capture_deepseek),
        )
        await provider.complete(context)
        return "\n".join(
            message["content"] for message in deepseek_payloads[-1]["messages"]
            if isinstance(message.get("content"), str)
        )

    current_deepseek = await check_deepseek(current_context)
    assert "2026-10-23 19:15" in current_deepseek
    assert "2026-10-22 18:40" not in current_deepseek
    historical_deepseek = await check_deepseek(historical_context)
    assert "2026-10-22 18:40" in historical_deepseek
    assert "2026-10-23 19:15" in historical_deepseek

    second_conversation_id = uuid4()
    city_message_id = uuid4()
    city_source = (
        "Синтетическая история: вымышленный персонаж живёт в Лесогорске "
        "и планирует поездку в Китай."
    )
    city_envelope = crypto.encrypt(
        city_source,
        MessageIngestionService._aad_for(
            TENANT_ID, user_id, second_conversation_id, city_message_id
        ),
    )
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            session.add(
                Conversation(
                    id=second_conversation_id,
                    tenant_id=TENANT_ID,
                    relationship_id=relationship.id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    started_at=retrieval_at,
                )
            )
            session.add(
                Message(
                    id=city_message_id,
                    tenant_id=TENANT_ID,
                    conversation_id=second_conversation_id,
                    user_id=user_id,
                    character_id=character_id,
                    scope=Scope.real_world,
                    scenario_id=None,
                    idempotency_key=f"city-source-{suffix}",
                    role="user",
                    content_ciphertext=city_envelope.ciphertext,
                    content_nonce=city_envelope.nonce,
                    content_key_version=city_envelope.key_version,
                    occurred_at=retrieval_at,
                )
            )
    async with SessionFactory() as session:
        memory_service = ExplicitMemoryService(
            session,
            hmac_key=hmac_key,
            hmac_key_version="test-v1",
            slot_identity_hmac_key=base64.b64decode(
                settings.shadow_candidate_hmac_key.get_secret_value()
            ),
        )
        await memory_service.remember(
            principal,
            Remember(
                command_id=f"remember-city-{suffix}",
                character_id=character_id,
                scope=Scope.real_world,
                scenario_id=None,
                source_message_id=city_message_id,
                slot_key="user.home_city",
                predicate="lives_in",
                value_text="Лесогорск",
            ),
        )
        await memory_service.remember(
            principal,
            Remember(
                command_id=f"remember-trip-{suffix}",
                character_id=character_id,
                scope=Scope.real_world,
                scenario_id=None,
                source_message_id=city_message_id,
                slot_key="user.trip.china",
                predicate="travel_to",
                value_text="Китай",
            ),
        )

    async with SessionFactory() as session:
        cross_conversation_result = await HybridRetrievalService(
            session,
            DeterministicFakeEmbeddingProvider(),
            hmac_key=b"r" * 32,
        ).retrieve(
            principal,
            RetrievalRequest(
                character_id=character_id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                scenario_id=None,
                query="lives_in",
                as_of=retrieval_at + timedelta(seconds=1),
            ),
        )
    city_question = "В каком городе живёт персонаж?"
    city_context = ContextBuilder.build(
        profile,
        city_question,
        cross_conversation_result.results,
        QueryPlanner().plan(city_question, retrieval_at + timedelta(seconds=1)),
        as_of=retrieval_at + timedelta(seconds=1),
    )
    city_payload = _worker_request(city_context)
    assert any(item["value"] == "Лесогорск" for item in city_payload["memories"])

    async with SessionFactory() as session:
        trip_result = await HybridRetrievalService(
            session,
            DeterministicFakeEmbeddingProvider(),
            hmac_key=b"r" * 32,
        ).retrieve(
            principal,
            RetrievalRequest(
                character_id=character_id,
                relationship_id=relationship.id,
                scope=Scope.real_world,
                scenario_id=None,
                query="travel_to",
                as_of=retrieval_at + timedelta(seconds=1),
            ),
        )
    trip_context = ContextBuilder.build(
        profile,
        "Куда планировалась поездка?",
        trip_result.results,
        QueryPlanner().plan("Куда планировалась поездка?", retrieval_at + timedelta(seconds=1)),
        as_of=retrieval_at + timedelta(seconds=1),
    )
    assert any(item["value"] == "Китай" for item in _worker_request(trip_context)["memories"])

    async with SessionFactory() as session:
        service = ExplicitMemoryService(
            session, hmac_key=hmac_key, hmac_key_version="test-v1",
            slot_identity_hmac_key=base64.b64decode(settings.shadow_candidate_hmac_key.get_secret_value()),
        )
        assert (
            await service.forget(
                principal,
                Forget(command_id=f"forget-{suffix}", memory_id=replacement_id),
            )
            is False
        )
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            forgotten = await session.get(Memory, replacement_id)
            control = await session.scalar(
                select(MemoryControlState).where(
                    MemoryControlState.tenant_id == TENANT_ID,
                    MemoryControlState.user_id == user_id,
                    MemoryControlState.character_id == character_id,
                    MemoryControlState.scope == Scope.real_world,
                    MemoryControlState.scenario_id.is_(None),
                )
            )
            assert forgotten is not None and forgotten.lifecycle_state == LifecycleState.deleted
            assert (
                forgotten.value_text,
                forgotten.value_json,
                forgotten.search_text,
                forgotten.embedding,
            ) == (None, None, None, None)
            assert control is not None and control.deletion_epoch == 1
