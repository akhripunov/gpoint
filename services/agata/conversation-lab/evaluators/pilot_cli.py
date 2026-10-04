"""Reproducible local dry-run for the frozen AGATA pilot. No inference transport."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pilot


LAB = Path(__file__).resolve().parents[1]


def _write_new(path, value, *, private=False):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    if private:
        os.chmod(path, 0o600)


def dry_run(run_id):
    if not re.fullmatch(r"phase3-[a-z0-9][a-z0-9-]{2,48}", run_id):
        raise ValueError("run ID must start phase3- and contain only lowercase letters, digits and hyphens")
    frozen = pilot.load_frozen_pilot(LAB)
    report = pilot.run_fixture_dry_run(LAB, frozen)
    packet, mapping = pilot.fixture_blind_packet(LAB, frozen)
    output = LAB / "data/runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    _write_new(output / "dry-run-report.json", {**report, "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "pilot_id": frozen["pilot_id"],
        "frozen_scenario_ids": frozen["scenario_ids"], "input_hashes": frozen["input_hashes"]})
    _write_new(output / "blind-fixture-packet.json", packet)
    _write_new(output / "private-fixture-mapping.json", mapping, private=True)
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dry = commands.add_parser("dry-run", help="build fixture-only payloads and blind packets, no model calls")
    dry.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "dry-run":
        path, report = dry_run(args.run_id)
        print(json.dumps({**report, "output_directory": str(path)}, ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
