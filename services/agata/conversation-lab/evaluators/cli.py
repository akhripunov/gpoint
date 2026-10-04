#!/usr/bin/env python3
"""Local-only lab commands: no inference, downloader or production access."""
import argparse
import json
from pathlib import Path
import sys
import secrets
from corpus import CANONICAL_DATA, SOURCE_IDS, read_pointer_dialogues, redact, verify_sources, write_report
from benchmark import load_scenarios, validate_scenarios, validate_manifest, scenario_similarity_alerts
from leakage import check_leakage
from blind import make_blind_packets

DEFAULT_SCENARIOS = Path(__file__).resolve().parents[1] / "data/golden/synthetic/agata-benchmark-v1.jsonl"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "review", "benchmark", "leakage", "blind"))
    parser.add_argument("--data-root", type=Path, default=CANONICAL_DATA)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_SCENARIOS.parent / "manifest-v1.json")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "configs/benchmark-v1.json")
    parser.add_argument("--source", choices=sorted(SOURCE_IDS))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--seed", type=int, help="private reproduction seed; default cryptographically random")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = {"status": "PASS", "sources": verify_sources(args.data_root)}
            if set(result["sources"]) != SOURCE_IDS:
                raise ValueError("canonical starter inventory incomplete")
        elif args.command == "review":
            if not args.source or not 0 <= args.start < 30 or not 1 <= args.count <= 5:
                raise ValueError("source/start/count required; review batches limited to five full dialogues")
            rows = read_pointer_dialogues(args.data_root, args.source)
            result = {"status": "READ_ONLY_SELECTION", "source": args.source, "selection_total": len(rows),
                      "rows": [{**r, "turns": [{"speaker": t["speaker"], "text": redact(t["text"])} for t in r["turns"]]} for r in rows[args.start:args.start+args.count]]}
        else:
            scenarios = load_scenarios(args.scenarios); structure = validate_scenarios(scenarios)
            structure = validate_manifest(scenarios, json.loads(args.manifest.read_text(encoding="utf-8")), json.loads(args.config.read_text(encoding="utf-8")))
            if args.command == "benchmark":
                result = structure
                result["near_duplicate_alerts"] = scenario_similarity_alerts(scenarios)
                if result["near_duplicate_alerts"]:
                    result["status"] = "REVIEW_REQUIRED"
            elif args.command == "leakage":
                sources = verify_sources(args.data_root)
                if set(sources) != SOURCE_IDS:
                    raise ValueError("canonical starter inventory incomplete")
                result = check_leakage(scenarios, [Path(x["processed_path"]) for x in sources.values()])
                result["input_checksums"] = {sid: x["processed_sha256"] for sid, x in sources.items()}
            else:
                if not args.results or not args.run_id:
                    raise ValueError("blind requires local imported results and an exclusive run-id")
                results = json.loads(args.results.read_text(encoding="utf-8"))
                packet, private = make_blind_packets(scenarios, results, args.seed if args.seed is not None else secrets.randbits(128))
                key_path = write_report(args.data_root, args.run_id, "unblinding-private.json", private)
                public_path = write_report(args.data_root, args.run_id, "reviewer-packet.json", packet)
                result = {"status": "OFFLINE_PACKETS_CREATED", "reviewer_packet": str(public_path),
                          "private_key": str(key_path), "completion_status": packet["completion_status"], "owner_review": "PENDING"}
        if args.run_id and args.command != "blind":
            write_report(args.data_root, args.run_id, args.command + "-report.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in {"FAIL", "REVIEW_REQUIRED"} else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
