"""Transport-neutral LocaleResolver and rendering port.

The resolver reads only the authoritative Stage 8 ``user_character_locale_
preferences`` table under the runtime role and tenant context. It never accepts a
provider profile hint (in particular the Telegram ``language_code``) as an
authoritative locale: such a hint may, in a future stage, become a non-binding
*proposal* presented to the user, but only an explicit user choice stored in the
authoritative table selects a pack here.

Fallback is deterministic and documented: a missing, unknown or disabled locale
always resolves to ``ru-RU``, the safe product default for the initial Russian
launch. The fallback is fixed in code so a malformed preference cannot pick an
arbitrary language.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import UserCharacterLocalePreference
from app.i18n.contracts import (
    DEFAULT_LOCALE_CODE,
    SUPPORTED_LOCALE_CODES,
    LocalePackError,
    LocalePackSource,
    LocalePreferenceReader,
)
from app.i18n.registry import PackRegistry, default_registry
from app.i18n.renderer import LocaleRenderer, RenderedMessage


class LocaleResolutionError(Exception):
    """Raised only when the authoritative table cannot be consulted safely."""


class AuthoritativeLocalePreferenceReader(LocalePreferenceReader):
    """Reads the Stage 8 locale preference under tenant RLS and the runtime role.

    Returns ``None`` when no explicit preference exists; the resolver maps that
    to the deterministic fallback. Any failure to read the authoritative table
    surfaces as an exception instead of a silent provider-hint fallback.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(self, tenant_id: object, user_id: object, character_id: object) -> str | None:
        if not isinstance(tenant_id, UUID) or not isinstance(user_id, UUID):
            raise LocaleResolutionError("invalid_principal")
        if not isinstance(character_id, UUID):
            raise LocaleResolutionError("invalid_principal")
        await set_tenant_context(self._session, str(tenant_id))
        row = await self._session.scalar(
            select(UserCharacterLocalePreference).where(
                UserCharacterLocalePreference.tenant_id == tenant_id,
                UserCharacterLocalePreference.user_id == user_id,
                UserCharacterLocalePreference.character_id == character_id,
            )
        )
        if row is None:
            return None
        code = row.locale_code
        if not isinstance(code, str) or code not in SUPPORTED_LOCALE_CODES:
            # The table CHECK constraint already bounds this, but a defensive
            # read still maps a malformed stored value to the deterministic
            # fallback via the resolver, never to a provider hint.
            return None
        return code


@dataclass(frozen=True, slots=True)
class ResolvedLocale:
    locale_code: str
    is_fallback: bool


class LocaleResolver:
    """Resolves a Locale Pack from the authoritative user-character preference.

    The resolver has no ``language_code`` parameter and accepts no provider
    hint. ``en-US`` and ``en-GB`` are distinct resolutions even where strings
    overlap today.
    """

    def __init__(
        self,
        reader: LocalePreferenceReader,
        registry: LocalePackSource | PackRegistry | None = None,
    ) -> None:
        self._reader = reader
        self._registry = registry if registry is not None else default_registry()

    async def resolve(self, tenant_id: UUID, user_id: UUID, character_id: UUID) -> ResolvedLocale:
        authoritative = await self._reader.read(tenant_id, user_id, character_id)
        if authoritative is not None:
            pack = self._registry.get(authoritative)
            if pack is not None:
                return ResolvedLocale(locale_code=pack.locale_code, is_fallback=False)
        return ResolvedLocale(locale_code=DEFAULT_LOCALE_CODE, is_fallback=True)

    async def resolve_pack(
        self, tenant_id: UUID, user_id: UUID, character_id: UUID
    ) -> tuple[LocaleRenderer, ResolvedLocale]:
        resolved = await self.resolve(tenant_id, user_id, character_id)
        pack = self._registry.get(resolved.locale_code)
        if pack is None:
            # The default pack must always be enabled; reaching here means the
            # registry itself is inconsistent, which is a fail-closed condition.
            raise LocalePackError("default_pack_unavailable")
        return LocaleRenderer(pack), resolved


async def render_system_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    character_id: UUID,
    message_key: str,
    placeholders: dict[str, object] | None = None,
    registry: PackRegistry | None = None,
) -> RenderedMessage:
    """Convenience port for future transport adapters.

    Adapters (Telegram, MAX, web, mobile) call this port; they never import
    pack internals and never supply a provider locale hint. This stage does not
    wire the port into any webhook or outbound worker.
    """
    pack_source: PackRegistry | None = registry if registry is not None else default_registry()
    resolver = LocaleResolver(AuthoritativeLocalePreferenceReader(session), registry=pack_source)
    renderer, _ = await resolver.resolve_pack(tenant_id, user_id, character_id)
    return renderer.render(message_key, placeholders)
