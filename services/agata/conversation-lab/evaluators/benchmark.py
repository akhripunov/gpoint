"""Structural contracts, not conversational-quality scores."""
import collections
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata

CATEGORIES = ("ru_naturalness", "daily", "humor", "support", "romance", "persona", "initiative", "topic_change", "return_after_break", "long_memory", "fact_update", "false_memory", "disagreement", "no_excess_advice", "long_dialogue", "boundaries")
REQUIRED = {"scenario_id", "category", "language", "context", "character_context", "memory_fixture", "dialogue_turns", "expected_behavior", "failure_patterns", "evaluation_notes"}


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_scenarios(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("temporal metadata must be an ISO date/time string")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp requires explicit timezone")
        return parsed
    except ValueError as exc:
        raise ValueError("invalid ISO date/time") from exc


def later(left, right):
    if type(left) is date or type(right) is date:
        return (left.date() if isinstance(left, datetime) else left) > (right.date() if isinstance(right, datetime) else right)
    return left > right


def validate_temporal(scenario):
    context = scenario["context"]
    parsed_context = {k: parse_time(v) for k, v in context.items() if k in {"as_of", "current_session_at", "prior_session_at"}}
    turns = [parse_time(t["at"]) for t in scenario["dialogue_turns"] if "at" in t]
    if any(later(a, b) for a, b in zip(turns, turns[1:])):
        raise ValueError("reversed turn chronology")
    start = parsed_context.get("current_session_at", turns[0] if turns else parsed_context.get("as_of"))
    if "prior_session_at" in parsed_context and start is not None and later(parsed_context["prior_session_at"], start):
        raise ValueError("prior session after current session")
    facts = {f["fact_id"]: f for f in scenario["memory_fixture"]}
    dates = {fid: parse_time(f["valid_from"]) for fid, f in facts.items() if "valid_from" in f}
    for fid, fact in facts.items():
        # V1 fixtures contain facts already known at session start. A known
        # future plan is described in text, with its prior statement date here.
        if fid in dates and start is not None and later(dates[fid], start):
            raise ValueError("initial memory is not yet available at session start")
        visited = set(); current = fid
        while current is not None:
            if current in visited:
                raise ValueError("cyclic memory supersession")
            visited.add(current)
            previous = facts[current].get("supersedes")
            if previous in dates and current in dates and later(dates[previous], dates[current]):
                raise ValueError("memory update predates superseded fact")
            current = previous


def validate_scenarios(scenarios):
    if not isinstance(scenarios, list) or len(scenarios) != 128:
        raise ValueError("benchmark requires exactly 128 scenarios")
    ids, signatures, counts = set(), set(), collections.Counter()
    personas = set()
    for s in scenarios:
        if not isinstance(s, dict) or not REQUIRED.issubset(s):
            raise ValueError("missing required scenario fields")
        sid, category = s["scenario_id"], s["category"]
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9_-]+", sid) or sid in ids:
            raise ValueError("invalid/duplicate scenario id")
        if category not in CATEGORIES or s["language"] != "ru":
            raise ValueError("unknown category/language")
        if not isinstance(s["context"], dict) or not isinstance(s["context"].get("setup"), str) or not s["context"]["setup"].strip():
            raise ValueError("scenario context must contain setup")
        character = s["character_context"]
        if not isinstance(character, dict) or not isinstance(character.get("persona_ref"), str) or not character["persona_ref"]:
            raise ValueError("missing shared evaluation Persona")
        personas.add(character["persona_ref"])
        if not isinstance(s["memory_fixture"], list):
            raise ValueError("memory fixture must be a list")
        memory = s["memory_fixture"]; fact_ids = [x.get("fact_id") for x in memory if isinstance(x, dict)]
        if len(fact_ids) != len(memory) or any(not isinstance(x, str) or not x for x in fact_ids) or len(set(fact_ids)) != len(fact_ids):
            raise ValueError("invalid/duplicate memory fact")
        for fact in memory:
            if not isinstance(fact.get("text"), str) or not fact["text"].strip() or (fact.get("supersedes") is not None and (fact["supersedes"] not in fact_ids or fact["supersedes"] == fact["fact_id"])):
                raise ValueError("invalid memory/supersession reference")
        turns = s["dialogue_turns"]
        minimum = 24 if category == "long_dialogue" else 8
        if not isinstance(turns, list) or len(turns) < minimum or len(turns) % 2:
            raise ValueError("insufficient/misaligned multi-turn rounds")
        utterances = []
        for i, t in enumerate(turns):
            role = "user" if i % 2 == 0 else "assistant"
            if not isinstance(t, dict) or t.get("role") != role:
                raise ValueError("invalid turn role/order")
            if role == "user":
                if not isinstance(t.get("text"), str) or not t["text"].strip():
                    raise ValueError("empty user prompt")
                utterances.append(normalize(t["text"]))
            elif t.get("response_slot") is not True or "text" in t:
                raise ValueError("assistant must be an unfilled response slot")
        validate_temporal(s)
        sig = stable_hash(utterances)
        if sig in signatures:
            raise ValueError("duplicate normalized dialogue")
        for field in ("expected_behavior", "failure_patterns"):
            if not isinstance(s[field], list) or not s[field] or any(not isinstance(x, str) or not x.strip() for x in s[field]):
                raise ValueError("empty/invalid evaluation criterion")
        if not isinstance(s["evaluation_notes"], str) or not s["evaluation_notes"].strip():
            raise ValueError("missing evaluation notes")
        ids.add(sid); signatures.add(sig); counts[category] += 1
    if set(counts) != set(CATEGORIES) or any(counts[c] != 8 for c in CATEGORIES) or len(personas) != 1:
        raise ValueError("category coverage/Persona inconsistency")
    return {"status": "STRUCTURE_PASS", "count": len(scenarios), "category_counts": dict(counts), "scenario_hashes": {s["scenario_id"]: stable_hash(s) for s in scenarios}, "persona_ref": next(iter(personas)), "quality_status": "REQUIRES_MANUAL_REVIEW"}


def prompt_fixture(scenario, round_index=0):
    # This is an offline round fixture, not a provider request. Previous real
    # replies must be supplied by the future authorized Core adapter.
    turns = scenario["dialogue_turns"]
    if type(round_index) is not int or not 0 <= round_index < len(turns)//2:
        raise ValueError("invalid round index")
    context = {k: v for k, v in scenario["context"].items() if k in {"model_context", "as_of", "current_session", "current_session_at"}}
    return {"scenario_id": scenario["scenario_id"], "language": scenario["language"],
            "context": context, "character_context": scenario["character_context"],
            "memory_fixture": scenario["memory_fixture"],
            "user_turn": turns[round_index*2],
            "prior_user_turns": [t for t in turns[:round_index*2] if t["role"] == "user"]}


def validate_manifest(scenarios, manifest, config):
    report = validate_scenarios(scenarios)
    if manifest.get("schema_version") != 1 or config.get("schema_version") != 1:
        raise ValueError("unsupported benchmark schema version")
    if manifest.get("scenario_hashes") != report["scenario_hashes"] or manifest.get("category_counts") != report["category_counts"]:
        raise ValueError("benchmark manifest hash/coverage mismatch")
    if config.get("scenario_count") != 128 or config.get("per_category") != 8 or config.get("categories") != list(CATEGORIES):
        raise ValueError("benchmark config coverage mismatch")
    if config.get("model_calls_enabled") is not False or config.get("persona", {}).get("id") != report["persona_ref"]:
        raise ValueError("offline mode/Persona mismatch")
    persona_hash = stable_hash(config["persona"])
    if manifest.get("persona_hash") != persona_hash:
        raise ValueError("Persona manifest hash mismatch")
    return {**report, "manifest_status": "PASS", "persona_hash": persona_hash, "benchmark_hash": stable_hash(scenarios)}


def scenario_similarity_alerts(scenarios):
    """Lexical near-copy alerts; semantic independence still requires review."""
    from difflib import SequenceMatcher
    texts = [(s["scenario_id"], " ".join(re.findall(r"\w+", normalize(" ".join(t["text"] for t in s["dialogue_turns"] if t["role"] == "user"))))) for s in scenarios]
    alerts = []
    for i, (left_id, left) in enumerate(texts):
        for right_id, right in texts[i+1:]:
            if min(len(left.split()), len(right.split())) < 12:
                continue
            score = SequenceMatcher(None, left, right, autojunk=False).ratio()
            if score >= 0.9:
                alerts.append({"left": left_id, "right": right_id, "similarity": round(score, 4), "status": "REVIEW_REQUIRED"})
    return alerts
