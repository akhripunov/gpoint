"""Offline AGATA Naturalness experiment manifests and A/B preparation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import copy
from pathlib import Path
from typing import Optional


PHASE3_BASE_SHA = "d228b862d5d345eb1d55a6fb1164f7d5b4d77902"
DEVELOPMENT_SUFFIX = "-02"
BLIND_SEED = 41004
EXPECTED_CATEGORIES = (
    "ru_naturalness", "daily", "humor", "support", "romance", "persona",
    "initiative", "topic_change", "return_after_break", "long_memory",
    "fact_update", "false_memory", "disagreement", "no_excess_advice",
    "long_dialogue", "boundaries",
)


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module(lab: Path, name: str):
    folder = str(lab / "evaluators")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location(f"agata_naturalness_{name}", lab / "evaluators" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Conversation Lab evaluator: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(lab: Path):
    lab = Path(lab)
    benchmark = _module(lab, "benchmark")
    pilot = _module(lab, "pilot")
    data = lab / "data/golden/synthetic"
    scenarios = benchmark.load_scenarios(data / "agata-benchmark-v1.jsonl")
    manifest = json.loads((data / "manifest-v1.json").read_text(encoding="utf-8"))
    benchmark_config = json.loads((lab / "configs/benchmark-v1.json").read_text(encoding="utf-8"))
    benchmark.validate_manifest(scenarios, manifest, benchmark_config)
    frozen_pilot = pilot.load_frozen_pilot(lab)
    return benchmark, pilot, scenarios, manifest, benchmark_config, frozen_pilot


def create_naturalness_config(lab: Path) -> dict:
    """Create a deterministic source manifest; scenario text is never copied."""
    lab = Path(lab)
    benchmark, pilot, scenarios, manifest, benchmark_config, frozen_pilot = _inputs(lab)
    categories = tuple(benchmark.CATEGORIES)
    if categories != EXPECTED_CATEGORIES:
        raise ValueError("Benchmark V1 category order differs from approved Naturalness manifest")
    selected = []
    for category in categories:
        matches = [s for s in scenarios if s["category"] == category and s["scenario_id"].endswith(DEVELOPMENT_SUFFIX)]
        if len(matches) != 1:
            raise ValueError("Naturalness development set requires exactly one category -02 scenario")
        selected.append(matches[0])
    by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    holdout_ids = list(frozen_pilot["scenario_ids"])
    development_ids = [scenario["scenario_id"] for scenario in selected]
    if set(development_ids) & set(holdout_ids):
        raise ValueError("development scenarios overlap the frozen Phase 3 holdout")
    if any(scenario_id not in by_id for scenario_id in holdout_ids):
        raise ValueError("frozen Phase 3 holdout scenario is absent from Benchmark V1")

    baseline_fixture = pilot.build_invocation(lab, frozen_pilot, holdout_ids[0], "codex-cli-baseline", 0, [])
    policy_path = lab / "configs/naturalness-policy-v1.md"
    examples_path = lab / "data/golden/synthetic/naturalness-examples-v1.jsonl"
    artifact_hashes = {}
    if policy_path.exists() and examples_path.exists():
        artifact_hashes = {"policy_sha256": _file_hash(policy_path), "examples_sha256": _file_hash(examples_path)}

    return {
        "schema_version": 1,
        "experiment_id": "agata-naturalness-v1",
        "source_sha": PHASE3_BASE_SHA,
        "baseline": {
            "configuration_id": "baseline",
            "model_route": "Codex Local via existing Mac worker; offline descriptor only",
            "model_snapshot": "gpt-6.1-sol",
            "reasoning_effort_snapshot": "medium",
            "worker_version_snapshot": "v2",
            "snapshot_note": "Phase 3 audit-time configuration; not evidence of a live request or production effective values",
            "instructions_sha256": hashlib.sha256(baseline_fixture["core_request"]["instructions"].encode()).hexdigest(),
        },
        "candidate": {"configuration_id": "naturalness-v1", "policy_version": 1, "policy_variant_limit": 3},
        "source_hashes": {
            "benchmark_config_sha256": _file_hash(lab / "configs/benchmark-v1.json"),
            "benchmark_manifest_sha256": _file_hash(lab / "data/golden/synthetic/manifest-v1.json"),
            "benchmark_scenarios_sha256": _file_hash(lab / "data/golden/synthetic/agata-benchmark-v1.jsonl"),
            "pilot_config_sha256": _file_hash(lab / "configs/pilot-v1.json"),
            "pilot_metrics_sha256": _file_hash(lab / "configs/pilot-metrics-v1.json"),
            "rubric_sha256": _file_hash(lab / "configs/rubric-v1.json"),
        },
        "artifact_hashes": artifact_hashes,
        "development": {
            "scenario_ids": development_ids,
            "scenario_hashes": {scenario["scenario_id"]: manifest["scenario_hashes"][scenario["scenario_id"]] for scenario in selected},
            "scenario_rounds": {scenario["scenario_id"]: len(scenario["dialogue_turns"]) // 2 for scenario in selected},
        },
        "holdout": {
            "scenario_ids": holdout_ids,
            "scenario_hashes": dict(frozen_pilot["scenario_hashes"]),
            "pilot_id": frozen_pilot["pilot_id"],
        },
        "blind_seed": BLIND_SEED,
        "live_inference_enabled": False,
    }


def validate_naturalness_config(lab: Path, config: dict) -> dict:
    """Fail closed if any frozen source, selection, or later policy hash drifts."""
    expected = create_naturalness_config(lab)
    if config != expected:
        raise ValueError("Naturalness manifest or frozen source hashes changed; create a new experiment version")
    return expected


def load_naturalness_config(lab: Path) -> dict:
    path = Path(lab) / "configs/naturalness-v1.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    return validate_naturalness_config(lab, config)


def write_naturalness_config(lab: Path, *, replace_existing: bool = False) -> dict:
    """Write a generated manifest without silently replacing an existing one."""
    lab = Path(lab)
    path = lab / "configs/naturalness-v1.json"
    config = create_naturalness_config(lab)
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        return config
    current = json.loads(path.read_text(encoding="utf-8"))
    if current == config:
        return config
    if not replace_existing:
        raise FileExistsError("Naturalness manifest exists with different contents")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    os.replace(temporary, path)
    return config


def load_policy(lab: Path, config: Optional[dict] = None) -> str:
    """Return only the model-facing policy section after checking its hash."""
    lab = Path(lab)
    config = load_naturalness_config(lab) if config is None else validate_naturalness_config(lab, config)
    path = lab / "configs/naturalness-policy-v1.md"
    if config.get("artifact_hashes", {}).get("policy_sha256") != _file_hash(path):
        raise ValueError("Naturalness policy hash does not match the frozen manifest")
    raw = path.read_text(encoding="utf-8")
    marker = "## Candidate-visible policy\n"
    if raw.count(marker) != 1:
        raise ValueError("Naturalness policy must contain one candidate-visible section")
    policy = raw.split(marker, 1)[1].split("\n## ", 1)[0].strip()
    if not policy:
        raise ValueError("candidate-visible Naturalness policy is empty")
    return policy


def load_examples(lab: Path, config: Optional[dict] = None) -> list[dict]:
    """Load only author-created examples and keep review metadata out of turns."""
    lab = Path(lab)
    config = load_naturalness_config(lab) if config is None else validate_naturalness_config(lab, config)
    path = lab / "data/golden/synthetic/naturalness-examples-v1.jsonl"
    if config.get("artifact_hashes", {}).get("examples_sha256") != _file_hash(path):
        raise ValueError("Naturalness examples hash does not match the frozen manifest")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    benchmark = _module(lab, "benchmark")
    scenarios = benchmark.load_scenarios(lab / "data/golden/synthetic/agata-benchmark-v1.jsonl")
    benchmark_turns = {
        benchmark.normalize(turn["text"])
        for scenario in scenarios
        for turn in scenario["dialogue_turns"]
        if turn["role"] == "user"
    }
    seen_ids, seen_dialogues = set(), set()
    for row in rows:
        if not isinstance(row, dict) or row.get("schema_version") != 1 or row.get("provenance") != "synthetic_authored":
            raise ValueError("Naturalness examples must be versioned, authored synthetic fixtures")
        example_id = row.get("example_id")
        turns = row.get("candidate_turns")
        review = row.get("review_only")
        if not isinstance(example_id, str) or not example_id.strip() or example_id in seen_ids:
            raise ValueError("Naturalness example IDs must be non-empty and unique")
        if not isinstance(turns, list) or len(turns) < 2 or len(turns) % 2:
            raise ValueError("candidate-visible examples require complete user/assistant turns")
        if not isinstance(review, dict) or not all(isinstance(review.get(field), str) and review[field].strip() for field in ("anti_pattern_id", "anti_pattern_response", "rationale")):
            raise ValueError("review-only anti-pattern response and rationale are required")
        normalized_users, signature = [], []
        for index, turn in enumerate(turns):
            expected_role = "user" if index % 2 == 0 else "assistant"
            if not isinstance(turn, dict) or turn.get("role") != expected_role or not isinstance(turn.get("text"), str) or not turn["text"].strip():
                raise ValueError("Naturalness example has an empty or misordered turn")
            text = benchmark.normalize(turn["text"])
            signature.append((expected_role, text))
            if expected_role == "user":
                normalized_users.append(text)
        dialogue_signature = tuple(signature)
        if dialogue_signature in seen_dialogues:
            raise ValueError("duplicate Naturalness example dialogue")
        if set(normalized_users) & benchmark_turns:
            raise ValueError("Naturalness example user text duplicates a Benchmark V1 turn")
        seen_ids.add(example_id)
        seen_dialogues.add(dialogue_signature)
    if len(rows) != 15:
        raise ValueError("Naturalness examples V1 requires fifteen authored pattern examples")
    return rows


def candidate_visible_examples(examples: list[dict]) -> list[dict]:
    """Strip anti-pattern labels, bad responses and review rationale."""
    return [{"turns": copy.deepcopy(example["candidate_turns"])} for example in examples]
