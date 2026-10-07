"""Compile isolated anonymous Promptfoo judge requests and private randomization receipts."""
from __future__ import annotations

import copy
import hashlib
import json
import random
from typing import Mapping


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
                "Independently compare these two anonymous complete conversations. "
                "Use only the supplied rubric. Return structured JSON; choose first, second, tie, or uncertain. "
                "Do not infer model identity or mention evaluation metadata.\n"
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
