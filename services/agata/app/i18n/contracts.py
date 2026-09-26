"""Strict, versioned Locale Pack contracts.

A Locale Pack is a finite, immutable mapping of message keys to literal system
strings. There is no free-form JSON, no nested template structure, no eval and
no external translation surface. Packs are loaded once at construction time and
fail closed on any malformed shape, missing key, duplicate key, mixed-language
input, or forged/unexpected content hash.

These contracts are transport-neutral: nothing in this module imports a channel,
provider, Telegram, request or HTTP type. The LocaleResolver only reads the
Stage 8 ``user_character_locale_preferences`` authoritative table and selects an
``enabled`` pack with a documented deterministic fallback.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol


class LocalePackError(ValueError):
    """A malformed, incomplete or ambiguous Locale Pack fails closed with this error."""


# Finite, ordered allowlist of mandatory system message keys. Adding a key is a
# deliberate contract change: every pack must contain exactly this set.
REQUIRED_MESSAGE_KEYS: tuple[str, ...] = (
    "ai_disclosure",
    "ai_disclosure_staging",
    "onboarding_welcome",
    "onboarding_age_required",
    "onboarding_consent_required",
    "onboarding_locale_selected",
    "onboarding_character_required",
    "onboarding_ready",
    "proactive_check_in",
    "help_overview",
    "privacy_overview",
    "memory_overview",
    "unsupported_action",
    "request_rejected",
    "temporarily_unavailable",
    "generic_retry",
    "deletion_requested",
    "export_requested",
)

# Locale codes are deliberately explicit. "en-US" and "en-GB" are distinct packs
# even if a subset of strings is currently identical; never collapse them.
SUPPORTED_LOCALE_CODES: frozenset[str] = frozenset({"ru-RU", "en-US", "en-GB"})

# Safe product default for the initial Russian launch. The fallback is fixed in
# code, not in configuration, so a malformed or disabled preference cannot pick
# an arbitrary language.
DEFAULT_LOCALE_CODE: str = "ru-RU"

PACK_STATUSES: frozenset[str] = frozenset({"experimental", "enabled", "retired"})

# Conservative limits that bound the deterministic content hash input, the
# literal pack value size and the rendered output size. Renderer placeholder
# values are bounded separately.
_MAX_PACK_VERSION_DIGITS = 6
_MAX_SCHEMA_VERSION_DIGITS = 4
_MAX_MESSAGE_LENGTH = 4096
_MIN_MESSAGE_LENGTH = 1

# Placeholder contract. Only this finite, typed allowlist may ever reach the
# renderer. Raw user message, prompt, memory content, Telegram profile fields
# and any provider value are explicitly out of scope.
PLACEHOLDER_FIELDS: frozenset[str] = frozenset({"character_name"})
_MAX_PLACEHOLDER_VALUE_LENGTH = 128
_MAX_RENDERED_LENGTH = 8192


def _require_non_empty_str(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise LocalePackError(f"invalid_{name}")
    return value


def _require_bounded_str(value: object, *, name: str, minimum: int, maximum: int) -> str:
    text = _require_non_empty_str(value, name=name)
    if not minimum <= len(text) <= maximum:
        raise LocalePackError(f"invalid_{name}")
    if "\x00" in text:
        raise LocalePackError(f"invalid_{name}")
    return text


def _require_positive_int(value: object, *, name: str, maximum_digits: int) -> int:
    # Booleans are ints in Python; reject them explicitly so a pack never
    # accidentally accepts ``True`` as version 1.
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 10**maximum_digits - 1
    ):
        raise LocalePackError(f"invalid_{name}")
    return value


def _require_status(value: object) -> str:
    text = _require_non_empty_str(value, name="pack_status")
    if text not in PACK_STATUSES:
        raise LocalePackError("invalid_pack_status")
    return text


def _require_locale_code(value: object) -> str:
    text = _require_non_empty_str(value, name="locale_code")
    if text not in SUPPORTED_LOCALE_CODES:
        raise LocalePackError("invalid_locale_code")
    return text


# ---- canonical content hash -------------------------------------------------

_HASH_DOMAIN_TAG = b"gptgirls-locale-pack:v2"


def compute_content_hash(
    *,
    schema_version: int,
    locale_code: str,
    pack_version: int,
    status: str,
    messages: dict[str, str],
) -> str:
    """Deterministic SHA-256 over a tagged length-prefixed canonical representation.

    No delimiter-join for arbitrary strings — each field is length-prefixed so
    no value can inject a separator that would create an ambiguous encoding.
    The domain tag prevents collision with any other hash in the system.
    """
    h = hashlib.sha256()
    # Versioned domain tag with length prefix.
    h.update(_HASH_DOMAIN_TAG)
    h.update(b"\x00")
    # Each field: 4-byte big-endian length, then UTF-8 bytes.
    for field in (
        str(schema_version),
        locale_code,
        str(pack_version),
        status,
    ):
        encoded = field.encode("utf-8")
        h.update(len(encoded).to_bytes(4, "big"))
        h.update(encoded)
    # Messages in REQUIRED_MESSAGE_KEYS order — skip missing keys silently
    # (validation is the caller's job).
    for key in REQUIRED_MESSAGE_KEYS:
        if key not in messages:
            continue
        value = messages[key]
        if not isinstance(value, str) or not value:
            continue
        for field in (key, value):
            encoded = field.encode("utf-8")
            h.update(len(encoded).to_bytes(4, "big"))
            h.update(encoded)
    return h.hexdigest()


def _constant_time_hash_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two hex hash strings."""
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


# ---- LocalePack --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalePack:
    """Immutable, validated, transport-neutral Locale Pack.

    Construction validates the full shape, key parity, and content hash
    integrity. The content hash is recomputed from the validated fields and
    compared constant-time to the supplied value — a forged or mismatched hash
    fails closed.
    """

    schema_version: int
    locale_code: str
    pack_version: int
    status: str
    messages: dict[str, str]
    content_hash: str

    def __post_init__(self) -> None:
        schema_version = _require_positive_int(
            self.schema_version,
            name="schema_version",
            maximum_digits=_MAX_SCHEMA_VERSION_DIGITS,
        )
        pack_version = _require_positive_int(
            self.pack_version,
            name="pack_version",
            maximum_digits=_MAX_PACK_VERSION_DIGITS,
        )
        locale_code = _require_locale_code(self.locale_code)
        status = _require_status(self.status)
        if not isinstance(self.messages, dict) or not self.messages:
            raise LocalePackError("invalid_messages")
        required = set(REQUIRED_MESSAGE_KEYS)
        seen: set[str] = set()
        normalized: dict[str, str] = {}
        for key, value in self.messages.items():
            if not isinstance(key, str) or key not in required:
                raise LocalePackError("invalid_message_key")
            if key in seen:
                raise LocalePackError("duplicate_message_key")
            seen.add(key)
            normalized[key] = _require_bounded_str(
                value,
                name="message_value",
                minimum=_MIN_MESSAGE_LENGTH,
                maximum=_MAX_MESSAGE_LENGTH,
            )
        if seen != required:
            missing = required - seen
            extra = seen - required
            if missing:
                raise LocalePackError("missing_message_key")
            if extra:
                raise LocalePackError("invalid_message_key")
        # content_hash is validated for shape first, then recomputed and
        # compared constant-time to detect forgery.
        if not isinstance(self.content_hash, str) or len(self.content_hash) != 64:
            raise LocalePackError("invalid_content_hash")
        try:
            int(self.content_hash, 16)
        except ValueError as exc:
            raise LocalePackError("invalid_content_hash") from exc
        computed = compute_content_hash(
            schema_version=schema_version,
            locale_code=locale_code,
            pack_version=pack_version,
            status=status,
            messages=normalized,
        )
        if not _constant_time_hash_equal(computed, self.content_hash):
            raise LocalePackError("content_hash_mismatch")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "pack_version", pack_version)
        object.__setattr__(self, "locale_code", locale_code)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "messages", normalized)

    def message(self, key: str) -> str:
        """Return a literal message; an unknown key fails closed instead of substituting."""
        if not isinstance(key, str) or key not in self.messages:
            raise LocalePackError("unknown_message_key")
        return self.messages[key]


class LocalePackSource(Protocol):
    """Transport-neutral source of authoritative locale packs."""

    def get(self, locale_code: str) -> LocalePack | None: ...
    def enabled_locale_codes(self) -> frozenset[str]: ...


class LocalePreferenceReader(Protocol):
    """Reads the authoritative user-character locale preference."""

    async def read(
        self, tenant_id: object, user_id: object, character_id: object
    ) -> str | None: ...
