"""Offline reviewer packets and separate private unblinding metadata."""
import hashlib
import importlib.util
import json
import random
from pathlib import Path


def _validate_sanity(scenario, arms):
    path = Path(__file__).with_name("persona_sanity.py")
    spec = importlib.util.spec_from_file_location("agata_blind_persona_sanity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Persona sanity validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_transcripts(scenario, arms)


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
    if any((scenario_id, model) not in indexed for scenario_id in ids for model in models):
        raise ValueError("inconsistent model/scenario coverage")
    rng = random.Random(seed); packets, mappings, sanity_receipts = [], [], []; incomplete = False; quality_escalation = False
    for s in scenarios:
        order = models[:]; rng.shuffle(order); candidates = []
        expected = s["dialogue_turns"]
        sanity_arms = []
        for model in order:
            result = indexed[(s["scenario_id"], model)]
            sanity_arms.append({"model_id": model, "turns": result["turns"]})
        sanity_report = _validate_sanity(s, sanity_arms)
        if sanity_report["structural_status"] != "PASS":
            raise ValueError("blind packet blocked by structural or metadata sanity failure")
        private_quality = sanity_report["private_quality_findings"]
        quality_escalation |= bool(private_quality)
        sanity_receipts.append({"scenario_id": s["scenario_id"], "private_quality_findings": private_quality})
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
            label = ("X", "Y", "Z", "W")[position] if position < 4 else "anonymous-" + str(position + 1)
            candidates.append({"label": label, "status": r["status"], "turns": safe_turns})
            mappings.append({"scenario_id": s["scenario_id"], "label": label, "model_id": model,
                             "scenario_hash": fingerprint(s), "result_hash": fingerprint(r),
                             "usage": r.get("usage"), "latency": r.get("latency"), "cost": r.get("cost")})
            incomplete |= r["status"] != "success"
        packets.append({"scenario_id": s["scenario_id"], "category": s.get("category", "unknown"), "context": s.get("context", {}),
                        "character_context": s.get("character_context", {}),
                        "memory_fixture": s.get("memory_fixture", []), "candidates": candidates})
    private = {"schema_version": 1, "seed": seed, "mappings": mappings, "sanity_findings": sanity_receipts, "private": True}
    private["mapping_sha256"] = fingerprint(mappings)
    return ({"schema_version": 1, "completion_status": "INCOMPLETE" if incomplete else "COMPLETE_IMPORTED_RESULTS",
             "scenarios": packets, "quality_status": "HUMAN_ESCALATION" if quality_escalation else "OWNER_REVIEW_PENDING",
             "blinding_limit": "Metadata and exact model IDs removed; self-identification in prose needs manual review before OWNER packet release."},
            private)


def make_v2_blind_packets(scenarios, results, *, batch_id):
    """Use reproducible X/Y assignment derived from immutable V2 batch identity."""
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("immutable V2 batch identity is required")
    import importlib.util
    provenance_path = Path(__file__).with_name("v2_provenance.py")
    provenance_spec = importlib.util.spec_from_file_location("agata_v2_blind_provenance", provenance_path)
    if provenance_spec is None or provenance_spec.loader is None:
        raise PermissionError("V2 provenance validator is unavailable")
    provenance = importlib.util.module_from_spec(provenance_spec)
    provenance_spec.loader.exec_module(provenance)
    source_provenance = provenance.validate_source_scenarios(scenarios)
    if not isinstance(results, list) or {row.get("model_id") for row in results if isinstance(row, dict)} != {"baseline", "naturalness-v1"}:
        raise ValueError("V2 blindness requires exactly the Baseline and Naturalness V1 arms")
    source = {"batch_id": batch_id, "scenarios": scenarios, "result_hashes": [fingerprint(row) for row in results]}
    digest = hashlib.sha256(("agata-lab-v2-blind\0" + fingerprint(source)).encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    packet, private = make_blind_packets(scenarios, results, seed)
    private["batch_id"] = batch_id
    private["batch_sha256"] = fingerprint(source)
    private.update(source_provenance)
    private["blind_packet_sha256"] = fingerprint(packet)
    for mapping in private["mappings"]:
        candidate = next(
            item for scenario in packet["scenarios"] if scenario["scenario_id"] == mapping["scenario_id"]
            for item in scenario["candidates"] if item["label"] == mapping["label"]
        )
        mapping["blind_candidate_sha256"] = fingerprint(candidate)
    private["mapping_sha256"] = fingerprint(private["mappings"])
    return packet, private
