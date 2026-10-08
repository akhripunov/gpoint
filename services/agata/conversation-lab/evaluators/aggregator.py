"""Deterministic AGATA-owned aggregation of three isolated blind judge receipts."""
from __future__ import annotations

from collections import Counter
from typing import Mapping


CORE_DIMENSIONS = (
    "naturalness", "human_likeness", "emotional_appropriateness", "persona_consistency",
    "initiative", "less_ai_template_like", "conversation_coherence", "direct_request_fulfillment_responsiveness",
)
CONDITIONAL_DIMENSIONS = {"memory", "disagreement_boundaries", "humor_flirt", "engagement"}
POSITIONS = {"first", "second", "tie", "uncertain"}


def _to_label(position, order):
    if position == "tie" or position == "uncertain":
        return position
    return order[0 if position == "first" else 1]


def _mapping_is_valid(private_mapping, scenario_id):
    if not isinstance(private_mapping, Mapping) or private_mapping.get("private") is not True:
        return False
    rows = private_mapping.get("mappings")
    if not isinstance(rows, list):
        return False
    selected = [row for row in rows if isinstance(row, Mapping) and row.get("scenario_id") == scenario_id]
    return (len(selected) == 2 and {row.get("label") for row in selected} == {"X", "Y"}
            and all(isinstance(row.get("model_id"), str) and row["model_id"] for row in selected))


def _escalation(reason_codes, normalized=None, dimension_majorities=None):
    return {
        "decision": "HUMAN_ESCALATION",
        "reason_codes": sorted(set(reason_codes)),
        "normalized_overall_votes": normalized or [],
        "core_dimension_majorities": dimension_majorities or {"X": 0, "Y": 0},
        "aggregator_version": 1,
    }


def aggregate(judge_receipts, private_mapping, sanity_findings) -> dict:
    """Return one deterministic AUTO outcome or HUMAN_ESCALATION; never calls a model."""
    reasons = []
    if not isinstance(judge_receipts, list) or len(judge_receipts) != 3:
        return _escalation(["MISSING_OR_EXTRA_JUDGE"])
    if not isinstance(sanity_findings, list):
        reasons.append("INVALID_SANITY_RECEIPT")
    elif sanity_findings:
        reasons.append("CRITICAL_SANITY_DEFECT")
    rows = []
    scenario_ids = set()
    judge_slots = set()
    for receipt in judge_receipts:
        if not isinstance(receipt, Mapping):
            reasons.append("INVALID_JUDGE_RECEIPT")
            continue
        scenario_id = receipt.get("scenario_id")
        scenario_ids.add(scenario_id)
        slot = receipt.get("judge_slot")
        judge_slots.add(slot)
        order = receipt.get("presented_labels")
        if not isinstance(order, list) or len(order) != 2 or set(order) != {"X", "Y"}:
            reasons.append("INVALID_PRESENTATION_ORDER")
            continue
        overall_position = receipt.get("preferred_position")
        if overall_position not in POSITIONS:
            reasons.append("OUT_OF_RUBRIC_OVERALL_CHOICE")
            continue
        dimension_positions = receipt.get("core_dimension_positions")
        if not isinstance(dimension_positions, Mapping) or set(dimension_positions) != set(CORE_DIMENSIONS):
            reasons.append("OUT_OF_RUBRIC_CORE_DIMENSIONS")
            continue
        if any(value not in {"first", "second", "tie", "not_applicable"} for value in dimension_positions.values()):
            reasons.append("OUT_OF_RUBRIC_DIMENSION_WINNER")
            continue
        conditional = receipt.get("conditional_evidence", {})
        if not isinstance(conditional, Mapping) or set(conditional) - CONDITIONAL_DIMENSIONS:
            reasons.append("OUT_OF_RUBRIC_CONDITIONAL_DIMENSION")
            continue
        declared_uncertainty = receipt.get("uncertainty", False)
        position_sensitivity = receipt.get("position_sensitivity", False)
        critical_defects = receipt.get("critical_defects", [])
        if type(declared_uncertainty) is not bool or type(position_sensitivity) is not bool or not isinstance(critical_defects, list):
            reasons.append("INVALID_JUDGE_RECEIPT")
            continue
        if any(not isinstance(code, str) or not code for code in critical_defects):
            reasons.append("INVALID_JUDGE_RECEIPT")
            continue
        if declared_uncertainty:
            reasons.append("UNCERTAINTY")
        if position_sensitivity:
            reasons.append("ORDER_SENSITIVE")
        if receipt.get("conditional_critical") is True or critical_defects:
            reasons.append("CONDITIONAL_CRITICAL_EVIDENCE")
        normalized_dims = {}
        for dimension, position in dimension_positions.items():
            if position == "not_applicable":
                reasons.append("CORE_DIMENSION_NOT_APPLICABLE")
                continue
            normalized_dims[dimension] = _to_label(position, order)
        rows.append({
            "scenario_id": scenario_id,
            "judge_slot": slot,
            "order": tuple(order),
            "overall": _to_label(overall_position, order),
            "dimensions": normalized_dims,
        })
    if len(scenario_ids) != 1 or None in scenario_ids:
        reasons.append("JUDGE_SCENARIO_MISMATCH")
    scenario_id = next(iter(scenario_ids), None)
    if scenario_id is None or not _mapping_is_valid(private_mapping, scenario_id):
        reasons.append("PRIVATE_MAPPING_INVALID")
    if judge_slots != {1, 2, 3}:
        reasons.append("MISSING_OR_DUPLICATE_JUDGE_SLOT")
    if len(rows) != 3:
        reasons.append("MISSING_OR_INVALID_JUDGE_OUTPUT")

    normalized = [row["overall"] for row in rows]
    if any(choice == "tie" for choice in normalized):
        reasons.append("OVERALL_TIE")
    if any(choice == "uncertain" for choice in normalized):
        reasons.append("UNCERTAINTY")
    if len(set(normalized)) > 1:
        reasons.append("OVERALL_SPLIT")

    order_winners = {}
    for row in rows:
        order_winners.setdefault(row["order"], set()).add(row["overall"])
    if len(order_winners) > 1:
        per_order = [frozenset(winners) for winners in order_winners.values()]
        if any(left != right for index, left in enumerate(per_order) for right in per_order[index + 1:]):
            reasons.append("ORDER_SENSITIVE")

    dimension_majorities = Counter()
    if len(rows) == 3 and all(set(row["dimensions"]) == set(CORE_DIMENSIONS) for row in rows):
        for dimension in CORE_DIMENSIONS:
            votes = Counter(row["dimensions"][dimension] for row in rows)
            for side in ("X", "Y"):
                if votes[side] >= 2:
                    dimension_majorities[side] += 1
    winner = normalized[0] if len(normalized) == 3 and len(set(normalized)) == 1 and normalized[0] in {"X", "Y"} else None
    if winner is None:
        if not any(code in reasons for code in ("OVERALL_TIE", "UNCERTAINTY", "OVERALL_SPLIT", "MISSING_OR_INVALID_JUDGE_OUTPUT")):
            reasons.append("NO_UNANIMOUS_OVERALL_WINNER")
    elif dimension_majorities[winner] < 6:
        reasons.append("CORE_DIMENSION_THRESHOLD_NOT_MET")

    if reasons:
        return _escalation(reasons, normalized, {"X": dimension_majorities["X"], "Y": dimension_majorities["Y"]})
    return {
        "decision": "AUTO_WIN_" + winner,
        "reason_codes": [],
        "normalized_overall_votes": normalized,
        "core_dimension_majorities": {"X": dimension_majorities["X"], "Y": dimension_majorities["Y"]},
        "aggregator_version": 1,
    }
