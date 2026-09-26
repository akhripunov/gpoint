"""Encrypt legacy AGATA memory and derived narrative payloads in place.

Revision ID: agata0002
Revises: agata0001
"""

from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    content_aad,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
    opaque_search_text,
    validate_outbox_payload,
)

revision = "agata0002"
down_revision = "agata0001"
branch_labels = None
depends_on = None


def _crypto(required: bool) -> AeadCrypto | None:
    key = os.environ.get("AGATA_MESSAGE_AEAD_KEY")
    if not key:
        if required:
            raise RuntimeError("AGATA_MESSAGE_AEAD_KEY required to protect existing payloads")
        return None
    return AeadCrypto(
        encoded_key=key,
        key_version=os.environ.get("AGATA_MESSAGE_AEAD_KEY_VERSION", "v1"),
    )


def _search_key(required: bool) -> str | None:
    key = os.environ.get("AGATA_RETRIEVAL_QUERY_HMAC_KEY")
    if not key and required:
        raise RuntimeError("AGATA_RETRIEVAL_QUERY_HMAC_KEY required to protect search indexes")
    return key


def _encrypted_search_text(value: str) -> bool:
    return bool(value) and all(
        len(token) == 65 and token[0] == "w" and all(c in "0123456789abcdef" for c in token[1:])
        for token in value.split()
    )


def upgrade() -> None:
    bind = op.get_bind()
    memories = list(
        bind.execute(
            text(
                "SELECT id,tenant_id,user_id,character_id,scope,scenario_id,value_text,value_json,search_text "
                "FROM agata.memories WHERE value_text IS NOT NULL OR value_json IS NOT NULL "
                "OR search_text IS NOT NULL"
            )
        )
    )
    segments = list(
        bind.execute(
            text(
                "SELECT s.id,s.tenant_id,c.user_id,c.character_id,c.scope,c.scenario_id,"
                "s.summary,s.keyphrases FROM agata.conversation_segments s "
                "JOIN agata.conversations c ON c.tenant_id=s.tenant_id AND c.id=s.conversation_id "
                "WHERE s.summary IS NOT NULL OR s.keyphrases IS NOT NULL"
            )
        )
    )
    views = list(
        bind.execute(
            text(
                "SELECT id,tenant_id,user_id,character_id,scope,scenario_id,content "
                "FROM agata.derived_memory_views WHERE content <> '{}'::jsonb"
            )
        )
    )

    content_crypto = _crypto(
        any(
            row.value_text is not None or row.value_json is not None or row.search_text is not None
            for row in memories
        )
        or any(row.summary is not None or row.keyphrases is not None for row in segments)
        or bool(views)
    )
    search_key = _search_key(any(row.search_text is not None for row in memories))

    if content_crypto is not None:
        for row in memories:
            value_text = row.value_text
            value_json = row.value_json
            if value_text is not None:
                value_text = encrypt_text_for_storage(
                    value_text,
                    content_aad(
                        entity="memories",
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        character_id=row.character_id,
                        scope=row.scope,
                        scenario_id=row.scenario_id,
                        record_id=row.id,
                        field="value_text",
                    ),
                    crypto=content_crypto,
                )
            if value_json is not None:
                value_json = encrypt_json_for_storage(
                    value_json,
                    content_aad(
                        entity="memories",
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        character_id=row.character_id,
                        scope=row.scope,
                        scenario_id=row.scenario_id,
                        record_id=row.id,
                        field="value_json",
                    ),
                    crypto=content_crypto,
                )
            search_text = row.search_text
            if search_text is not None and not _encrypted_search_text(search_text):
                assert search_key is not None
                search_text = opaque_search_text(search_text, search_key)
            bind.execute(
                text(
                    "UPDATE agata.memories SET value_text=:value_text,value_json=CAST(:value_json AS jsonb),"
                    "search_text=:search_text WHERE tenant_id=:tenant AND id=:id"
                ),
                {
                    "tenant": row.tenant_id,
                    "id": row.id,
                    "value_text": value_text,
                    "value_json": json.dumps(value_json, ensure_ascii=False, separators=(",", ":"))
                    if value_json is not None
                    else None,
                    "search_text": search_text,
                },
            )

        for row in segments:
            summary = row.summary
            keyphrases = row.keyphrases
            if summary is not None:
                summary = encrypt_text_for_storage(
                    summary,
                    content_aad(
                        entity="conversation_segments",
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        character_id=row.character_id,
                        scope=row.scope,
                        scenario_id=row.scenario_id,
                        record_id=row.id,
                        field="summary",
                    ),
                    crypto=content_crypto,
                )
            if keyphrases is not None:
                keyphrases = encrypt_json_for_storage(
                    keyphrases,
                    content_aad(
                        entity="conversation_segments",
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        character_id=row.character_id,
                        scope=row.scope,
                        scenario_id=row.scenario_id,
                        record_id=row.id,
                        field="keyphrases",
                    ),
                    crypto=content_crypto,
                )
            bind.execute(
                text(
                    "UPDATE agata.conversation_segments SET summary=:summary,keyphrases=CAST(:keyphrases AS jsonb) "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {
                    "tenant": row.tenant_id,
                    "id": row.id,
                    "summary": summary,
                    "keyphrases": json.dumps(keyphrases, ensure_ascii=False, separators=(",", ":"))
                    if keyphrases is not None
                    else None,
                },
            )

        for row in views:
            encrypted = encrypt_json_for_storage(
                row.content,
                content_aad(
                    entity="derived_memory_views",
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                    character_id=row.character_id,
                    scope=row.scope,
                    scenario_id=row.scenario_id,
                    record_id=row.id,
                    field="content",
                ),
                crypto=content_crypto,
            )
            bind.execute(
                text(
                    "UPDATE agata.derived_memory_views SET content=CAST(:content AS jsonb) "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {
                    "tenant": row.tenant_id,
                    "id": row.id,
                    "content": json.dumps(encrypted, ensure_ascii=False, separators=(",", ":")),
                },
            )

    # Outbox payloads are IDs plus independently AEAD-protected reply-context envelopes.
    # Refuse migration if historic payloads contain canonical narrative keys.
    for row in bind.execute(text("SELECT job_type,payload FROM agata.outbox_jobs")):
        try:
            validate_outbox_payload(row.job_type, row.payload)
        except ValueError as exc:
            raise RuntimeError("outbox payload contains unprotected user content") from exc

    # Nullable authenticated envelopes let retention physically erase payload bytes
    # without deleting audit/ownership rows. These guards require either a complete
    # valid envelope or a fully purged (all-NULL) envelope.
    columns = {
        table: {item["name"]: item for item in inspect(bind).get_columns(table, schema="agata")}
        for table in ("messages", "shadow_memory_candidates")
    }
    if not columns["messages"]["content_ciphertext"]["nullable"]:
        op.alter_column(
            "messages",
            "content_ciphertext",
            schema="agata",
            existing_type=sa.LargeBinary(),
            nullable=True,
        )
    shadow_columns = columns["shadow_memory_candidates"]
    for name, kind in (
        ("value_ciphertext", sa.LargeBinary()),
        ("value_nonce", sa.LargeBinary()),
        ("value_key_version", sa.String(length=32)),
    ):
        if not shadow_columns[name]["nullable"]:
            op.alter_column(
                "shadow_memory_candidates",
                name,
                schema="agata",
                existing_type=kind,
                nullable=True,
            )
    for table, name in (
        ("messages", "ck_message_content_envelope"),
        ("shadow_memory_candidates", "ck_shadow_candidate_envelope"),
    ):
        constraints = {item["name"] for item in inspect(bind).get_check_constraints(table, schema="agata")}
        if name in constraints:
            op.drop_constraint(name, table, schema="agata", type_="check")
    op.create_check_constraint(
        "ck_message_content_envelope",
        "messages",
        "(content_ciphertext IS NULL AND content_nonce IS NULL AND content_key_version IS NULL) "
        "OR (content_ciphertext IS NOT NULL AND content_nonce IS NOT NULL "
        "AND content_key_version IS NOT NULL AND octet_length(content_ciphertext) > 16 "
        "AND octet_length(content_nonce) = 12 AND content_key_version IS NOT NULL)",
        schema="agata",
    )
    op.create_check_constraint(
        "ck_shadow_candidate_envelope",
        "shadow_memory_candidates",
        "(value_ciphertext IS NULL AND value_nonce IS NULL AND value_key_version IS NULL) "
        "OR (value_ciphertext IS NOT NULL AND value_nonce IS NOT NULL "
        "AND value_key_version IS NOT NULL AND length(value_ciphertext) > 16 "
        "AND octet_length(value_nonce) = 12 AND value_key_version IS NOT NULL)",
        schema="agata",
    )


def downgrade() -> None:
    raise RuntimeError(
        "agata0002 is irreversible: canonical narrative data must not be downgraded to plaintext"
    )
