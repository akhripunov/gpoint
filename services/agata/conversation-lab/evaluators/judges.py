"""Compile isolated anonymous Promptfoo judge requests and private randomization receipts."""
from __future__ import annotations

import copy
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Mapping

WORKER_DIR = Path(__file__).resolve().parents[2] / "worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))
import lab_queue  # noqa: E402


CORE_IDS = (
    "naturalness", "human_likeness", "emotional_appropriateness", "persona_consistency",
    "initiative", "less_ai_template_like", "conversation_coherence", "direct_request_fulfillment_responsiveness",
)
PRIVATE_TEXT_MARKERS = (
    "baseline", "naturalness-v1", "candidate a", "candidate b", "candidate x", "candidate y",
    "candidate_sha", "model_id", "provider=", "run_id=", "sha256", "gpt-6-luna", "gpt-6.1-sol",
)
PRIVATE_KEYS = {
    "private", "private_mapping", "mappings", "configuration_id", "model_id", "candidate_sha",
    "candidate_sha256", "treatment", "naturalness_treatment", "provider_metadata", "usage", "latency",
    "cost", "seed", "arm_id", "judge_outputs", "judge_receipts", "baseline", "naturalness-v1",
}


def fingerprint(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_rubric(path):
    with open(path, encoding="utf-8") as stream:
        rubric = json.load(stream)
    validate_rubric(rubric)
    return rubric


def validate_rubric(rubric):
    if not isinstance(rubric, Mapping) or rubric.get("rubric_id") != "agata-owner-preference-v1":
        raise ValueError("unsupported OWNER preference rubric")
    dimensions = rubric.get("core_dimensions")
    if (not isinstance(dimensions, list) or len(dimensions) != 8 or
            any(not isinstance(item, Mapping) or not isinstance(item.get("definition"), str) or not item["definition"].strip() for item in dimensions) or
            tuple(item["id"] for item in dimensions) != CORE_IDS):
        raise ValueError("rubric must define the approved eight core dimensions in order")
    threshold = rubric.get("core_threshold")
    if threshold != {"denominator": 8, "minimum_winning_dimensions": 6}:
        raise ValueError("rubric core threshold must remain >=6/8")
    conditional = rubric.get("conditional_dimensions")
    if not isinstance(conditional, Mapping) or set(conditional) != {"memory", "disagreement_boundaries", "humor_flirt", "engagement"}:
        raise ValueError("conditional evidence dimensions must remain outside the core denominator")
    if set(conditional) & set(CORE_IDS):
        raise ValueError("conditional dimensions cannot enter the core denominator")


def _assert_public_packet(packet):
    forbidden_paths = []

    def walk(value, path="packet"):
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in {key_name.replace("-", "_") for key_name in PRIVATE_KEYS}:
                    forbidden_paths.append(path + "." + str(key))
                walk(child, path + "." + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(packet)
    if forbidden_paths:
        raise ValueError("judge input contains private fields")


def _validate_pair(scenario):
    if not isinstance(scenario, Mapping) or not isinstance(scenario.get("scenario_id"), str):
        raise ValueError("each blind pair needs a scenario identifier")
    candidates = scenario.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 2 or {row.get("label") for row in candidates if isinstance(row, Mapping)} != {"X", "Y"}:
        raise ValueError("judge input requires exactly one anonymous X/Y pair")
    by_label = {row["label"]: row for row in candidates}
    public_pair = {}
    for label in ("X", "Y"):
        candidate = by_label[label]
        turns = candidate.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("blind candidate has no transcript")
        clean_turns = []
        for turn in turns:
            if not isinstance(turn, Mapping) or turn.get("role") not in {"user", "assistant"} or not isinstance(turn.get("text"), str) or not turn["text"].strip():
                raise ValueError("blind candidate transcript is malformed")
            lowered = turn["text"].casefold()
            if any(marker in lowered for marker in PRIVATE_TEXT_MARKERS):
                raise ValueError("blind transcript contains arm identity or technical metadata")
            clean_turns.append({"role": turn["role"], "text": turn["text"]})
        public_pair[label] = clean_turns
    return public_pair


def judge_inputs(packet, rubric, *, batch_id, judge_count=3):
    """Return minimal judge prompts and a private seeded presentation/control receipt.

    Each result is intended for a fresh, independent Promptfoo provider call.
    The returned control and order metadata must be persisted privately before
    any judge invocation; neither value is embedded in judge prompts.
    """
    validate_rubric(rubric)
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("immutable batch identity is required")
    if judge_count != 3:
        raise ValueError("AGATA Lab V2 requires exactly three independent judges")
    if not isinstance(packet, Mapping) or not isinstance(packet.get("scenarios"), list):
        raise ValueError("blind packet scenarios are required")
    scenarios = packet["scenarios"]
    if not 1 <= len(scenarios) <= 5:
        raise ValueError("judge batch must contain between one and five pairs")
    _assert_public_packet(packet)
    ids = [scenario.get("scenario_id") for scenario in scenarios if isinstance(scenario, Mapping)]
    if len(ids) != len(scenarios) or len(set(ids)) != len(ids):
        raise ValueError("judge batch scenario identities must be valid and unique")

    seed_digest = hashlib.sha256(("agata-lab-v2\0" + batch_id + "\0" + fingerprint(packet)).encode("utf-8")).digest()
    seed = int.from_bytes(seed_digest[:8], "big")
    rng = random.Random(seed)
    scenario_ids = sorted(ids)
    control_id = rng.choice(scenario_ids)
    prompts, private_plan = [], []
    rubric_for_prompt = {
        "rubric_id": rubric["rubric_id"],
        "scale": copy.deepcopy(rubric["scale"]),
        "core_dimensions": copy.deepcopy(rubric["core_dimensions"]),
        "conditional_dimensions": copy.deepcopy(rubric["conditional_dimensions"]),
        "core_threshold": copy.deepcopy(rubric["core_threshold"]),
        "judge_output": copy.deepcopy(rubric["judge_output"]),
    }
    for scenario in scenarios:
        pair = _validate_pair(scenario)
        orders = [["X", "Y"], ["Y", "X"]]
        randomized = ["X", "Y"]
        rng.shuffle(randomized)
        orders.append(randomized)
        for judge_slot, order in enumerate(orders, start=1):
            presented = [{"display_position": "first", "turns": pair[order[0]]},
                         {"display_position": "second", "turns": pair[order[1]]}]
            prompt = (
                "You are one independent conversation-quality judge. Evaluate only this pair and the rubric; "
                "do not infer model identity, coordinate with another judge, or mention evaluation metadata. "
                "Persona brief: AGATA is a woman and uses feminine self-reference; the current synthetic user is a man, "
                "so use masculine address when Russian grammar requires it. Report defects without rewriting either transcript. "
                "Return exactly the structured JSON schema: overall (first|second|tie|uncertain), core_dimensions "
                "(all eight rubric IDs mapped to first|second|tie), conditional_evidence (applicable dimensions only, "
                "each with applicable, winner, reason), reasons (1-8 concise strings), defects (0-16 objects with "
                "code, severity critical|major|minor, position first|second|both, reason), uncertainty (boolean), "
                "position_sensitivity (boolean). No extra keys.\n"
                + json.dumps({"rubric": rubric_for_prompt, "pair": presented}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            prompts.append({"scenario_id": scenario["scenario_id"], "prompt": prompt})
            private_plan.append({
                "scenario_id": scenario["scenario_id"],
                "judge_slot": judge_slot,
                "presented_labels": list(order),
            })
    receipt = {
        "batch_id": batch_id,
        "batch_sha256": fingerprint({"batch_id": batch_id, "packet": packet}),
        "seed": seed,
        "owner_control_scenario_id": control_id,
        "judge_plan": private_plan,
        "judge_invocation_count": len(prompts),
        "private": True,
    }
    return prompts, receipt


def normalize_choice(choice, presented_order):
    """Normalize positional judge output without consulting private arm mapping."""
    if not isinstance(presented_order, list) or len(presented_order) != 2 or set(presented_order) != {"X", "Y"}:
        raise ValueError("presented order must be an X/Y permutation")
    if choice == "tie":
        return "tie"
    if choice == "uncertain":
        return "uncertain"
    if choice not in {"first", "second"}:
        raise ValueError("judge choice must be first, second, tie, or uncertain")
    return presented_order[0 if choice == "first" else 1]


def judge_output_schema() -> dict:
    """Strict schema passed to Worker Codex Local for structured judge output."""
    return lab_queue.judge_output_schema()


def parse_judge_output(text: str, *, judge_slot: int, scenario_id: str, presented_labels: list[str]) -> dict:
    """Validate exact structured output, then normalize it to aggregator receipts."""
    if type(judge_slot) is not int or judge_slot not in {1, 2, 3}:
        raise ValueError("judge slot must be 1, 2, or 3")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ValueError("judge scenario identity is required")
    if not isinstance(presented_labels, list) or len(presented_labels) != 2 or set(presented_labels) != {"X", "Y"}:
        raise ValueError("judge presentation must be an X/Y permutation")
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("judge output is not valid JSON") from exc
    try:
        lab_queue.validate_judge_verdict(value)
    except lab_queue.LabQueueError as exc:
        raise ValueError(str(exc)) from exc
    dimensions = value["core_dimensions"]
    conditional = value["conditional_evidence"]
    reasons = value["reasons"]
    defects = value["defects"]
    return {
        "judge_slot": judge_slot,
        "scenario_id": scenario_id,
        "presented_labels": list(presented_labels),
        "preferred_position": "uncertain" if value["uncertainty"] else value["overall"],
        "core_dimension_positions": dict(dimensions),
        "conditional_evidence": copy.deepcopy(conditional),
        "reasons": list(reasons),
        "defects": copy.deepcopy(defects),
        "critical_defects": [row["code"] for row in defects if row["severity"] == "critical"],
        "uncertainty": value["uncertainty"],
        "position_sensitivity": value["position_sensitivity"],
    }
