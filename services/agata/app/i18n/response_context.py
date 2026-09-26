"""Bounded, authoritative Locale Pack fragment for deterministic responses."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.domain.principal import PrincipalContext
from app.i18n.contracts import LocalePackError
from app.i18n.registry import default_registry
from app.i18n.resolver import AuthoritativeLocalePreferenceReader, LocaleResolver


@dataclass(frozen=True, slots=True, init=False)
class LocaleResponseFragment:
    schema_version: str
    locale_code: str
    pack_version: int
    content_hash: str
    message_key: str
    _expression: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        # The public constructor is deliberately unusable. A fragment is a
        # capability issued only by the finite-locale registry factory;
        # accepting caller-supplied hash/expression would forge trust metadata.
        raise LocalePackError("locale_response_fragment_factory_required")

    @property
    def expression(self) -> str:
        return self._expression

    @classmethod
    def for_locale(cls, locale_code: str) -> "LocaleResponseFragment":
        """Issue a fragment only for the canonical enabled registry entry.

        Callers provide a finite locale selector, never a pack or expression.
        The registry constructs and validates the canonical pack internally.
        """
        if not isinstance(locale_code, str):
            raise LocalePackError("invalid_locale_code")
        registry = default_registry()
        pack = registry.require(locale_code)
        expression = pack.messages["onboarding_ready"]
        if not 1 <= len(expression) <= 512 or "\x00" in expression:
            raise LocalePackError("invalid_locale_response_expression")
        obj = object.__new__(cls)
        object.__setattr__(obj, "schema_version", "locale-response-v1")
        object.__setattr__(obj, "locale_code", pack.locale_code)
        object.__setattr__(obj, "pack_version", pack.pack_version)
        object.__setattr__(obj, "content_hash", pack.content_hash)
        object.__setattr__(obj, "message_key", "onboarding_ready")
        object.__setattr__(obj, "_expression", expression)
        return obj

    @classmethod
    def from_pack(
        cls, pack: object, *, message_key: str = "onboarding_ready"
    ) -> "LocaleResponseFragment":
        """Deprecated forgeable entrypoint; never accepts caller-owned packs."""
        raise LocalePackError("locale_response_fragment_registry_binding_required")


async def resolve_locale_response_fragment(
    session: AsyncSession, principal: PrincipalContext, character_id: UUID
) -> LocaleResponseFragment:
    if not isinstance(principal, PrincipalContext) or not isinstance(character_id, UUID):
        raise LocalePackError("invalid_locale_response_fragment")
    await set_tenant_context(session, str(principal.tenant_id))
    resolver = LocaleResolver(
        AuthoritativeLocalePreferenceReader(session), registry=default_registry()
    )
    resolved = await resolver.resolve(principal.tenant_id, principal.user_id, character_id)
    return LocaleResponseFragment.for_locale(resolved.locale_code)
