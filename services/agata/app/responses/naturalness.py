"""Frozen Naturalness V1 treatment for the existing AGATA response pipeline."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

POLICY_ID = "agata-naturalness-v1"
POLICY_VERSION = 1
POLICY_SHA256 = "b68d51e29b8611a053ac50bc89806bc8f1903f476126e155a7dc331927d94a6e"
EXAMPLES_SHA256 = "dcef7702d08657d3bc1ab50cc2670f4d99a21de321cb1c614e1472da98a889d6"
_ASSET_DIR = Path(__file__).parent


def _verified_text(path: Path, expected_sha256: str) -> str:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise RuntimeError("naturalness_v1_asset_hash_mismatch")
    return content.decode("utf-8")


@lru_cache(maxsize=1)
def _candidate_policy() -> str:
    text = _verified_text(_ASSET_DIR / "naturalness_v1_policy.md", POLICY_SHA256)
    start_marker = "## Candidate-visible policy\n"
    end_marker = "\n## Evidence map"
    if start_marker not in text or end_marker not in text:
        raise RuntimeError("naturalness_v1_policy_sections_invalid")
    candidate = text.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    if not candidate:
        raise RuntimeError("naturalness_v1_policy_empty")
    return candidate


@lru_cache(maxsize=1)
def _candidate_examples() -> tuple[tuple[str, str], ...]:
    text = _verified_text(_ASSET_DIR / "naturalness_v1_examples_source.jsonl", EXAMPLES_SHA256)
    examples: list[tuple[str, str]] = []
    try:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("naturalness_v1_examples_invalid") from exc
    if len(rows) != 15:
        raise RuntimeError("naturalness_v1_examples_count_invalid")
    for row in rows:
        turns = row.get("candidate_turns") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or row.get("provenance") != "synthetic_authored"
            or not isinstance(turns, list)
            or len(turns) != 2
            or not all(isinstance(turn, dict) for turn in turns)
            or turns[0].get("role") != "user"
            or turns[1].get("role") != "assistant"
            or not all(isinstance(turn.get("text"), str) and turn["text"] for turn in turns)
        ):
            raise RuntimeError("naturalness_v1_example_schema_invalid")
        # Deliberately discard review_only anti-patterns/rationales. They are
        # evaluator material and must never reach the generation provider.
        examples.append((turns[0]["text"], turns[1]["text"]))
    return tuple(examples)


@lru_cache(maxsize=1)
def build_naturalness_instructions() -> str:
    """Return the frozen shared style treatment, without Lab-only review data."""
    rendered_examples = "\n\n".join(
        f"User: {user}\nCharacter: {character}"
        for user, character in _candidate_examples()
    )
    return (
        f"AGATA Naturalness Policy V{POLICY_VERSION} ({POLICY_ID})\n\n"
        f"{_candidate_policy()}\n\n"
        "The following approved positive examples illustrate conversational wording only. "
        "They are not facts about this character, user, or relationship. Use them only when "
        "consistent with the current Persona, direct request, and system-authored safety and "
        "privacy boundaries; do not copy them mechanically.\n\n"
        f"{rendered_examples}"
    )
