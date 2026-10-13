#!/usr/bin/env python3
"""Provenance-bound component advisory. It never authorizes skipping V3 gates."""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path

SHA = re.compile(r"^[a-f0-9]{40}$")
SAFETY = ("immutable_baseline", "exact_candidate", "image_integrity",
          "migration_compatibility", "preflight", "owner_approval",
          "runtime_health", "rollback", "recovery_evidence")


def safe_path(path):
    return (isinstance(path, str) and bool(path) and
            not path.startswith(("/", "./", "../")) and
            "\\" not in path and "\x00" not in path and "\n" not in path and
            all(part not in ("", ".", "..") for part in path.split("/")))


def owner(path):
    if not safe_path(path):
        return None
    if (path == "apps/miniapp/index.html" or
        (path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")) and
         len(path.split("/")) >= 4)):
        return "ui"
    if (path.startswith("services/agata/app/responses/") and
            path.endswith(".py") and len(path.split("/")) >= 5):
        return "agata"
    if path in ("services/agata/tests/test_message_chronology.py",
                "services/agata/tests/test_internal_api_e2e.py"):
        return "agata"
    return None


def plan(candidate, baseline, entries, *, baseline_verified=False):
    if not SHA.fullmatch(candidate or ""):
        raise ValueError("invalid candidate SHA")
    entries = list(entries)
    affected = set()
    unsafe = []
    seen = set()
    for change, path in entries:
        if path in seen:
            unsafe.append(path)
        seen.add(path)
        target = owner(path) if change in ("A", "M") else None
        if target:
            affected.add(target)
        else:
            unsafe.append(path)
    verified = (bool(SHA.fullmatch(baseline or "")) and
                candidate != baseline and baseline_verified)
    narrow = verified and bool(entries) and not unsafe and len(affected) == 1
    component = next(iter(affected)) if narrow else None
    consumers = {
        "ui": ["miniapp_static"],
        "agata": ["agata-api", "agata-memory-worker",
                  "gsales-agata-api", "gsales-agata-memory-worker"],
    }.get(component, ["ui", "app", "agata", "gsales"])
    return {
        "schema": 1, "candidateSha": candidate,
        "productionBaselineSha": baseline if verified else None,
        "baselineProvenance": "immutable-COMPLETE" if verified else None,
        "classification": "COMPONENT_CANDIDATE" if narrow else "FULL",
        "component": component,
        "affectedRuntimeServices": consumers,
        "changedPaths": sorted(seen) if verified else [],
        "unsafePaths": sorted(set(unsafe)) if verified else [],
        "maySkipV3Gates": False, "mayDeploySelectively": False,
        "requiredSafetyGates": list(SAFETY),
    }


def git_changes(repo, baseline, candidate):
    if not SHA.fullmatch(baseline or "") or not SHA.fullmatch(candidate or ""):
        raise ValueError("invalid source SHA")
    subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate],
                   cwd=repo, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    raw = subprocess.check_output(["git", "diff", "--name-status", "-z",
                                   "--no-renames", baseline, candidate, "--"],
                                  cwd=repo)
    parts = raw.decode("utf8").split("\x00")
    if not parts or parts.pop() != "" or len(parts) % 2:
        raise ValueError("malformed diff")
    result = list(zip(parts[::2], parts[1::2]))
    for status, path in result[:]:
        if status in ("A", "M") and safe_path(path):
            ls = subprocess.check_output(["git", "ls-tree", candidate, "--", path],
                                         cwd=repo).decode()
            if not ls.startswith(("100644 blob ", "100755 blob ")):
                result.append(("UNSAFE_MODE", path))
                break
    return result


def verified_plan(repo, baseline, candidate, *, baseline_verified):
    if not baseline_verified or not SHA.fullmatch(baseline or ""):
        return plan(candidate, baseline, [], baseline_verified=False)
    try:
        return plan(candidate, baseline, git_changes(repo, baseline, candidate),
                    baseline_verified=True)
    except (subprocess.CalledProcessError, OSError, UnicodeError, ValueError):
        return plan(candidate, baseline, [], baseline_verified=False)


def verify_again(repo, baseline, candidate, receipt):
    actual = verified_plan(repo, baseline, candidate, baseline_verified=True)
    if actual != receipt or receipt.get("mayDeploySelectively") or receipt.get("maySkipV3Gates"):
        raise ValueError("advisory source scope invalid")
    return actual


def main():
    # Advisory only: V3 already establishes canonical source provenance.
    # This output never controls V3's bundle verifier or deploy executor.
    import argparse
    from release_impact_v3 import load_verified_plan
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--v3-plan", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                               text=True).strip() != args.candidate:
        raise ValueError("V4 checkout SHA mismatch")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root).strip():
        raise ValueError("V4 source checkout must be clean")
    v3 = load_verified_plan(args.v3_plan, args.candidate, root)
    baseline = v3.get("productionBaselineSha")
    proved = (
        v3.get("baselineSource") == "gpoint-release-ledger" and
        v3.get("reason") in ("narrow-unchanged-backend", "impact-not-proven") and
        isinstance(baseline, str)
    )
    advisory = verified_plan(root, baseline, args.candidate,
                             baseline_verified=bool(proved))
    if advisory["maySkipV3Gates"] or advisory["mayDeploySelectively"]:
        raise ValueError("V4 advisory cannot bypass operational V3 gates")
    destination = Path(args.output).resolve()
    if destination.is_relative_to(root):
        raise ValueError("advisory evidence must stay outside checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(advisory, indent=2, sort_keys=True) + "\n")
    print("V4_COMPONENT_ADVISORY=" + advisory["classification"])
    print("V4_COMPONENT_OWNER=" + str(advisory["component"]))
    print("V4_PARTIAL_DEPLOY_ENABLED=NO")


if __name__ == "__main__":
    main()
