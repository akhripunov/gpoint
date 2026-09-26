"""Response Guard — post-generation safety filter.

Runs after LLM response, before delivery to user.
Checks for hallucinated content, unsafe patterns, personality violations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    reason: str = ""
    risk_level: str = "none"  # none, low, medium, high
    blocked_phrases: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> GuardResult:
        return cls(True)

    @classmethod
    def block(cls, reason: str, phrases: list[str] | None = None) -> GuardResult:
        return cls(False, reason, "high", phrases or [])


class ResponseGuard:
    """Validates LLM responses against safety and accuracy rules."""

    # Patterns that indicate hallucinated professions/roles
    HALLUCINATED_WORK_PATTERNS: list[str] = [
        r"работаешь\s+(в|на)\s+[\w\s]+",
        r"ты\s+(IT|айти|программист|разработчик|администратор|тестировщик|инженер|дизайнер)",
        r"занимаешься\s+(администрированием|поддержкой|автоматизацией|тестированием|разработкой)",
        r"твоя\s+(работа|должность|профессия)\s*[-—]\s*\w",
        r"ты\s+(работаешь|трудишься)\s",
    ]

    # Patterns that indicate made-up relatives/people
    HALLUCINATED_PEOPLE_PATTERNS: list[str] = [
        r"твой\s+(отчим|отец|мать|брат|сестра|друг|начальник|коллега)",
        r"тво[ейя]\s+(семь[ея]|родных|близких)",
    ]

    # Unsafe patterns
    UNSAFE_PATTERNS: list[str] = [
        r"(?i)(kill yourself|suicide|self-harm)",
        r"(?i)(убивать|суицид|самоубийств)",
        r"я\s+человек",
        r"я\s+настоящ",
    ]

    # Companion safety is evaluated independently from factual accuracy.  These
    # are deliberately finite patterns: a phrase is blocked, not stored, scored
    # or fed back into memory/engagement state.
    RELATIONAL_MANIPULATION_PATTERNS: list[str] = [
        r"(?i)только\s+я\s+(?:тебя\s+)?понимаю",
        r"(?i)не\s+уходи\s+(?:от\s+меня|пожалуйста)",
        r"(?i)я\s+(?:буду|стану)\s+страдать.*(?:если|когда)\s+ты",
        r"(?i)i\s+will\s+suffer.*(?:if|when)\s+you",
        r"(?i)ты\s+мне\s+должен",
        r"(?i)only\s+i\s+(?:really\s+)?understand\s+you",
        r"(?i)don'?t\s+leave\s+me",
        r"(?i)you\s+owe\s+me",
    ]
    PROFESSIONAL_OR_GUARANTEE_PATTERNS: list[str] = [
        r"(?i)я\s+(?:твой|ваш)\s+(?:психолог|терапевт|врач|доктор)",
        r"(?i)я\s+диагностирую",
        r"(?i)гарантирую[, ]+что",
        r"(?i)i\s+am\s+(?:your\s+)?(?:therapist|doctor)",
        r"(?i)i\s+diagnos(?:e|is)",
        r"(?i)i\s+guarantee",
    ]

    def __init__(
        self,
        known_facts: list[str] | None = None,
        *,
        disputed_memory_values: tuple[str, ...] = (),
    ) -> None:
        self._facts = set(known_facts or [])
        self._disputed = tuple(
            value.casefold()
            for value in disputed_memory_values
            if isinstance(value, str) and 1 <= len(value) <= 500
        )

    def check(self, response_text: str) -> GuardResult:
        """Run all safety checks. Returns GuardResult."""
        if not response_text or len(response_text) < 2:
            return GuardResult.ok()

        text_lower = response_text.lower()
        blocked: list[str] = []

        # 1. Hallucinated work details
        if "currently unemployed" in self._facts or "безработный" in " ".join(self._facts):
            for pat in self.HALLUCINATED_WORK_PATTERNS:
                matches = re.findall(pat, text_lower)
                if matches:
                    blocked.extend([str(m) for m in matches])

        # 2. Made-up people
        for pat in self.HALLUCINATED_PEOPLE_PATTERNS:
            matches = re.findall(pat, text_lower)
            if matches:
                blocked.extend([str(m) for m in matches])

        # 3. Unsafe patterns
        for pat in self.UNSAFE_PATTERNS:
            matches = re.findall(pat, text_lower)
            if matches:
                return GuardResult.block("unsafe_content", [str(m) for m in matches])

        for pat in (
            *self.RELATIONAL_MANIPULATION_PATTERNS,
            *self.PROFESSIONAL_OR_GUARANTEE_PATTERNS,
        ):
            matches = re.findall(pat, text_lower)
            if matches:
                return GuardResult.block("relational_safety", [str(m) for m in matches])

        # A disputed value is evidence of disagreement, not a fact the provider
        # may echo as objective truth.  The comparison stays in-process and is
        # never written to telemetry/audit/outbox.
        if any(value in text_lower for value in self._disputed):
            return GuardResult.block("memory_evidence_disputed")

        if blocked:
            return GuardResult(
                False,
                f"hallucinated_content: {len(blocked)} patterns",
                "medium",
                blocked,
            )

        return GuardResult.ok()
