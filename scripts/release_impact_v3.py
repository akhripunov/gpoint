#!/usr/bin/env python3
"""Release Orchestrator V3: fail-closed, provenance-bound CI impact selection."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

SHA_RE = re.compile(r"^[a-f0-9]{40}$")
CHECKS = ("agata_postgres_rehearsal", "gsales_postgres_regression")
VERSION = 1


def safe_ui_path(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    if path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")):
        return len(path.split("/")) >= 4 and all(p not in ("", ".", "..") for p in path.split("/"))
    return path == "apps/miniapp/index.html"


def changed_paths_between(repo, baseline, candidate):
    if not SHA_RE.fullmatch(baseline or "") or not SHA_RE.fullmatch(candidate or ""):
        raise ValueError("invalid source SHA")
    subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate], cwd=repo,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", "-z", baseline, candidate, "--"], cwd=repo)
    paths = sorted(set(p.decode("utf-8") for p in data.split(b"\x00") if p))
    if any(x.startswith("/") or x.startswith("../") or "/../" in x for x in paths):
        raise ValueError("untrusted diff path")
    return paths


def make_plan(candidate, baseline=None, changed_paths=None, reason="unknown"):
    if not SHA_RE.fullmatch(candidate or ""):
        raise ValueError("candidate requires exact SHA")
    paths = sorted(set(changed_paths or []))
    return {
        "version": VERSION,
        "candidateSha": candidate,
        # Prepare has no trusted live VPS readback. A Git ref or historical
        # release record is not enough to prove the production baseline.
        "productionBaselineSha": None,
        "baselineSource": None,
        "classification": "FULL",
        "reason": reason if reason != "unknown" else "verified-live-baseline-unavailable",
        "changedPaths": [],
        "checks": {k: True for k in CHECKS},
        "invariantChecks": [
            "workspace_build", "workspace_typecheck", "workspace_tests", "release_tests",
            "artifact_migrations", "docker_images_exact_sha", "artifact_parity",
            "immutable_receipt", "deploy_rehearsal", "canonical_preflight",
            "production_approval"
        ],
    }


def build_ci_plan(repo, candidate, token=None, ref=None):
    if ref != "refs/heads/main":
        return make_plan(candidate, reason="not-main")
    return make_plan(candidate, reason="verified-live-baseline-unavailable")


def load_verified_plan(path, candidate, repo):
    """Recompute the diff before omitting any CI tests. Invalid plan blocks."""
    if not path:
        return make_plan(candidate, reason="no-v3-plan")
    plan = json.loads(Path(path).read_text())
    if plan.get("candidateSha") != candidate or plan.get("version") != VERSION:
        raise ValueError("impact plan candidate/version mismatch")
    if (plan.get("classification") != "FULL" or
            plan.get("checks") != {k: True for k in CHECKS} or
            plan.get("productionBaselineSha") is not None or
            plan.get("baselineSource") is not None or
            plan.get("changedPaths") != []):
        raise ValueError("Prepare requires FULL gates until a verified live baseline is available")
    return plan


def verify_receipt_baseline(receipt_path, candidate, token=None):
    """Immediately before publication, require the full source regression gates."""
    proof = json.loads(Path(receipt_path).read_text())
    if proof.get("candidateSha") != candidate or proof.get("status") != "READY":
        raise ValueError("receipt identity is not the exact candidate")
    impact = proof.get("impactV3") or {}
    if (impact.get("classification") != "FULL" or impact.get("productionBaselineSha") is not None
            or impact.get("baselineSource") is not None or impact.get("changedPaths") != []
            or impact.get("checks") != {k: True for k in CHECKS}
            or proof.get("gates", {}).get("postgres") != "PASS"
            or proof.get("gates", {}).get("gsalesPostgres") != "PASS"):
        raise ValueError("unproved FULL release source gates")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output")
    mode.add_argument("--verify-receipt")
    opts = parser.parse_args()
    if opts.verify_receipt:
        verify_receipt_baseline(opts.verify_receipt, opts.candidate)
        print("V3_BASELINE_RECHECK=PASS")
        return
    repo = Path(__file__).resolve().parents[1]
    plan = build_ci_plan(repo, opts.candidate, os.environ.get("GITHUB_TOKEN"),
                         os.environ.get("GITHUB_REF"))
    output = Path(opts.output).resolve()
    if os.path.commonpath([str(output), str(repo)]) == str(repo):
        raise ValueError("plan output must be outside source checkout")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print("V3_IMPACT=" + plan["classification"] + " REASON=" + plan["reason"])
    print("V3_CHANGED_PATH_COUNT=" + str(len(plan["changedPaths"])))
    print("V3_DB_REGRESSION=" + ("FULL" if plan["checks"]["gsales_postgres_regression"] else "UNCHANGED_PRODUCTION"))


if __name__ == "__main__":
    main()
