"""Offline reviewer packets and separate private unblinding metadata."""
import hashlib
import json
import random


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_blind_packets(scenarios, results, seed):
    if type(seed) is not int or not scenarios or not results:
        raise ValueError("seed, scenarios and results required")
    ids = [s["scenario_id"] for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario")
    models = sorted({r["model_id"] for r in results})
    if not models or any(not isinstance(m, str) or not m for m in models):
        raise ValueError("invalid model identifier")
    indexed = {}
    for r in results:
        pair = (r["scenario_id"], r["model_id"])
        if pair in indexed or r["scenario_id"] not in ids:
            raise ValueError("duplicate/unknown scenario result")
        if r.get("status") not in {"success", "partial", "error", "refusal"} or not isinstance(r.get("turns"), list):
            raise ValueError("invalid imported result status/turns")
        indexed[pair] = r
    rng = random.Random(seed); packets, mappings = [], []; incomplete = False
    for s in scenarios:
        order = models[:]; rng.shuffle(order); candidates = []
        expected = s["dialogue_turns"]
        for position, model in enumerate(order):
            r = indexed.get((s["scenario_id"], model))
            if r is None:
                raise ValueError("inconsistent model/scenario coverage")
            turns = r["turns"]
            if len(turns) > len(expected) or (r["status"] == "success" and len(turns) != len(expected)):
                raise ValueError("incomplete/extra successful conversation")
            safe_turns = []
            for i, t in enumerate(turns):
                if not isinstance(t, dict) or t.get("role") != expected[i]["role"] or not isinstance(t.get("text"), str) or not t["text"].strip():
                    raise ValueError("invalid imported conversation")
                if t["role"] == "user" and t["text"] != expected[i]["text"]:
                    raise ValueError("user prompt mismatch")
                text = t["text"]
                for identifier in models:
                    text = text.replace(identifier, "[MODEL REDACTED]")
                safe_turns.append({"role": t["role"], "text": text})
            label = "candidate-" + str(position+1)
            candidates.append({"label": label, "status": r["status"], "turns": safe_turns})
            mappings.append({"scenario_id": s["scenario_id"], "label": label, "model_id": model,
                             "scenario_hash": fingerprint(s), "result_hash": fingerprint(r),
                             "usage": r.get("usage"), "latency": r.get("latency"), "cost": r.get("cost")})
            incomplete |= r["status"] != "success"
        packets.append({"scenario_id": s["scenario_id"], "context": s.get("context", {}),
                        "evaluation_notes": s.get("evaluation_notes", ""), "character_context": s["character_context"],
                        "memory_fixture": s["memory_fixture"], "expected_behavior": s["expected_behavior"],
                        "failure_patterns": s["failure_patterns"], "candidates": candidates})
    return ({"schema_version": 1, "completion_status": "INCOMPLETE" if incomplete else "COMPLETE_IMPORTED_RESULTS",
             "scenarios": packets, "quality_status": "OWNER_REVIEW_PENDING",
             "blinding_limit": "Metadata and exact model IDs removed; self-identification in prose needs manual review before OWNER packet release."},
            {"schema_version": 1, "seed": seed, "mappings": mappings, "private": True})
