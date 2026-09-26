"""Real PostgreSQL isolation checks using synthetic identities and encrypted values."""

import base64
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
    Consent,
    Conversation,
    EvidenceState,
    LifecycleState,
    Memory,
    MemoryControlState,
    MemorySource,
    Message,
    Scope,
    Tenant,
    User,
)
from app.domain.principal import PrincipalContext
from app.memory.retrieval import (
    DeterministicFakeEmbeddingProvider,
    HybridRetrievalService,
    RetrievalRequest,
)
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import content_aad, encrypt_text_for_storage
from app.messages.service import MessageIngestionService

TEST_DSN = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="AGATA_TEST_DATABASE_URL not set")
TENANT_ID = UUID("7508f36f-774a-48aa-8c6b-42f6f160415d")
SCENARIO_ZERO = UUID("00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio(loop_scope="module")
async def test_postgres_user_character_tenant_and_scenario_retrieval_isolation():
    settings = get_settings()
    crypto = AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )
    suffix = uuid4().hex
    labels: dict[tuple[str, str, str], str] = {}
    principals: dict[tuple[str, str, str], tuple[UUID, UUID]] = {}
    scenario_ids: dict[tuple[str, str, str], UUID | None] = {}
    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            tenant = await session.get(Tenant, TENANT_ID)
            assert tenant is not None
            tenant2_id = uuid4()
            session.add(Tenant(id=tenant2_id))
            users = {name: User(tenant_id=TENANT_ID, external_id=f"iso-{suffix}-{name}") for name in ("A", "B")}
            characters = {name: Character(tenant_id=TENANT_ID, external_id=f"iso-{suffix}-{name}", name=name) for name in ("Anna", "Maria")}
            session.add_all([*users.values(), *characters.values()])
            await session.flush()
            users2 = User(tenant_id=tenant2_id, external_id=f"iso-{suffix}-A")
            anna2 = Character(tenant_id=tenant2_id, external_id=f"iso-{suffix}-Anna", name="Anna")
            await set_tenant_context(session, str(tenant2_id))
            session.add_all([users2, anna2])
            await session.flush()
            consents = {}
            for user in (*users.values(), users2):
                await set_tenant_context(session, str(user.tenant_id))
                consent = Consent(tenant_id=user.tenant_id, user_id=user.id, purpose="long_term_memory", granted_at=datetime.now(UTC), policy_version="test")
                session.add(consent)
                consents[user.id] = consent
            await session.flush()

            combinations = [
                ("A", "Anna", "real_world", None, TENANT_ID),
                ("A", "Maria", "real_world", None, TENANT_ID),
                ("B", "Anna", "real_world", None, TENANT_ID),
                ("A", "Anna", "roleplay", "S1", TENANT_ID),
                ("A", "Anna", "roleplay", "S2", TENANT_ID),
                ("A2", "Anna2", "real_world", None, tenant2_id),
            ]
            for user_label, character_label, scope_label, scenario_label, tenant_id in combinations:
                user = users2 if user_label == "A2" else users[user_label]
                character = anna2 if character_label == "Anna2" else characters[character_label]
                await set_tenant_context(session, str(tenant_id))
                scenario_id = uuid4() if scenario_label else None
                if scenario_id is not None:
                    from app.db.models import RoleplayScenario
                    session.add(RoleplayScenario(id=scenario_id, tenant_id=tenant_id, user_id=user.id, character_id=character.id, name=f"isolation-{scenario_label}-{suffix}", started_at=datetime.now(UTC)))
                    await session.flush()
                scope = Scope(scope_label)
                conversation = Conversation(tenant_id=tenant_id, user_id=user.id, character_id=character.id, scope=scope, scenario_id=scenario_id, started_at=datetime.now(UTC))
                control = MemoryControlState(tenant_id=tenant_id, user_id=user.id, character_id=character.id, scope=scope, scenario_id=scenario_id)
                session.add_all([conversation, control])
                await session.flush()
                message_id = uuid4()
                message = Message(
                    id=message_id, tenant_id=tenant_id, conversation_id=conversation.id, user_id=user.id,
                    character_id=character.id, scope=scope, scenario_id=scenario_id,
                    idempotency_key=f"iso-{suffix}-{user_label}-{character_label}-{scenario_label or 'real'}",
                    role="user", content_ciphertext=None, content_nonce=None, content_key_version=None,
                    occurred_at=datetime.now(UTC),
                )
                envelope = crypto.encrypt("synthetic source", MessageIngestionService._aad_for(tenant_id, user.id, conversation.id, message_id))
                message.content_ciphertext = envelope.ciphertext
                message.content_nonce = envelope.nonce
                message.content_key_version = envelope.key_version
                session.add(message)
                memory_id = uuid4()
                token = f"private-{user_label}-{character_label}-{scenario_label or 'real'}-{suffix}"
                memory = Memory(
                    id=memory_id, tenant_id=tenant_id, user_id=user.id, character_id=character.id,
                    kind="semantic_fact", scope=scope, scenario_id=scenario_id,
                    slot_key=f"test.{suffix}.{user_label}.{character_label}.{scenario_label or 'real'}",
                    predicate="test_fact",
                    value_text=encrypt_text_for_storage(
                        token,
                        content_aad(entity="memories", tenant_id=tenant_id, user_id=user.id, character_id=character.id, scope=scope, scenario_id=scenario_id, record_id=memory_id, field="value_text"),
                        crypto=crypto,
                    ),
                    observed_at=datetime.now(UTC), evidence_state=EvidenceState.confirmed,
                    lifecycle_state=LifecycleState.active, confidence=0.9, importance=0.9,
                    consent_id=consents[user.id].id, created_epoch=control.deletion_epoch,
                )
                session.add(memory)
                await session.flush()
                session.add(MemorySource(
                    tenant_id=tenant_id, memory_id=memory_id, message_id=message_id,
                    user_id=user.id, character_id=character.id, scope=scope,
                    scenario_key=scenario_id or SCENARIO_ZERO, relation="supports",
                ))
                key = (user_label, character_label, scenario_label or "real")
                labels[key] = token
                principals[key] = (user.id, character.id)
                scenario_ids[key] = scenario_id

    hmac_key = base64.b64decode(settings.retrieval_query_hmac_key.get_secret_value())
    service_results: dict[tuple[str, str, str], set[str]] = {}
    for key, (user_id, character_id) in principals.items():
        user_label, _, scenario_label = key
        tenant_id = TENANT_ID if user_label != "A2" else tenant2_id
        principal = PrincipalContext(tenant_id, user_id, user_id, "internal")
        scope = Scope.roleplay if scenario_label in {"S1", "S2"} else Scope.real_world
        scenario_id = scenario_ids[key]
        request = RetrievalRequest(character_id, scope, scenario_id, "synthetic isolation query", datetime.now(UTC))
        async with SessionFactory() as session:
            result = await HybridRetrievalService(session, DeterministicFakeEmbeddingProvider(), hmac_key=hmac_key).retrieve(principal, request)
        service_results[key] = {item.value for item in result.results if item.value}

    assert service_results[("A", "Anna", "real")] == {labels[("A", "Anna", "real")]}
    assert service_results[("A", "Maria", "real")] == {labels[("A", "Maria", "real")]}
    assert service_results[("B", "Anna", "real")] == {labels[("B", "Anna", "real")]}
    assert service_results[("A", "Anna", "S1")] == {labels[("A", "Anna", "S1")]}
    assert service_results[("A", "Anna", "S2")] == {labels[("A", "Anna", "S2")]}
    assert service_results[("A2", "Anna2", "real")] == {labels[("A2", "Anna2", "real")]}
