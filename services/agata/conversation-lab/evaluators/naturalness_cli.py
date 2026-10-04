#!/usr/bin/env python3
"""Offline-only CLI for preparing the AGATA Naturalness V1 fixture packet."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare offline AGATA Naturalness V1 fixtures; never runs a model.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("dry-run", help="validate, build paired fixture requests and summarize an anonymous fixture packet")
    args = parser.parse_args(argv)

    lab = Path(__file__).resolve().parents[1]
    folder = str(lab / "evaluators")
    import sys
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import naturalness

    config = naturalness.load_naturalness_config(lab)
    dry_run = naturalness.run_fixture_dry_run(lab, config)
    packet, private = naturalness.package_blind_fixture(
        lab, config, naturalness.fixture_ab_result_rows(lab, config), config["blind_seed"],
    )
    output = {
        "experiment_id": config["experiment_id"],
        "status": "FIXTURE_ONLY",
        "development_scenarios": dry_run["development_scenarios"],
        "candidate_turn_requests": dry_run["planned_candidate_turns"],
        "fixture_responses": dry_run["fixture_responses"],
        "live_inference_requests": 0,
        "holdout_scenarios_used": 0,
        "blind_packet": {
            "completion_status": packet["completion_status"],
            "candidate_slots": sum(len(scenario["candidates"]) for scenario in packet["scenarios"]),
            "sha256": naturalness.stable_hash(packet),
            "private_mapping_sha256": private["mapping_sha256"],
        },
        "quality_status": "NO_MODEL_OUTPUTS_OWNER_REVIEW_NOT_APPLICABLE",
        "writes_files": False,
        "worker_or_production_access": False,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
