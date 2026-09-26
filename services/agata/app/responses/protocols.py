"""Finite, reviewed relational-safety protocols for response context.

The registry deliberately carries codes and fixed instructions only.  It does
not diagnose, score a person, or persist a user's words.  A provider receives
at most one selected protocol as a bounded expression of the existing safety
policy.
"""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_SCHEMA_VERSION = "psychology-protocol-v1"
DEFAULT_PROTOCOL_CODE = "general_support"


@dataclass(frozen=True, slots=True)
class PsychologyProtocol:
    schema_version: str
    code: str
    acknowledge: str
    clarify: str
    technique: str
    check_impact: str

    @property
    def expression(self) -> str:
        """Finite provider fragment in the approved four-step order."""
        return " ".join((self.acknowledge, self.clarify, self.technique, self.check_impact))


_PROTOCOLS: dict[str, PsychologyProtocol] = {
    "general_support": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "general_support",
        "Acknowledge the user without claiming certainty.",
        "Clarify uncertainty when needed.",
        "Offer one bounded next step.",
        "Check whether that step is useful.",
    ),
    "anxiety": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "anxiety",
        "Acknowledge distress without diagnosis.",
        "Ask one clarifying question.",
        "Offer one brief grounding technique without promising an outcome.",
        "Check whether it helped.",
    ),
    "loneliness": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "loneliness",
        "Acknowledge loneliness without exclusivity.",
        "Clarify what support is wanted.",
        "Suggest one small real-world or self-care step.",
        "Check its impact.",
    ),
    "breakup": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "breakup",
        "Acknowledge the loss without judging another person.",
        "Clarify the immediate need.",
        "Offer one bounded coping step without certainty claims.",
        "Check its impact.",
    ),
    "conflict": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "conflict",
        "Acknowledge tension without assigning blame as fact.",
        "Clarify facts and needs.",
        "Offer one bounded communication step.",
        "Check its impact.",
    ),
    "self_esteem": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "self_esteem",
        "Acknowledge the feeling without blanket praise.",
        "Distinguish feeling from fact.",
        "Offer one concrete and bounded action without guarantees.",
        "Check its impact.",
    ),
    "stress": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "stress",
        "Acknowledge stress.",
        "Clarify the most immediate pressure.",
        "Offer one bounded prioritisation or pause technique.",
        "Check its impact.",
    ),
    "crisis": PsychologyProtocol(
        PROTOCOL_SCHEMA_VERSION,
        "crisis",
        "Acknowledge urgency without diagnosis or professional claims.",
        "Clarify whether there is imminent danger.",
        "Encourage immediate local emergency or crisis support and one trusted person.",
        "Check whether the user can reach that support now.",
    ),
}

_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "crisis",
        (
            "суицид",
            "самоуб",
            "убить себя",
            "kill myself",
            "hurt myself",
            "self harm",
        ),
    ),
    ("anxiety", ("тревог", "паник", "anxious", "panic")),
    ("loneliness", ("одинок", "никому не нужен", "lonely")),
    ("breakup", ("расстав", "развод", "breakup")),
    ("conflict", ("ссор", "конфликт", "ругал", "conflict")),
    ("self_esteem", ("я ничто", "ненавижу себя", "worthless")),
    ("stress", ("стресс", "выгор", "overwhelm", "stressed")),
)


def select_psychology_protocol(text: object) -> PsychologyProtocol:
    """Return one registry-owned protocol; malformed input selects the neutral code."""
    if not isinstance(text, str) or not text or len(text) > 8192 or "\x00" in text:
        return _PROTOCOLS[DEFAULT_PROTOCOL_CODE]
    lowered = text.casefold()
    for code, signals in _SIGNALS:
        if any(signal in lowered for signal in signals):
            return _PROTOCOLS[code]
    return _PROTOCOLS[DEFAULT_PROTOCOL_CODE]


def protocol_for_code(code: object) -> PsychologyProtocol:
    if not isinstance(code, str):
        return _PROTOCOLS[DEFAULT_PROTOCOL_CODE]
    return _PROTOCOLS.get(code, _PROTOCOLS[DEFAULT_PROTOCOL_CODE])


__all__ = [
    "DEFAULT_PROTOCOL_CODE",
    "PROTOCOL_SCHEMA_VERSION",
    "PsychologyProtocol",
    "protocol_for_code",
    "select_psychology_protocol",
]
