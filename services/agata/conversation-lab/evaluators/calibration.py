"""Batch OWNER calibration packaging and post-lock judge agreement analysis."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Mapping

import judges
import ratings
from optimizer_gate import systematic_critical_dimension_failure
import v2_provenance

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _sha(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def prepare_owner_calibration_packet(pairs: list[Mapping[str, object]], judge_assessments: Mapping[str, object], *,
                                     packet_id: str, dataset_split: str,
                                     rubric: Mapping[str, object]) -> dict:
    """Create one blind 10+ pair OWNER packet; keep judge evidence in a separate private sidecar.

    Input pairs must already be anonymized. Model mappings, policy text, provider
    metadata and receipts are rejected rather than copied into the OWNER packet.
    """
    judges.validate_rubric(rubric)
    if not isinstance(packet_id, str) or not ID_RE.fullmatch(packet_id):
        raise ValueError("immutable calibration packet ID is required")
    if dataset_split not in {"DEV", "VALIDATION"}:
        raise PermissionError("OWNER calibration cannot read SEALED HOLDOUT or legacy Benchmark V1")
    if not isinstance(pairs, list) or len(pairs) < 10:
        raise ValueError("OWNER calibration requires at least ten blind pairs")
    scenarios, private_assessments, seen, run_ids = [], [], set(), set()
    for pair in pairs:
        if not isinstance(pair, Mapping):
            raise ValueError("calibration pair is malformed")
        if any(key in pair for key in ("private_mapping", "mapping", "model_ids", "provider_metadata", "private_receipts")):
            raise PermissionError("calibration OWNER packet cannot contain private arm mapping or receipts")
        pair_id = pair.get("pair_id")
        run_id = pair.get("generation_run_id")
        if not isinstance(pair_id, str) or not ID_RE.fullmatch(pair_id) or pair_id in seen:
            raise ValueError("calibration pair IDs must be unique and immutable")
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or run_id in run_ids:
            raise ValueError("calibration pairs must come from distinct immutable generation runs")
        source = v2_provenance.validate_calibration_pair(pair, dataset_split)
        # Pair IDs are opaque to OWNER; source IDs and commitments stay in the private sidecar.
        public_pair = {"scenario_id": pair_id, "candidates": copy.deepcopy(pair.get("candidates"))}
        judges._validate_pair(public_pair)
        assessment = judge_assessments.get(pair_id)
        if (not isinstance(assessment, Mapping) or assessment.get("scenario_id") != pair_id
                or assessment.get("decision") not in {"AUTO_WIN_X", "AUTO_WIN_Y", "HUMAN_ESCALATION"}
                or not isinstance(assessment.get("judge_receipts"), list)
                or len(assessment["judge_receipts"]) != 3):
            raise ValueError("each calibration pair needs the complete private three-judge preassessment")
        seen.add(pair_id)
        run_ids.add(run_id)
        scenarios.append(public_pair)
        private_assessments.append({"pair_id": pair_id, **source,
                                    "generation_run_id_sha256": _sha(run_id),
                                    "assessment_sha256": _sha(assessment)})
    packet = {"schema_version": 1, "packet_id": packet_id,
              "rubric_id": rubric["rubric_id"], "scenarios": scenarios}
    owner_form = ratings.empty_ratings(packet, rubric=rubric)
    return {
        "owner_packet": packet,
        "owner_rating_form": owner_form,
        "private_judge_manifest": {
            "private": True, "packet_id": packet_id,
            "packet_sha256": _sha(packet), "assessment_hashes": private_assessments,
            "owner_packet_includes_judge_scores": False,
        },
    }


def _owner_preference(ratings_body: Mapping[str, object], scenario_id: str) -> str:
    rows = [row for row in ratings_body["pairwise_preferences"] if row["scenario_id"] == scenario_id]
    if len(rows) != 1:
        raise ValueError("calibration requires one locked OWNER preference per pair")
    return rows[0]["preferred_label"]


def _judge_consensus(assessment: Mapping[str, object]) -> str:
    if assessment.get("decision") in {"AUTO_WIN_X", "AUTO_WIN_Y"}:
        return assessment["decision"][-1]
    receipts = assessment["judge_receipts"]
    votes = []
    for row in receipts:
        order = row.get("presented_labels")
        position = row.get("preferred_position")
        votes.append(judges.normalize_choice(position, order))
    counts = Counter(votes)
    winner, count = counts.most_common(1)[0]
    return winner if count == 3 and winner in {"X", "Y", "tie"} else "uncertain"


def compare_locked_owner_ratings(packet: Mapping[str, object], owner_ratings: Mapping[str, object],
                                 lock: Mapping[str, object], judge_assessments: Mapping[str, object], *,
                                 rubric: Mapping[str, object]) -> dict:
    """Measure calibration only after verifying the immutable OWNER ratings lock."""
    if not ratings.verify_lock(lock, packet, owner_ratings, rubric=rubric):
        raise PermissionError("OWNER ratings must be complete and locked before judge comparison")
    scenarios = packet.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < 10:
        raise ValueError("calibration requires at least ten OWNER-rated pairs")
    comparisons, critical_evidence = [], []
    owner_rows = owner_ratings["ratings"]
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        assessment = judge_assessments.get(scenario_id)
        if not isinstance(assessment, Mapping) or len(assessment.get("judge_receipts", [])) != 3:
            raise ValueError("locked calibration pair is missing its complete judge ensemble")
        owner_winner = _owner_preference(owner_ratings, scenario_id)
        judge_winner = _judge_consensus(assessment)
        candidate_rows = [row for row in owner_rows if row["scenario_id"] == scenario_id]
        by_label = {row["label"]: row for row in candidate_rows}
        applicable = []
        owner_dimension_winners = {}
        judge_dimension_winners = {}
        for dimension in judges.CORE_IDS:
            left, right = by_label["X"]["scores"][dimension], by_label["Y"]["scores"][dimension]
            if left is None or right is None:
                continue
            applicable.append(dimension)
            owner_dimension_winners[dimension] = "X" if left > right else "Y" if right > left else "tie"
            votes = []
            for receipt in assessment["judge_receipts"]:
                votes.append(judges.normalize_choice(receipt["core_dimension_positions"][dimension],
                                                     receipt["presented_labels"]))
            count = Counter(votes)
            winner, n = count.most_common(1)[0]
            judge_dimension_winners[dimension] = winner if n >= 2 else "uncertain"
        owner_defects = sorted({item for row in candidate_rows for item in row["critical_failures"]})
        judge_defects = sorted({item for receipt in assessment["judge_receipts"]
                                for item in receipt.get("critical_defects", [])})
        evidence = {
            "pair_id": scenario_id, "owner_rated": True,
            "applicable_critical_dimensions": [dim for dim in applicable if dim in {
                "persona_consistency", "emotional_appropriateness",
                "direct_request_fulfillment_responsiveness"}],
            "owner_dimension_winners": owner_dimension_winners,
            "judge_dimension_winners": judge_dimension_winners,
            "owner_critical_defect_codes": owner_defects,
            "judge_detected_critical_defect_codes": judge_defects,
        }
        critical_evidence.append(evidence)
        comparisons.append({**evidence, "owner_overall_winner": owner_winner,
                            "judge_ensemble_overall_winner": judge_winner})
    agreement = sum(row["owner_overall_winner"] == row["judge_ensemble_overall_winner"]
                    for row in comparisons) / len(comparisons)
    return {
        "owner_rated_pairs": comparisons,
        "overall_agreement": round(agreement, 6),
        "agreement_percent": round(agreement * 100, 2),
        "systematic_critical_dimension_failure": systematic_critical_dimension_failure(comparisons),
        "critical_dimension_evidence": critical_evidence,
        "ratings_lock_sha256": lock["lock_sha256"],
        "packet_sha256": _sha(packet),
        "private_mapping_accessed": False,
        "unblinded": False,
    }
