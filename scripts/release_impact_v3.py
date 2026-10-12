#!/usr/bin/env python3
"""Release Orchestrator V3: fail-closed, provenance-bound CI impact selection."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

SHA_RE = re.compile(r"^[a-f0-9]{40}$")
LEDGER_URL = "https://api.github.com/repos/akhripunov/gpoint/contents/docs/release/state/production.json?ref=gpoint-release-ledger"
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
    verified = bool(SHA_RE.fullmatch(baseline or "") and baseline != candidate
                    and paths and reason == "verified-production-ledger")
    ui_only = verified and all(safe_ui_path(p) for p in paths)
    return {
        "version": VERSION,
        "candidateSha": candidate,
        "productionBaselineSha": baseline if verified else None,
        "baselineSource": "gpoint-release-ledger" if verified else None,
        "classification": "UI_ONLY" if ui_only else "FULL",
        "reason": "narrow-unchanged-backend" if ui_only else
                  ("impact-not-proven" if verified else reason),
        "changedPaths": paths if verified else [],
        "checks": {k: not ui_only for k in CHECKS},
        "invariantChecks": [
            "workspace_build", "workspace_typecheck", "workspace_tests", "release_tests",
            "artifact_migrations", "docker_images_exact_sha", "artifact_parity",
            "immutable_receipt", "deploy_rehearsal", "canonical_preflight",
            "production_approval"
        ],
    }


def read_ledger_baseline(token, opener=urllib.request.urlopen):
    if not token:
        raise ValueError("GitHub token unavailable")
    req = urllib.request.Request(
        LEDGER_URL, headers={"Authorization": "Bearer " + token,
                             "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28"})
    with opener(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ValueError("invalid ledger response")
    state = json.loads(base64.b64decode(payload["content"]))
    sha = state.get("currentProductionSha")
    if (not SHA_RE.fullmatch(sha or "") or
        state.get("lastReleaseStatus") != "COMPLETE" or
        state.get("serverDeployStatus") != "COMPLETE" or
        state.get("releaseOrigin") != "durable-production-release" or
        state.get("recoveryRequired") is not False):
        raise ValueError("canonical production state is not reusable")
    return sha


def build_ci_plan(repo, candidate, token=None, ref=None):
    if ref != "refs/heads/main":
        return make_plan(candidate, reason="not-main")
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        if head != candidate:
            raise ValueError("checkout SHA mismatch")
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo).strip():
            raise ValueError("dirty checkout")
        baseline = read_ledger_baseline(token)
        paths = changed_paths_between(repo, baseline, candidate)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, TimeoutError, json.JSONDecodeError):
        return make_plan(candidate, reason="canonical-proof-unavailable")
    return make_plan(candidate, baseline, paths, reason="verified-production-ledger")


def load_verified_plan(path, candidate, repo):
    """Recompute the diff before omitting any CI tests. Invalid plan blocks."""
    if not path:
        return make_plan(candidate, reason="no-v3-plan")
    plan = json.loads(Path(path).read_text())
    if plan.get("candidateSha") != candidate or plan.get("version") != VERSION:
        raise ValueError("impact plan candidate/version mismatch")
    if plan.get("classification") == "UI_ONLY":
        base = plan.get("productionBaselineSha")
        try:
            actual = changed_paths_between(repo, base, candidate)
        except (subprocess.CalledProcessError, ValueError):
            raise ValueError("impact evidence source tree could not be proved") from None
        if plan != make_plan(candidate, base, actual, reason="verified-production-ledger"):
            raise ValueError("impact evidence differs from exact source tree")
    else:
        if plan.get("checks") != {k: True for k in CHECKS}:
            raise ValueError("FULL plan cannot skip gates")
    return plan


def verify_receipt_baseline(receipt_path, candidate, token):
    """Immediately before artifact publication, reject live baseline drift."""
    proof = json.loads(Path(receipt_path).read_text())
    if proof.get("candidateSha") != candidate or proof.get("status") != "READY":
        raise ValueError("receipt identity is not the exact candidate")
    impact = proof.get("impactV3") or {}
    if impact.get("classification") == "UI_ONLY":
        if read_ledger_baseline(token) != impact.get("productionBaselineSha"):
            raise ValueError("production baseline changed after impact selection")
    else:
        if proof.get("gates", {}).get("postgres") != "PASS" or proof.get("gates", {}).get("gsalesPostgres") != "PASS":
            raise ValueError("unproved FULL release source gates")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output")
    mode.add_argument("--verify-receipt")
    opts = parser.parse_args()
    if opts.verify_receipt:
        verify_receipt_baseline(opts.verify_receipt, opts.candidate, os.environ.get("GITHUB_TOKEN"))
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
