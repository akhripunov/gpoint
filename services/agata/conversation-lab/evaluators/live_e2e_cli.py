#!/usr/bin/env python3
"""Operator entry point for one exact, pre-authorized V2 DEV live run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from live_e2e import (
    OfflinePreflightError,
    run_v2_live_e2e,
    validate_offline_runner_prerequisites,
)
from run_coordinator import CallLedger
from worker_client import canonical_lab_queue
import lab_queue


def _private_directory(path: Path, *, create: bool = False) -> Path:
    path = Path(path)
    if create and not path.exists():
        path.mkdir(mode=0o700)
    if (path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise PermissionError("private LIVE run directory is missing or unsafe")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--promptfoo", required=True, help="absolute path to Lab-local Promptfoo 0.123.1")
    parser.add_argument("--preflight-only", action="store_true",
                        help="verify local source/runtime/Promptfoo without reading approval or creating run state")
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--expected-lab-bundle-sha256")
    parser.add_argument("--expected-lab-approval-bundle-sha256")
    parser.add_argument("--expected-worker-install-bundle-sha256")
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        expected = (args.expected_source_sha, args.expected_lab_bundle_sha256,
                    args.expected_lab_approval_bundle_sha256, args.expected_worker_install_bundle_sha256)
        if any(not item for item in expected):
            raise OfflinePreflightError(
                "EXACT_CANDIDATE", "EXPECTED_FINGERPRINT_REQUIRED",
                ValueError("preflight requires all exact candidate fingerprints"),
            )
        preflight = validate_offline_runner_prerequisites(
            args.scenario_id, args.promptfoo, env,
            expected_source_sha=args.expected_source_sha,
            expected_lab_bundle_sha256=args.expected_lab_bundle_sha256,
            expected_lab_approval_bundle_sha256=args.expected_lab_approval_bundle_sha256,
            expected_worker_install_bundle_sha256=args.expected_worker_install_bundle_sha256,
        )
        if args.preflight_only:
            print(json.dumps(preflight, sort_keys=True))
            return 0
        if not args.run_id or not lab_queue.ID_RE.fullmatch(args.run_id):
            raise ValueError("run ID is invalid")
        queue = canonical_lab_queue(env)
        support = queue.parents[1]
        if support.is_symlink() or not support.is_dir() or support.stat().st_uid != os.getuid():
            raise PermissionError("canonical Worker support directory is unavailable")
        private_root = _private_directory(support / "private", create=True)
        lab_root = _private_directory(private_root / "conversation-lab", create=True)
        runs_root = _private_directory(lab_root / "runs", create=True)
        run_root = runs_root / args.run_id
        run_root.mkdir(mode=0o700)
        _private_directory(run_root)
        receipts = _private_directory(run_root / "receipts", create=True)
        artifacts = _private_directory(run_root / "private", create=True)
        ledger = CallLedger(journal_path=run_root / "call-ledger.jsonl")
        result = run_v2_live_e2e(
            args.scenario_id, run_id=args.run_id, ledger=ledger,
            promptfoo_executable=str(Path(args.promptfoo).resolve()),
            receipt_directory=receipts, private_directory=artifacts, env=env,
            expected_source_sha=args.expected_source_sha,
            expected_lab_bundle_sha256=args.expected_lab_bundle_sha256,
            expected_lab_approval_bundle_sha256=args.expected_lab_approval_bundle_sha256,
            expected_worker_install_bundle_sha256=args.expected_worker_install_bundle_sha256,
        )
        print(json.dumps({
            "status": result["status"], "run_id": result["run_id"],
            "scenario_id": result["scenario_id"], "decision_type": result["decision_type"],
            "generator_invocations": result["generator_invocations"],
            "judge_invocations": result["judge_invocations"], "retries": result["retries"],
            "model_identity_status": result["model_identity_status"],
            "receipt_sha256": result["receipt_sha256"],
        }, sort_keys=True))
        return 0
    except Exception as exc:
        # The exception text can include local paths, prompt content, or provider output.
        if isinstance(exc, OfflinePreflightError):
            result = {"status": "PRE_INFERENCE_BLOCKED", "stage": exc.stage,
                      "reason_code": exc.reason_code, "error_type": exc.cause_type}
        else:
            result = {"status": "FAILED", "error_type": type(exc).__name__}
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
