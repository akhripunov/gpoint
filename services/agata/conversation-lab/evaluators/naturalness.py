"""Offline AGATA Naturalness experiment manifests and A/B preparation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


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
