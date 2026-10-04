"""Locked human ratings and hash-checked unblinding for blind packets."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path


DIMENSIONS = ("naturalness", "emotion", "persona", "memory", "initiative", "diversity", "disagreement", "consent")
SUPPLEMENTAL = ("humor", "context_continuity", "dialogue_engagement")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _expected(packet):
    expected = set()
    for scenario in packet["scenarios"]:
        for candidate in scenario["candidates"]:
            expected.add((scenario["scenario_id"], candidate["label"]))
    return expected


def _validate_mapping(packet, private_mapping):
    if not isinstance(private_mapping, dict) or private_mapping.get("private") is not True or not isinstance(private_mapping.get("mappings"), list):
        raise ValueError("private mapping missing")
    mappings = private_mapping["mappings"]
    if private_mapping.get("mapping_sha256") != _hash(mappings):
        raise ValueError("private mapping integrity check failed")
    public_pairs = {(s["scenario_id"], c["label"]) for s in packet["scenarios"] for c in s["candidates"]}
    if len(mappings) != len(public_pairs) or {(m.get("scenario_id"), m.get("label")) for m in mappings} != public_pairs:
        raise ValueError("private mapping does not match locked packet")
    for mapping in mappings:
        if not mapping.get("model_id") or not mapping.get("scenario_hash") or not mapping.get("result_hash"):
            raise ValueError("private mapping provenance is incomplete")
    if len({m["scenario_hash"] for m in mappings}) != len(packet["scenarios"]):
        raise ValueError("private mapping scenario provenance is inconsistent")
    return mappings


def empty_ratings(packet, lab=None):
    return {"schema_version": 1, "packet_sha256": _hash(packet), "ratings": [
        {"scenario_id": sid, "label": label, "scores": {d: None for d in DIMENSIONS},
         "supplemental": {d: None for d in SUPPLEMENTAL}, "not_applicable": {}, "critical_failures": [], "notes": ""}
        for sid, label in sorted(_expected(packet))],
        "pairwise_preferences": [{"scenario_id": scenario["scenario_id"], "left_label": left, "right_label": right,
                                   "preferred_label": None, "notes": ""}
            for scenario in packet["scenarios"] for left, right in itertools.combinations(
                [candidate["label"] for candidate in scenario["candidates"]], 2)]}


def _validate(packet, ratings):
    rows = ratings.get("ratings")
    if not isinstance(rows, list) or len(rows) != len(_expected(packet)):
        raise ValueError("all blind candidates require one rating row")
    seen = set()
    for row in rows:
        pair = (row.get("scenario_id"), row.get("label"))
        if pair in seen or pair not in _expected(packet):
            raise ValueError("duplicate or unknown rating row")
        seen.add(pair)
        scores = row.get("scores", {})
        supplemental = row.get("supplemental", {})
        not_applicable = row.get("not_applicable", {})
        all_dimensions = set(DIMENSIONS) | set(SUPPLEMENTAL)
        if set(scores) != set(DIMENSIONS) or set(supplemental) != set(SUPPLEMENTAL) or set(not_applicable) - all_dimensions:
            raise ValueError("rating dimensions do not match the versioned rubric and metric set")
        combined = {**scores, **supplemental}
        for dimension, value in combined.items():
            if value is None:
                if not isinstance(not_applicable.get(dimension), str) or not not_applicable[dimension].strip():
                    raise ValueError("N/A rating requires an OWNER explanation")
            elif type(value) is not int or not 1 <= value <= 5:
                raise ValueError("scores must be whole-conversation OWNER ratings from 1 to 5")
        if not isinstance(row.get("critical_failures"), list) or not isinstance(row.get("notes"), str):
            raise ValueError("critical failures and notes are required")
    if seen != _expected(packet) or ratings.get("packet_sha256") != _hash(packet):
        raise ValueError("ratings do not cover the exact blind packet")
    preferences = ratings.get("pairwise_preferences")
    expected_pairs = set()
    for scenario in packet["scenarios"]:
        for left, right in itertools.combinations(sorted(c["label"] for c in scenario["candidates"]), 2):
            expected_pairs.add((scenario["scenario_id"], left, right))
    if not isinstance(preferences, list) or {(p.get("scenario_id"), p.get("left_label"), p.get("right_label")) for p in preferences} != expected_pairs or len(preferences) != len(expected_pairs):
        raise ValueError("one OWNER comparison per candidate pair and scenario is required")
    for preference in preferences:
        if preference.get("preferred_label") not in {preference["left_label"], preference["right_label"], "tie"}:
            raise ValueError("pairwise preference must select one presented candidate or tie")


def lock_ratings(packet, ratings, *, cycle_id):
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise ValueError("review cycle ID required")
    _validate(packet, ratings)
    body = {"schema_version": 1, "cycle_id": cycle_id, "packet_sha256": _hash(packet), "ratings_sha256": _hash(ratings)}
    body["lock_sha256"] = _hash(body)
    return body


def verify_lock(lock, packet, ratings):
    try:
        _validate(packet, ratings)
        body = {k: lock[k] for k in ("schema_version", "cycle_id", "packet_sha256", "ratings_sha256")}
        return lock["lock_sha256"] == _hash(body) and body["packet_sha256"] == _hash(packet) and body["ratings_sha256"] == _hash(ratings)
    except (ValueError, KeyError, TypeError):
        return False


def new_review_cycle(ratings, prior_lock, *, cycle_id, owner_reason):
    if not isinstance(owner_reason, str) or not owner_reason.strip():
        raise ValueError("OWNER reason required for a correction cycle")
    try:
        prior_body = {k: prior_lock[k] for k in ("schema_version", "cycle_id", "packet_sha256", "ratings_sha256")}
        prior_lock_valid = prior_lock["lock_sha256"] == _hash(prior_body)
    except (KeyError, TypeError):
        prior_lock_valid = False
    if not prior_lock_valid or prior_lock.get("ratings_sha256") != _hash(ratings) or prior_lock.get("packet_sha256") != ratings.get("packet_sha256") or prior_lock.get("cycle_id") == cycle_id:
        raise ValueError("new review cycle must preserve a verified prior lock under a new ID")
    body = copy.deepcopy(ratings)
    for row in body["ratings"]:
        row["scores"] = {d: None for d in DIMENSIONS}
        row["supplemental"] = {d: None for d in SUPPLEMENTAL}
        row["not_applicable"] = {}
        row["critical_failures"] = []
        row["notes"] = ""
    cycle = {"schema_version": 1, "cycle_id": cycle_id, "prior_lock_sha256": prior_lock.get("lock_sha256"),
             "owner_reason": owner_reason.strip(), "ratings": body}
    for preference in body.get("pairwise_preferences", []):
        preference["preferred_label"] = None
        preference["notes"] = ""
    if not cycle_id or not cycle["prior_lock_sha256"]:
        raise ValueError("new cycle must identify prior lock and new cycle ID")
    return cycle


def persist_locked_ratings(path, packet, ratings, *, cycle_id):
    """Create a read-only, write-once ratings artifact; corrections require another path/cycle."""
    lock = lock_ratings(packet, ratings, cycle_id=cycle_id)
    destination = Path(path)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump({"ratings": ratings, "lock": lock}, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    os.chmod(destination, 0o444)
    return lock


def unblind(packet, lock, ratings, private_mapping):
    if not verify_lock(lock, packet, ratings):
        raise ValueError("ratings must be complete and locked before unblinding")
    mappings = _validate_mapping(packet, private_mapping)
    return {"cycle_id": lock["cycle_id"], "revealed": [
        {"scenario_id": row["scenario_id"], "label": row["label"], "model_id": next(
            m["model_id"] for m in mappings if (m["scenario_id"], m["label"]) == (row["scenario_id"], row["label"])),
         "scores": copy.deepcopy(row["scores"]), "supplemental": copy.deepcopy(row["supplemental"]),
         "critical_failures": list(row["critical_failures"])} for row in ratings["ratings"]],
        "pairwise_preferences": copy.deepcopy(ratings["pairwise_preferences"])}


def aggregate_ratings(packet, lock, ratings, private_mapping):
    """Descriptive post-lock summary; does not rank a winner or add inference."""
    if not verify_lock(lock, packet, ratings):
        raise ValueError("aggregate requires an exact locked OWNER rating set")
    mappings = _validate_mapping(packet, private_mapping)
    mapping = {(m["scenario_id"], m["label"]): m["model_id"] for m in mappings}
    grouped = {}
    statuses = {(s["scenario_id"], c["label"]): c["status"] for s in packet["scenarios"] for c in s["candidates"]}
    category_by_scenario = {s["scenario_id"]: s.get("category", "unknown") for s in packet["scenarios"]}
    for row in ratings["ratings"]:
        key = (mapping[(row["scenario_id"], row["label"])], category_by_scenario[row["scenario_id"]])
        group = grouped.setdefault(key, {"scenarios": 0, "status_counts": {}, "scores": {}, "supplemental": {}, "critical_failures": 0})
        status = statuses[(row["scenario_id"], row["label"])]
        group["status_counts"][status] = group["status_counts"].get(status, 0) + 1
        group["scenarios"] += 1
        for family in ("scores", "supplemental"):
            for name, value in row[family].items():
                cell = group[family].setdefault(name, {"sum": 0, "n": 0, "na": 0})
                if value is None:
                    cell["na"] += 1
                else:
                    cell["sum"] += value
                    cell["n"] += 1
        group["critical_failures"] += len(row["critical_failures"])
    for group in grouped.values():
        for family in ("scores", "supplemental"):
            for cell in group[family].values():
                cell["mean"] = round(cell["sum"] / cell["n"], 3) if cell["n"] else None
    preference_counts = {}
    scenario_to_models = {}
    for (scenario_id, label), model_id in mapping.items():
        scenario_to_models.setdefault(scenario_id, {})[label] = model_id
    for preference in ratings["pairwise_preferences"]:
        left = scenario_to_models[preference["scenario_id"]][preference["left_label"]]
        right = scenario_to_models[preference["scenario_id"]][preference["right_label"]]
        chosen = preference["preferred_label"]
        if chosen == "tie":
            preference_counts.setdefault(left, {"wins": 0, "ties": 0, "losses": 0})["ties"] += 1
            preference_counts.setdefault(right, {"wins": 0, "ties": 0, "losses": 0})["ties"] += 1
        else:
            winner = left if chosen == preference["left_label"] else right
            loser = right if winner == left else left
            preference_counts.setdefault(winner, {"wins": 0, "ties": 0, "losses": 0})["wins"] += 1
            preference_counts.setdefault(loser, {"wins": 0, "ties": 0, "losses": 0})["losses"] += 1
    return {"grouped_by_model_and_category": {f"{model}|{category}": value for (model, category), value in sorted(grouped.items())},
            "overall_human_preference_counts": preference_counts, "winner": None,
            "interpretation": "descriptive pilot only; one selected scenario per category is not a population estimate"}
