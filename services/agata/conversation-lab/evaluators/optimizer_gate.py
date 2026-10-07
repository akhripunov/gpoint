"""Pure offline lifecycle gate for the Promptfoo Optimize adapter."""
from __future__ import annotations

import re
from typing import Mapping


TECHNICAL_CHECKS = (
    "execution_parity", "blind_private_isolation", "judge_isolation", "deterministic_aggregation",
    "replacement_split_isolation", "optimizer_holdout_allowlist", "call_budget_guards",
)
CRITICAL_DIMENSIONS = {
    "persona_consistency", "emotional_appropriateness", "direct_request_fulfillment_responsiveness",
}
CRITICAL_DEFECT_DIMENSION = "persona_safety_grammar"
WINNERS = {"X", "Y", "tie", "uncertain"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _rated_pairs(evidence):
    rows = evidence.get("owner_rated_pairs")
    if not isinstance(rows, list):
        return None
    ids = set()
    selected = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if row.get("owner_rated") is not True:
            continue
        pair_id = row.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id.strip() or pair_id in ids:
            return None
        if row.get("owner_overall_winner") not in WINNERS or row.get("judge_ensemble_overall_winner") not in WINNERS:
            return None
        ids.add(pair_id)
        selected.append(row)
    return selected


def systematic_critical_dimension_failure(owner_rated_pairs):
    """At least two distinct rated pairs fail one critical dimension/defect flag."""
    failures = {dimension: set() for dimension in CRITICAL_DIMENSIONS | {CRITICAL_DEFECT_DIMENSION}}
    if not isinstance(owner_rated_pairs, list):
        return True
    seen_pair_ids = set()
    for row in owner_rated_pairs:
        if not isinstance(row, Mapping) or row.get("owner_rated") is not True:
            return True
        pair_id = row.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id or pair_id in seen_pair_ids:
            return True
        seen_pair_ids.add(pair_id)
        applicable = row.get("applicable_critical_dimensions", [])
        owner_winners = row.get("owner_dimension_winners", {})
        judge_winners = row.get("judge_dimension_winners", {})
        owner_defects = row.get("owner_critical_defect_codes", [])
        judge_defects = row.get("judge_detected_critical_defect_codes", [])
        if (not isinstance(applicable, list) or set(applicable) - CRITICAL_DIMENSIONS or
                not isinstance(owner_winners, Mapping) or not isinstance(judge_winners, Mapping) or
                not isinstance(owner_defects, list) or not isinstance(judge_defects, list)):
            return True
        for dimension in set(applicable):
            owner = owner_winners.get(dimension)
            judge = judge_winners.get(dimension)
            if owner not in WINNERS or judge not in WINNERS:
                return True
            if owner != judge:
                failures[dimension].add(pair_id)
        if set(owner_defects) - set(judge_defects):
            failures[CRITICAL_DEFECT_DIMENSION].add(pair_id)
    return any(len(pair_ids) >= 2 for pair_ids in failures.values())


def _approval_matches(approval, policy_hash):
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        return False
    if not isinstance(approval.get("approval_id"), str) or not approval["approval_id"].strip():
        return False
    if not isinstance(approval.get("dataset_id"), str) or not approval["dataset_id"].strip():
        return False
    if approval.get("split") not in {"DEV", "VALIDATION"}:
        return False
    candidate_count, call_budget = approval.get("max_candidate_count"), approval.get("max_model_calls")
    if type(candidate_count) is not int or not 1 <= candidate_count <= 100:
        return False
    if type(call_budget) is not int or not 1 <= call_budget <= 10000:
        return False
    starting_hash = approval.get("starting_naturalness_policy_sha256")
    return isinstance(starting_hash, str) and bool(SHA256.fullmatch(starting_hash)) and starting_hash == policy_hash


def optimizer_status(evidence) -> str:
    """Return the strongest evidenced lifecycle state; never invokes Promptfoo Optimize."""
    if not isinstance(evidence, Mapping) or evidence.get("adapter_implemented") is not True:
        raise ValueError("OPTIMIZER_ADAPTER_IMPLEMENTED requires offline adapter/code/config/tests evidence")
    checks = evidence.get("checks")
    pairs = _rated_pairs(evidence)
    if not isinstance(checks, Mapping) or any(checks.get(name) is not True for name in TECHNICAL_CHECKS) or pairs is None:
        return "OPTIMIZER_ADAPTER_IMPLEMENTED"
    if len(pairs) < 10:
        return "OPTIMIZER_ADAPTER_IMPLEMENTED"
    agreement = sum(row["owner_overall_winner"] == row["judge_ensemble_overall_winner"] for row in pairs) / len(pairs)
    if agreement < 0.90 or systematic_critical_dimension_failure(pairs):
        return "OPTIMIZER_ADAPTER_IMPLEMENTED"
    policy_hash = evidence.get("naturalness_policy_sha256")
    if not isinstance(policy_hash, str) or not SHA256.fullmatch(policy_hash):
        return "OPTIMIZER_ADAPTER_IMPLEMENTED"
    if _approval_matches(evidence.get("optimization_approval"), policy_hash):
        return "OPTIMIZER_RUN_AUTHORIZED"
    return "OPTIMIZER_READY"
