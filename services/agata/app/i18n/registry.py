"""Locale Pack registry: load once, validate, fail closed, expose only enabled.

The registry is the only constructor of pack instances in the runtime path: it
builds every pack through ``build_pack``, which validates parity and shape. A
malformed pack raises at construction and prevents the registry (and therefore
the resolver) from ever returning an incomplete or substituted string.

When constructed with an explicit pack dict (for test or future staged rollout),
the registry recomputes the content hash of every supplied pack to detect
forged or tampered objects that bypassed the constructor.
"""

from __future__ import annotations

from functools import lru_cache

from app.i18n.contracts import (
    DEFAULT_LOCALE_CODE,
    SUPPORTED_LOCALE_CODES,
    LocalePack,
    LocalePackError,
    LocalePackSource,
    _constant_time_hash_equal,
    compute_content_hash,
)
from app.i18n.packs import build_pack


class PackRegistry(LocalePackSource):
    """Read-only registry of validated Locale Packs.

    Only ``enabled`` packs are returned to the resolver. ``experimental`` and
    ``retired`` packs exist as construction-time constants but never reach the
    rendering path.
    """

    def __init__(self, packs: dict[str, LocalePack] | None = None) -> None:
        self._packs: dict[str, LocalePack] = (
            dict(packs) if packs is not None else self._load_default()
        )
        self._validate()

    @staticmethod
    def _load_default() -> dict[str, LocalePack]:
        packs: dict[str, LocalePack] = {}
        for locale_code in SUPPORTED_LOCALE_CODES:
            pack = build_pack(locale_code)
            if pack.status != "enabled":
                raise LocalePackError("non_default_pack_not_enabled")
            packs[locale_code] = pack
        return packs

    def _validate(self) -> None:
        if not self._packs:
            raise LocalePackError("empty_pack_registry")
        if DEFAULT_LOCALE_CODE not in self._packs:
            raise LocalePackError("missing_default_pack")
        default_pack = self._packs[DEFAULT_LOCALE_CODE]
        if default_pack.status != "enabled":
            raise LocalePackError("default_pack_not_enabled")
        seen_hashes: set[str] = set()
        for locale_code, pack in self._packs.items():
            if not isinstance(pack, LocalePack):
                raise LocalePackError("invalid_pack")
            if locale_code != pack.locale_code:
                raise LocalePackError("pack_locale_mismatch")
            # Reconstruct through the immutable contract before reading any
            # fields. This catches a malicious object.__setattr__ mutation of
            # message shape/key parity as well as the hash recheck below.
            LocalePack(
                schema_version=pack.schema_version,
                locale_code=pack.locale_code,
                pack_version=pack.pack_version,
                status=pack.status,
                messages=pack.messages,
                content_hash=pack.content_hash,
            )
            # Recompute the hash to detect tampered/forged pack objects.
            computed = compute_content_hash(
                schema_version=pack.schema_version,
                locale_code=pack.locale_code,
                pack_version=pack.pack_version,
                status=pack.status,
                messages=pack.messages,
            )
            if not _constant_time_hash_equal(computed, pack.content_hash):
                raise LocalePackError("content_hash_mismatch")
            if pack.content_hash in seen_hashes:
                raise LocalePackError("duplicate_pack_content_hash")
            seen_hashes.add(pack.content_hash)

    def get(self, locale_code: str) -> LocalePack | None:
        pack = self._packs.get(locale_code)
        if pack is None or pack.status != "enabled":
            return None
        return pack

    def enabled_locale_codes(self) -> frozenset[str]:
        return frozenset(code for code, pack in self._packs.items() if pack.status == "enabled")

    def require(self, locale_code: str) -> LocalePack:
        """Resolve an enabled pack or fail closed; used by the resolver fallback chain."""
        pack = self.get(locale_code)
        if pack is None:
            raise LocalePackError("pack_not_enabled")
        return pack


@lru_cache(maxsize=1)
def default_registry() -> PackRegistry:
    """Process-wide registry built exactly once from the canonical pack contents."""
    return PackRegistry()
