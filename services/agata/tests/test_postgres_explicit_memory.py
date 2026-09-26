"""PostgreSQL end-to-end coverage for explicit remember/correct/forget commands."""

import base64
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from conftest import ensure_test_relationship
from sqlalchemy import select

from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import (
    Character,
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
from app.messages.crypto import AeadCrypto
from app.messages.protected_content import decrypt_text_from_storage, memory_value_aad
from app.messages.service import MessageIngestionService

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
            envelope = crypto.encrypt("synthetic source", aad)
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
        slot_key="user.preference",
        predicate="likes",
        value_text="synthetic value",
    )
    async with SessionFactory() as session:
        service = ExplicitMemoryService(session, hmac_key=hmac_key, hmac_key_version="test-v1")
        memory_id = await service.remember(principal, remember)
        assert await service.remember(principal, remember) == memory_id

    async with SessionFactory() as session:
        async with session.begin():
            await set_tenant_context(session, str(TENANT_ID))
            memory = await session.get(Memory, memory_id)
            assert memory is not None and memory.lifecycle_state == LifecycleState.active
            assert memory.value_text is not None and "synthetic value" not in memory.value_text
            assert decrypt_text_from_storage(memory.value_text, memory_value_aad(memory), crypto=crypto) == "synthetic value"

    async with SessionFactory() as session:
        service = ExplicitMemoryService(session, hmac_key=hmac_key, hmac_key_version="test-v1")
        replacement_id = await service.correct(
            principal,
            Correct(
                command_id=f"correct-{suffix}",
                memory_id=memory_id,
                source_message_id=message_id,
                predicate="likes",
                value_text="corrected synthetic value",
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
                == "corrected synthetic value"
            )

    async with SessionFactory() as session:
        service = ExplicitMemoryService(session, hmac_key=hmac_key, hmac_key_version="test-v1")
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
