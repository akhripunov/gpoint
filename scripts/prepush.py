#!/usr/bin/env python3
"""One fast, measured local gate between "code is written" and "ready to push".

The release rules keep heavy verification on GitHub: full pytest/pnpm/Playwright,
Docker builds and migration rehearsals are not repeated locally. This runner does
only what must be true before a push and prints the three durations the rules ask
for: SYNC, CLASSIFY and LOCAL_CHECKS.

It never pushes. Publication stays a separate, authorized action.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import importlib.util
import sys
import time
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "classify-release-prepush.py"
_spec = importlib.util.spec_from_file_location("classify_release_prepush", _MODULE_PATH)
_classifier = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_classifier)

CandidateBlocked = _classifier.CandidateBlocked
classify = _classifier.classify

EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True,
                                   stderr=subprocess.PIPE).strip()


def sync(repo: Path, *, fetch: bool) -> dict:
    """Refresh origin/main without touching the working tree."""
    started = time.monotonic()
    if fetch:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=repo,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    main_sha = _git(repo, "rev-parse", "origin/main")
    if not EXACT_SHA.fullmatch(main_sha):
        raise CandidateBlocked("origin/main does not resolve to an exact SHA")
    return {"main_sha": main_sha, "seconds": round(time.monotonic() - started, 2)}


def local_checks(repo: Path, base: str, head: str) -> tuple[list[str], list[str]]:
    """Return (failures, checks_run) for the changed surface only."""
    failures: list[str] = []
    ran: list[str] = []
    diff_check = subprocess.run(["git", "diff", "--check", f"{base}..{head}"], cwd=repo,
                                text=True, capture_output=True)
    if diff_check.returncode != 0:
        failures.extend(line for line in diff_check.stdout.splitlines() if line.strip()
                        or line.startswith("+"))
        if not failures:
            failures.append(f"git diff --check exited {diff_check.returncode}")
    ran.append("git-diff-check")

    changed = [line for line in _git(repo, "diff", "--name-only", f"{base}..{head}").splitlines() if line]
    python_files = [path for path in changed if path.endswith(".py")]
    for path in python_files:
        source = (repo / path).read_text(encoding="utf-8", errors="ignore")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            failures.append(f"{path}: {exc}")
    if python_files:
        ran.append(f"python-syntax({len(python_files)})")

    yaml_files = [path for path in changed if path.startswith(".github/workflows/") and path.endswith(".yml")]
    if yaml_files:
        try:
            import yaml  # noqa: PLC0415
        except ImportError:
            failures.append("PyYAML is required to validate changed workflows")
        else:
            for path in yaml_files:
                try:
                    yaml.safe_load((repo / path).read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{path}: {exc}")
        ran.append(f"workflow-yaml({len(yaml_files)})")
    return failures, ran


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--production-sha", required=True,
                        help="installed production source SHA, verified from a successful Deployment")
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--main-sha", default="")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    try:
        synced = sync(repo, fetch=not args.no_fetch)
        candidate = _git(repo, "rev-parse", "--verify", f"{args.candidate}^{{commit}}")
        main_sha = args.main_sha or synced["main_sha"]

        started = time.monotonic()
        result = classify(repo, args.production_sha, main_sha, candidate)
        classify_seconds = round(time.monotonic() - started, 2)

        started = time.monotonic()
        failures, ran = local_checks(repo, args.production_sha, candidate)
        checks_seconds = round(time.monotonic() - started, 2)
    except (CandidateBlocked, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, CandidateBlocked) else "Git command failed"
        print(json.dumps({"status": "BLOCK", "reason": message}, sort_keys=True))
        return 2

    report = {
        "status": "BLOCK" if failures else "READY",
        "mode": result["mode"],
        "reason": result["reason"],
        "candidate_sha": candidate,
        "production_sha": result["production_sha"],
        "main_sha": main_sha,
        "changed_paths": len(result["changed_paths"]),
        "checks_run": ran,
        "failures": failures,
        "timings": {
            "SYNC": synced["seconds"],
            "CLASSIFY": classify_seconds,
            "LOCAL_CHECKS": checks_seconds,
            "TOTAL": round(synced["seconds"] + classify_seconds + checks_seconds, 2),
        },
    }
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"PREPUSH_MODE={report['mode']} REASON={report['reason']} PATHS={report['changed_paths']}")
        for name, seconds in report["timings"].items():
            print(f"{name}_SECONDS={seconds}")
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"PREPUSH_STATUS={report['status']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
