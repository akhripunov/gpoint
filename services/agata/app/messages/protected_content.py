"""Application-level protection for sensitive AGATA derived and canonical payloads."""

from __future__ import annotations

import base64
import json
import re
from hashlib import sha256
from hmac import new as hmac_new
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.messages.crypto import (
    AeadCrypto,
    CiphertextEnvelope,
    CryptoError,
    canonical_tagged_fields,
    derive_hmac_subkey,
)

_TEXT_PREFIX = "agata$"
_JSON_MARKER = "_agata_ciphertext"
_SEARCH_DOMAIN = b"gpoint/agata/search-index/v1"
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_OUTBOX_KEYS = {
    "conversation_summary": frozenset({"conversation_id", "user_id", "character_id"}),
    "relationship_note_refresh": frozenset({"relationship_id", "creation_epoch"}),
    "snapshot_refresh": frozenset({"relationship_id", "conversation_id", "snapshot_type", "creation_epoch"}),
    "memory_extract": frozenset(
        {
            "message_id",
            "conversation_id",
            "code",
            "reply_context_ciphertext",
            "reply_context_nonce",
            "reply_context_key_version",
        }
    ),
    "memory_promote": frozenset({"candidate_id"}),
    "memory_embed": frozenset({"memory_id", "code"}),
    "memory_graph_sync": frozenset({"memory_id", "memory_version"}),
    "generic": frozenset({"code", "aggregate_id"}),
    "response_retry": frozenset({"code", "request_id", "message_id"}),
    "inference": frozenset({
        "context_ciphertext", "context_nonce", "context_key_version",
        "result_ciphertext", "result_nonce", "result_key_version",
        "purpose", "parent_job_id", "source_message_ids",
    }),
}


def content_crypto() -> AeadCrypto:
    """Resolve required key material from validated settings; never fall back to plaintext."""
    settings = get_settings()
    return AeadCrypto(
        encoded_key=settings.message_aead_key.get_secret_value(),
        key_version=settings.message_aead_key_version,
    )


def content_aad(
    *,
    entity: str,
    tenant_id: UUID,
    user_id: UUID,
    character_id: UUID,
    scope: object,
    scenario_id: UUID | None,
    record_id: UUID,
    field: str,
) -> bytes:
    """Bind encrypted payloads to their full owner, scope, row and field identity."""
    if not all(isinstance(value, UUID) for value in (tenant_id, user_id, character_id, record_id)):
        raise CryptoError("invalid_content_owner")
    scope_value = getattr(scope, "value", scope)
    if not isinstance(entity, str) or not entity or not isinstance(field, str) or not field:
        raise CryptoError("invalid_content_aad")
    if not isinstance(scope_value, str) or (scenario_id is not None and not isinstance(scenario_id, UUID)):
        raise CryptoError("invalid_content_aad")
    return canonical_tagged_fields(
        (
            (b"version", b"gpoint/agata/content-aad/v1"),
            (b"entity", entity.encode("ascii")),
            (b"tenant", tenant_id.bytes),
            (b"user", user_id.bytes),
            (b"character", character_id.bytes),
            (b"scope", scope_value.encode("ascii")),
            (b"scenario", scenario_id.bytes if scenario_id else b"\x00"),
            (b"record", record_id.bytes),
            (b"field", field.encode("ascii")),
        )
    )


def encrypt_text_for_storage(
    value: str, aad: bytes, *, crypto: AeadCrypto | None = None
) -> str:
    """Encode encrypted text in a text column, idempotently validating existing envelopes."""
    active = crypto or content_crypto()
    if not isinstance(value, str) or not value:
        raise CryptoError("invalid_plaintext")
    if value.startswith(_TEXT_PREFIX):
        decrypt_text_from_storage(value, aad, crypto=active)
        return value
    envelope = active.encrypt(value, aad)
    return _TEXT_PREFIX + "$".join(
        (
            envelope.key_version,
            base64.b64encode(envelope.nonce).decode("ascii"),
            base64.b64encode(envelope.ciphertext).decode("ascii"),
        )
    )


def decrypt_text_from_storage(
    value: str | None, aad: bytes, *, crypto: AeadCrypto | None = None
) -> str | None:
    active = crypto or content_crypto()
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(_TEXT_PREFIX):
        raise CryptoError("plaintext_not_permitted")
    try:
        version, nonce, ciphertext = value[len(_TEXT_PREFIX) :].split("$", 2)
        envelope = CiphertextEnvelope(
            base64.b64decode(ciphertext, validate=True),
            base64.b64decode(nonce, validate=True),
            version,
        )
    except (ValueError, TypeError) as exc:
        raise CryptoError("ciphertext_envelope_invalid") from exc
    return active.decrypt(envelope, aad)


def encrypt_json_for_storage(
    value: Any, aad: bytes, *, crypto: AeadCrypto | None = None
) -> dict[str, str]:
    active = crypto or content_crypto()
    if isinstance(value, dict) and _JSON_MARKER in value:
        decrypt_json_from_storage(value, aad, crypto=active)
        return value
    try:
        plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CryptoError("invalid_json_plaintext") from exc
    envelope = active.encrypt(plaintext, aad)
    return {
        _JSON_MARKER: "1",
        "key_version": envelope.key_version,
        "nonce": base64.b64encode(envelope.nonce).decode("ascii"),
        "ciphertext": base64.b64encode(envelope.ciphertext).decode("ascii"),
    }


def decrypt_json_from_storage(
    value: Any, aad: bytes, *, crypto: AeadCrypto | None = None
) -> Any:
    active = crypto or content_crypto()
    if not isinstance(value, dict) or value.get(_JSON_MARKER) != "1":
        raise CryptoError("plaintext_not_permitted")
    try:
        envelope = CiphertextEnvelope(
            base64.b64decode(value["ciphertext"], validate=True),
            base64.b64decode(value["nonce"], validate=True),
            value["key_version"],
        )
        plaintext = active.decrypt(envelope, aad)
        return json.loads(plaintext)
    except CryptoError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CryptoError("ciphertext_envelope_invalid") from exc


def opaque_search_text(value: str, encoded_key: str | bytes) -> str:
    """HMAC-token FTS index; preserves token match without persisting narrative plaintext."""
    try:
        key = (
            base64.b64decode(encoded_key, validate=True)
            if isinstance(encoded_key, str)
            else encoded_key
        )
    except (ValueError, TypeError) as exc:
        raise CryptoError("search_key_invalid") from exc
    if len(key) < 32 or not isinstance(value, str):
        raise CryptoError("search_key_invalid")
    index_key = derive_hmac_subkey(key, _SEARCH_DOMAIN)
    tokens = _TOKEN_PATTERN.findall(value.casefold())
    opaque = [
        "w" + hmac_new(index_key, token.encode("utf-8"), sha256).hexdigest() for token in tokens
    ]
    return " ".join(dict.fromkeys(opaque))


def validate_outbox_payload(job_type: str, payload: object) -> None:
    """Outbox JSON is identifiers or encrypted envelopes, never arbitrary narrative text."""
    allowed = _OUTBOX_KEYS.get(job_type)
    if not isinstance(payload, dict) or allowed is None or not set(payload).issubset(allowed):
        raise CryptoError("outbox_payload_not_content_safe")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()):
        raise CryptoError("outbox_payload_not_content_safe")
    if job_type == "inference":
        purpose = payload.get("purpose", "response")
        if purpose not in {"response", "memory_extract", "conversation_summary"}:
            raise CryptoError("outbox_payload_not_content_safe")
        if purpose == "response" and "parent_job_id" in payload:
            raise CryptoError("outbox_payload_not_content_safe")
        if purpose != "response":
            try:
                UUID(payload["parent_job_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CryptoError("outbox_payload_not_content_safe") from exc
        if "source_message_ids" in payload:
            if purpose != "conversation_summary":
                raise CryptoError("outbox_payload_not_content_safe")
            try:
                source_ids = json.loads(payload["source_message_ids"])
                parsed_ids = [UUID(value) for value in source_ids]
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise CryptoError("outbox_payload_not_content_safe") from exc
            if not 1 <= len(parsed_ids) <= 10 or len(set(parsed_ids)) != len(parsed_ids):
                raise CryptoError("outbox_payload_not_content_safe")
    encrypted_context = {
        "reply_context_ciphertext",
        "reply_context_nonce",
        "reply_context_key_version",
    }
    present = encrypted_context.intersection(payload)
    if present and present != encrypted_context:
        raise CryptoError("outbox_payload_envelope_incomplete")
    if present:
        try:
            bytes.fromhex(payload["reply_context_ciphertext"])
            bytes.fromhex(payload["reply_context_nonce"])
        except ValueError as exc:
            raise CryptoError("outbox_payload_envelope_invalid") from exc
        if len(bytes.fromhex(payload["reply_context_nonce"])) != 12:
            raise CryptoError("outbox_payload_envelope_invalid")
    for prefix in ("context", "result"):
        envelope_keys = {f"{prefix}_ciphertext", f"{prefix}_nonce", f"{prefix}_key_version"}
        present = envelope_keys.intersection(payload)
        if present and present != envelope_keys:
            raise CryptoError("outbox_payload_envelope_incomplete")
        if present:
            try:
                bytes.fromhex(payload[f"{prefix}_ciphertext"])
                nonce = bytes.fromhex(payload[f"{prefix}_nonce"])
            except ValueError as exc:
                raise CryptoError("outbox_payload_envelope_invalid") from exc
            if len(nonce) != 12:
                raise CryptoError("outbox_payload_envelope_invalid")


def protected_text_aad(**owner: Any) -> bytes:
    """Small named adapter used by call sites to keep AAD construction consistent."""
    return content_aad(**owner)


def memory_value_aad(memory: object) -> bytes:
    return content_aad(
        entity="memories",
        tenant_id=memory.tenant_id,
        user_id=memory.user_id,
        character_id=memory.character_id,
        scope=memory.scope,
        scenario_id=memory.scenario_id,
        record_id=memory.id,
        field="value_text",
    )


def segment_summary_aad(segment: object, conversation: object) -> bytes:
    return content_aad(
        entity="conversation_segments",
        tenant_id=segment.tenant_id,
        user_id=conversation.user_id,
        character_id=conversation.character_id,
        scope=conversation.scope,
        scenario_id=conversation.scenario_id,
        record_id=segment.id,
        field="summary",
    )


def segment_keyphrases_aad(segment: object, conversation: object) -> bytes:
    return content_aad(
        entity="conversation_segments",
        tenant_id=segment.tenant_id,
        user_id=conversation.user_id,
        character_id=conversation.character_id,
        scope=conversation.scope,
        scenario_id=conversation.scenario_id,
        record_id=segment.id,
        field="keyphrases",
    )


def memory_snapshot_content_aad(snapshot: object) -> bytes:
    """Versioned semantic AAD for canonical derived snapshots."""
    snapshot_type = getattr(snapshot, "snapshot_type", None)
    snapshot_key = getattr(snapshot, "snapshot_key", None)
    generation = getattr(snapshot, "generation", None)
    if (
        not isinstance(snapshot_type, str)
        or not isinstance(snapshot_key, str)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise CryptoError("invalid_snapshot_aad")
    return content_aad(
        entity="agata/memory-snapshot/v1",
        tenant_id=snapshot.tenant_id,
        user_id=snapshot.user_id,
        character_id=snapshot.character_id,
        scope=snapshot.scope,
        scenario_id=snapshot.scenario_id,
        record_id=snapshot.id,
        field=f"{snapshot_type}:{snapshot_key}:{generation}",
    )
