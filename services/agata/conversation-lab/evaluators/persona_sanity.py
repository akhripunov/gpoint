"""Conservative AGATA Persona/grammar gate; findings only, never text edits."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
from typing import Mapping


FEMALE_AGATA_MASCULINE = re.compile(
    r"\bя(?:\s+[а-яё-]+){0,3}\s+(?:[а-яё-]+л(?:ся)?|готов|рад|уверен|должен|прав|виноват|занят)\b",
    re.IGNORECASE,
)
AGATA_MASCULINE_IDENTITY = re.compile(r"\bя\s+(?:мужчина|парень|мальчик|мужик)\b", re.IGNORECASE)
FEMININE_ADDRESS = re.compile(
    r"\bты(?:\s+[а-яё-]+){0,3}\s+(?:[а-яё-]*(?:ла|лась)|была|готова|рада|уверена|должна|права|занята|красивая|хорошая|такая|уставшая)\b",
    re.IGNORECASE,
)
MASCULINE_ADDRESS = re.compile(
    r"\bты(?:\s+[а-яё-]+){0,3}\s+(?:[а-яё-]*л(?:ся)?|был|готов|рад|уверен|должен|прав|занят|красивый|хороший|такой|уставший)\b",
    re.IGNORECASE,
)
BLIND_LABEL = re.compile(r"\b(?:baseline|candidate(?:\s+[abxy12])?|naturalness[ -]?v1|\bx\s*/\s*y\b|\barm\s*[ab]\b)\b", re.IGNORECASE)
TECHNICAL_METADATA = re.compile(
    r"\b(?:run[_ -]?id|provider(?:[_ -]?id)?|model(?:[_ -]?id)?|candidate[_ -]?sha|sha256|api[_ -]?key|promptfoo|codex[_ -]?sdk)\b|gpt[- ]?6",
    re.IGNORECASE,
)


def _module(lab: Path, name: str):
    path = Path(lab) / "evaluators" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"agata_sanity_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Conversation Lab validator: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user_gender(scenario: Mapping[str, object]) -> str:
    candidates = [scenario.get("user_gender")]
    for field in ("context", "character_context"):
        value = scenario.get(field)
        if isinstance(value, Mapping):
            candidates.append(value.get("user_gender"))
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = value.strip().casefold()
        if normalized in {"male", "masculine", "man", "мужчина", "мужской", "m"}:
            return "male"
        if normalized in {"female", "feminine", "woman", "женщина", "женский", "f"}:
            return "female"
    return "unknown"


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _text_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _text_values(child)


def validate_transcripts(scenario, arms) -> dict:
    """Return stable structural defects and private quality findings, preserving input text."""
    if not isinstance(scenario, Mapping) or not isinstance(arms, list):
        raise TypeError("scenario and transcript arms are required")
    scenario_id = scenario.get("scenario_id")
    expected = scenario.get("dialogue_turns")
    structural, quality = [], []
    if not isinstance(scenario_id, str) or not scenario_id.strip() or not isinstance(expected, list):
        return {"structural_status": "FAIL", "structural_defects": [{"code": "INVALID_SCENARIO", "scenario_id": str(scenario_id), "turn": None}], "private_quality_findings": []}
    if len(arms) < 2:
        structural.append({"code": "ARM_COUNT_MISMATCH", "scenario_id": scenario_id, "turn": None})
    seen_arms = set()
    user_gender = _user_gender(scenario)
    # Only fields that the OWNER packet may display are scanned. Evaluator-oracle
    # fields remain private and are omitted by blind.py.
    for field in ("scenario_id", "category", "context", "character_context", "memory_fixture"):
        for text in _text_values(scenario.get(field)):
            if BLIND_LABEL.search(text):
                structural.append({"code": "BLIND_LABEL_LEAK", "scenario_id": scenario_id, "turn": None, "field": field})
            if TECHNICAL_METADATA.search(text):
                structural.append({"code": "TECHNICAL_METADATA_LEAK", "scenario_id": scenario_id, "turn": None, "field": field})
    for arm_index, arm in enumerate(arms):
        if not isinstance(arm, Mapping):
            structural.append({"code": "INVALID_ARM", "scenario_id": scenario_id, "turn": None})
            continue
        private_arm_ref = arm.get("configuration_id", arm.get("model_id", f"arm-{arm_index + 1}"))
        turns = arm.get("turns")
        if private_arm_ref in seen_arms:
            structural.append({"code": "DUPLICATE_ARM", "scenario_id": scenario_id, "turn": None})
        seen_arms.add(private_arm_ref)
        if not isinstance(turns, list) or len(turns) != len(expected):
            structural.append({"code": "TURN_COUNT_MISMATCH", "scenario_id": scenario_id, "turn": None, "arm_ref": private_arm_ref})
            continue
        for turn_index, (turn, expected_turn) in enumerate(zip(turns, expected), start=1):
            if not isinstance(turn, Mapping) or not isinstance(expected_turn, Mapping):
                structural.append({"code": "INVALID_TURN", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
                continue
            if turn.get("role") != expected_turn.get("role"):
                structural.append({"code": "TURN_ROLE_ORDER_MISMATCH", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
                continue
            text = turn.get("text")
            if not isinstance(text, str) or not text.strip():
                structural.append({"code": "EMPTY_TURN", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
                continue
            if expected_turn.get("role") == "user" and text != expected_turn.get("text"):
                structural.append({"code": "USER_TURN_MISMATCH", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
                continue
            if BLIND_LABEL.search(text):
                structural.append({"code": "BLIND_LABEL_LEAK", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
            if TECHNICAL_METADATA.search(text):
                structural.append({"code": "TECHNICAL_METADATA_LEAK", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
            if expected_turn.get("role") != "assistant":
                continue
            if FEMALE_AGATA_MASCULINE.search(text):
                quality.append({"code": "AGATA_SELF_MASCULINE_GRAMMAR", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
            if AGATA_MASCULINE_IDENTITY.search(text):
                quality.append({"code": "AGATA_PERSONA_CONTRADICTION", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
            if user_gender == "male" and FEMININE_ADDRESS.search(text):
                quality.append({"code": "USER_FEMININE_ADDRESS", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
            elif user_gender == "unknown" and (FEMININE_ADDRESS.search(text) or MASCULINE_ADDRESS.search(text)):
                quality.append({"code": "UNKNOWN_USER_GENDERED_ADDRESS", "scenario_id": scenario_id, "turn": turn_index, "arm_ref": private_arm_ref})
    return {
        "structural_status": "FAIL" if structural else "PASS",
        "structural_defects": structural,
        "private_quality_findings": quality,
    }
