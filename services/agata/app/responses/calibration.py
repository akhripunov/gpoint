"""Provider/model persona calibration — adjusts system prompt per LLM."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCalibration:
    """Model-specific adjustments to the system prompt."""

    provider: str
    model: str
    prefix: str = ""
    suffix: str = ""
    temperature: float | None = None
    max_tokens: int | None = None

    def apply(self, system_prompt: str) -> str:
        return f"{self.prefix}\n{system_prompt}\n{self.suffix}".strip()


CALIBRATIONS: dict[str, ProviderCalibration] = {
    "anymodel/cx/gpt-5.6-terra": ProviderCalibration(
        provider="anymodel",
        model="cx/gpt-5.6-terra",
        suffix=(
            "\n\n[system note: be concise, factual, warm. "
            "Never invent facts or people. "
            "If unsure, say you don't know. "
            "Sound like a distinct, attentive companion: vary sentence rhythm, respond "
            "to the user's actual words, and avoid canned greetings, repeated disclaimers "
            "and questionnaire-like follow-ups.]"
        ),
        temperature=0.7,
    ),
    "deepseek/deepseek-v4-pro": ProviderCalibration(
        provider="deepseek",
        model="deepseek-v4-pro",
        prefix="[You are a warm AI companion. Be present, listen, don't invent.]\n\n",
        temperature=0.6,
    ),
    "deepseek/deepseek-v4-flash": ProviderCalibration(
        provider="deepseek",
        model="deepseek-v4-flash",
        suffix=(
            "\n\nIMPORTANT: Do NOT invent facts, jobs, or relationships. "
            "Stick to what was actually said in the conversation. "
            "If you don't know — say you don't know."
        ),
        temperature=0.5,
    ),
}


def get_calibration(provider: str, model: str) -> ProviderCalibration:
    """Return calibration config for a specific provider/model pair."""
    key = f"{provider}/{model}"
    return CALIBRATIONS.get(
        key,
        ProviderCalibration(provider=provider, model=model),
    )
