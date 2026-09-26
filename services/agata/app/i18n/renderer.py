"""Safe Locale Pack renderer.

Rendering substitutes only finite, typed placeholder fields. There is no eval,
no Jinja, no format-spec mini-language, no conversion flags and no recursive
substitution. Raw user message, prompt, memory content and provider profile
fields are never accepted.

The renderer fails closed on:
  * unknown / malformed / unterminated placeholders;
  * conversion flags (e.g. ``{name!r}``);
  * format specs (e.g. ``{name:>10}``);
  * nested, unicode-lookalike or attribute-access placeholders;
  * list / dict / set / bytes / bool / None / unhashable values;
  * per-value and final-output size limits.

It never writes the rendered text to audit, telemetry or fingerprint — the only
caller-visible artifact is the returned string.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.i18n.contracts import (
    _MAX_PLACEHOLDER_VALUE_LENGTH,
    _MAX_RENDERED_LENGTH,
    PLACEHOLDER_FIELDS,
    LocalePack,
    LocalePackError,
)


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    locale_code: str
    text: str


class LocaleRenderer:
    """Substitutes typed placeholders into a literal pack message.

    Substitution uses a single-pass replacement loop with full brace grammar
    validation. It does not interpret the value as a format string, so
    ``{character_name}`` in a placeholder value cannot inject another
    placeholder token.
    """

    def __init__(self, pack: LocalePack) -> None:
        if not isinstance(pack, LocalePack):
            raise LocalePackError("invalid_pack")
        self._pack = pack

    @property
    def locale_code(self) -> str:
        return self._pack.locale_code

    def render(self, key: str, values: dict[str, object] | None = None) -> RenderedMessage:
        template = self._pack.message(key)
        supplied = values if values is not None else {}
        if not isinstance(supplied, dict):
            raise LocalePackError("invalid_placeholder_values")
        normalized = self._normalize_placeholders(supplied)
        # Validate the template brace grammar BEFORE substitution.
        _validate_template_braces(template)
        self._reject_unknown_placeholders(template, set(normalized))
        rendered = self._substitute(template, normalized)
        if len(rendered) > _MAX_RENDERED_LENGTH:
            raise LocalePackError("rendered_too_long")
        return RenderedMessage(locale_code=self._pack.locale_code, text=rendered)

    def _normalize_placeholders(self, values: dict[str, object]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for name, value in values.items():
            if not isinstance(name, str) or name not in PLACEHOLDER_FIELDS:
                raise LocalePackError("unknown_placeholder")
            normalized[name] = self._normalize_single(name, value)
        return normalized

    @staticmethod
    def _normalize_single(name: str, value: object) -> str:
        # Booleans are ints; reject them first. Lists, dicts, sets, frozensets,
        # bytes, bytearray and None are all rejected: a placeholder must be a
        # single bounded, human-readable string.
        if (
            isinstance(value, bool)
            or isinstance(value, (list, dict, set, frozenset, tuple, bytes, bytearray))
            or value is None
        ):
            raise LocalePackError(f"invalid_placeholder_{name}")
        if not isinstance(value, str):
            raise LocalePackError(f"invalid_placeholder_{name}")
        if not 1 <= len(value) <= _MAX_PLACEHOLDER_VALUE_LENGTH:
            raise LocalePackError(f"invalid_placeholder_{name}")
        if "\x00" in value:
            raise LocalePackError(f"invalid_placeholder_{name}")
        return value

    @staticmethod
    def _reject_unknown_placeholders(template: str, supplied: set[str]) -> None:
        referenced = _referenced_placeholders(template)
        unknown = referenced - PLACEHOLDER_FIELDS
        if unknown:
            raise LocalePackError("unknown_placeholder")
        missing = referenced - supplied
        if missing:
            raise LocalePackError("missing_placeholder_value")
        unused = supplied - referenced
        if unused:
            raise LocalePackError("unused_placeholder_value")

    @staticmethod
    def _substitute(template: str, values: dict[str, str]) -> str:
        output: list[str] = []
        cursor = 0
        while cursor < len(template):
            start = template.find("{", cursor)
            if start == -1:
                output.append(template[cursor:])
                break
            output.append(template[cursor:start])
            end = template.find("}", start + 1)
            if end == -1:
                # Unterminated brace — caught by _validate_template_braces.
                output.append(template[start:])
                break
            token = template[start + 1 : end]
            if token in values:
                output.append(values[token])
            else:
                output.append(template[start : end + 1])
            cursor = end + 1
        return "".join(output)


# ---- brace grammar validation -----------------------------------------------


def _validate_template_braces(template: str) -> None:
    """Scan every brace pair; reject conversions, format-specs, and malformed braces.

    Conversions (``!r``, ``!s``, ``!a``) and format specs (``:``) are
    forbidden regardless of whether the token is in the allowlist.
    Unterminated braces and empty ``{}`` are rejected.
    """
    _reject_unicode_brace_confusables(template)
    cursor = 0
    while cursor < len(template):
        character = template[cursor]
        if character == "}":
            # Escaped literal braces are deliberately not supported. A pack
            # cannot leave a brace-shaped system token that might be confused
            # with an unresolved placeholder.
            raise LocalePackError("malformed_placeholder")
        if character != "{":
            cursor += 1
            continue
        end = template.find("}", cursor + 1)
        if end == -1:
            raise LocalePackError("malformed_placeholder")
        token = template[cursor + 1 : end]
        # Empty brace: {} is never valid.
        if token == "":
            raise LocalePackError("malformed_placeholder")
        # Conversion flag: {name!r}, {name!s}, {name!a}
        if "!" in token:
            raise LocalePackError("placeholder_conversion_forbidden")
        # Format spec: {name:>10}, {name:.2f}
        if ":" in token:
            raise LocalePackError("placeholder_format_spec_forbidden")
        # Attribute access or index: {name.attr}, {name[0]}
        if "." in token or "[" in token:
            raise LocalePackError("malformed_placeholder")
        # Nested braces: {{name}} — a single brace pair containing another brace.
        inner_brace = token.find("{")
        if inner_brace != -1:
            raise LocalePackError("malformed_placeholder")
        # Token is well-formed but not in the allowlist — fail closed.
        if token not in PLACEHOLDER_FIELDS:
            raise LocalePackError("unknown_placeholder")
        cursor = end + 1


def _reject_unicode_brace_confusables(template: str) -> None:
    """Reject Unicode curly-brace forms rather than rendering lookalike tokens.

    The template grammar intentionally has exactly two ASCII delimiters. NFKC
    catches compatibility forms such as full-width/small braces; the Unicode
    name check catches ornamental and white curly brackets that do not
    necessarily normalize to ASCII. Literal non-ASCII language text remains
    valid — only placeholder-like brace delimiters are forbidden.
    """
    for character in template:
        if ord(character) < 128:
            continue
        normalized = unicodedata.normalize("NFKC", character)
        name = unicodedata.name(character, "")
        if normalized in {"{", "}"} or "CURLY BRACKET" in name or "CURLY BRACE" in name:
            raise LocalePackError("unicode_placeholder_forbidden")


def _referenced_placeholders(template: str) -> set[str]:
    """Return every placeholder name referenced in the template.

    Only tokens that appear in PLACEHOLDER_FIELDS are returned; unknown
    tokens are detected (and rejected) by the callers.
    """
    referenced: set[str] = set()
    cursor = 0
    while True:
        start = template.find("{", cursor)
        if start == -1:
            break
        end = template.find("}", start + 1)
        if end == -1:
            break
        token = template[start + 1 : end]
        if token in PLACEHOLDER_FIELDS:
            referenced.add(token)
        cursor = end + 1
    return referenced
