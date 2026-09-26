import base64
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoError(ValueError):
    pass


class UnknownKeyVersionError(CryptoError):
    """The ciphertext key version is not available in this process."""


@dataclass(frozen=True, slots=True)
class CiphertextEnvelope:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class AeadCrypto:
    def __init__(self, *, encoded_key: str, key_version: str) -> None:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except ValueError as exc:
            raise CryptoError("invalid_key") from exc
        if len(key) != 32 or not key_version:
            raise CryptoError("invalid_key")
        self._aead = AESGCM(key)
        self._key_version = key_version

    def encrypt(self, plaintext: str, aad: bytes) -> CiphertextEnvelope:
        if not isinstance(plaintext, str) or not plaintext:
            raise CryptoError("invalid_plaintext")
        nonce = os.urandom(12)
        return CiphertextEnvelope(
            self._aead.encrypt(nonce, plaintext.encode(), aad), nonce, self._key_version
        )

    def decrypt(self, envelope: CiphertextEnvelope, aad: bytes) -> str:
        if envelope.key_version != self._key_version:
            raise UnknownKeyVersionError("unknown_key_version")
        try:
            return self._aead.decrypt(envelope.nonce, envelope.ciphertext, aad).decode()
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise CryptoError("decrypt_failed") from exc


class RequestFingerprinter:
    """Dedicated HMAC port; this key is intentionally separate from the AEAD key."""

    VERSION = b"gptgirls/request-fingerprint/v1"

    def __init__(self, *, encoded_key: str) -> None:
        if not isinstance(encoded_key, str) or not encoded_key:
            raise CryptoError("invalid_fingerprint_key")
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except ValueError as exc:
            raise CryptoError("invalid_fingerprint_key") from exc
        if len(key) < 32:
            raise CryptoError("invalid_fingerprint_key")
        self._key = key

    def fingerprint(
        self,
        *,
        tenant_id: UUID,
        principal_user_id: UUID,
        conversation_id: UUID,
        character_id: UUID,
        role: str,
        plaintext: str,
        occurred_at: datetime,
    ) -> str:
        if (
            not all(
                isinstance(value, UUID)
                for value in (tenant_id, principal_user_id, conversation_id, character_id)
            )
            or not isinstance(role, str)
            or not isinstance(plaintext, str)
            or not isinstance(occurred_at, datetime)
            or occurred_at.tzinfo is None
            or occurred_at.utcoffset() is None
        ):
            raise CryptoError("invalid_fingerprint_input")
        occurred_at_utc = occurred_at.astimezone(UTC).isoformat(timespec="microseconds")
        payload = canonical_tagged_fields(
            (
                (b"version", self.VERSION),
                (b"tenant_id", tenant_id.bytes),
                (b"principal_user_id", principal_user_id.bytes),
                (b"conversation_id", conversation_id.bytes),
                (b"character_id", character_id.bytes),
                (b"role", role.encode("utf-8")),
                (b"plaintext_utf8", plaintext.encode("utf-8")),
                (b"occurred_at_utc", occurred_at_utc.encode("ascii")),
            )
        )
        return hmac.new(self._key, payload, sha256).hexdigest()


def canonical_tagged_fields(fields: tuple[tuple[bytes, bytes], ...]) -> bytes:
    """Encode typed field bytes without delimiter or representation ambiguity."""
    encoded: list[bytes] = []
    for tag, value in fields:
        if (
            not isinstance(tag, bytes)
            or not tag
            or len(tag) > 65535
            or not isinstance(value, bytes)
        ):
            raise CryptoError("invalid_canonical_field")
        encoded.append(len(tag).to_bytes(2, "big") + tag + len(value).to_bytes(8, "big") + value)
    return b"".join(encoded)


def derive_hmac_subkey(master_key: bytes, domain: bytes) -> bytes:
    """Derive non-interchangeable HMAC subkeys from an injected memory master key."""
    if (
        not isinstance(master_key, bytes)
        or len(master_key) < 32
        or not isinstance(domain, bytes)
        or not domain
    ):
        raise CryptoError("invalid_hmac_key")
    return hmac.new(
        master_key,
        canonical_tagged_fields(
            ((b"kdf_version", b"gptgirls/hmac-subkey/v1"), (b"domain", domain))
        ),
        sha256,
    ).digest()
