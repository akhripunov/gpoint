#!/usr/bin/env python3
"""Offline V2 Conversation Lab preflight; this command never loads examples or calls a provider."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _validate_manifest(manifest: dict) -> tuple[int, int]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise ValueError("Conversation Lab split manifest V2 is required")
    if manifest.get("dataset_id") != "agata-conversation-lab-v2":
        raise ValueError("unexpected Conversation Lab V2 dataset identity")
    legacy = manifest.get("legacy_holdout_v1")
    read_incident = isinstance(legacy, dict) and legacy.get("scenario_content_read") is True
    incident_recorded = (isinstance(legacy, dict) and isinstance(legacy.get("access_incident"), str)
                         and bool(legacy["access_incident"].strip())
                         and isinstance(legacy.get("v2_fixture_provenance"), str)
                         and bool(legacy["v2_fixture_provenance"].strip()))
    if (not isinstance(legacy, dict) or legacy.get("status") != "COMPROMISED_EXCLUDED"
            or legacy.get("optimizer_access") is not False
            or legacy.get("scenario_content_used_for_v2") is not False
            or legacy.get("scenario_content_read") not in (False, True)
            or (read_incident and not incident_recorded)):
        raise PermissionError("compromised V1 holdout must remain excluded")
    splits = manifest.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"DEV", "VALIDATION", "SEALED_HOLDOUT"}:
        raise ValueError("V2 manifest must define DEV, VALIDATION, and SEALED_HOLDOUT")
    counts = {}
    all_ids = set()
    for name in ("DEV", "VALIDATION"):
        value = splits[name]
        ids = value.get("scenario_ids") if isinstance(value, dict) else None
        hashes = value.get("scenario_hashes") if isinstance(value, dict) else None
        if (not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() or item.endswith("-01") for item in ids)
                or len(ids) != len(set(ids)) or not isinstance(hashes, dict) or set(ids) != set(hashes)):
            raise ValueError(f"{name} scenario ID/hash allowlist is malformed")
        if all_ids.intersection(ids):
            raise ValueError("V2 splits must be disjoint")
        all_ids.update(ids)
    sealed = splits["SEALED_HOLDOUT"]
    if not isinstance(sealed, dict):
        raise ValueError("sealed split must remain opaque")
    if sealed.get("scenario_ids") != [] or sealed.get("scenario_hashes") != {}:
        raise PermissionError("sealed scenario identifiers and commitments are not available to this codepath")
    if sealed.get("plaintext_committed") is not False or sealed.get("path") or sealed.get("plaintext_path"):
        raise PermissionError("sealed holdout content or path cannot enter this checkout")
    counts = {"DEV": len(splits["DEV"]["scenario_ids"]), "VALIDATION": len(splits["VALIDATION"]["scenario_ids"])}
    return counts["DEV"], counts["VALIDATION"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline-only AGATA Conversation Lab V2 validation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("dry-run", help="validate V2 split commitments without opening scenario data")
    args = parser.parse_args(argv)

    lab = Path(__file__).resolve().parents[1]
    manifest = json.loads((lab / "configs/splits-v2.json").read_text(encoding="utf-8"))
    dev_count, validation_count = _validate_manifest(manifest)
    output = {
        "dataset_id": manifest["dataset_id"],
        "status": "READY_FOR_DEV_FIXTURES" if dev_count else "SPLITS_NOT_POPULATED",
        "dev_scenario_commitments": dev_count,
        "validation_scenario_commitments": validation_count,
        "scenario_payloads_loaded": 0,
        "live_invocations": 0,
        "conversation_arms": 0,
        "generator_provider_invocations": 0,
        "judge_provider_invocations": 0,
        "retries": 0,
        "sealed_holdout_opened": False,
        "optimizer_execution_enabled": False,
        "writes_files": False,
        "production_access": False,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
