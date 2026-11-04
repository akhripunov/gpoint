#!/usr/bin/env python3
"""Classify a GPoint release candidate's cumulative production diff."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")


class CandidateBlocked(ValueError):
    pass


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE,
    ).strip()


def _exact_commit(repo: Path, value: str, label: str) -> str:
    if not EXACT_SHA.fullmatch(value or ""):
        raise CandidateBlocked(f"{label} must be a full 40-character commit SHA")
    try:
        resolved = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise CandidateBlocked(f"{label} does not resolve to a commit") from exc
    if resolved != value:
        raise CandidateBlocked(f"{label} did not resolve to the supplied exact SHA")
    return resolved


def _is_ancestor(repo: Path, ancestor: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, candidate],
        cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise CandidateBlocked("Git could not verify candidate ancestry")
    return result.returncode == 0


def classify(repo: Path, production_sha: str, main_sha: str,
             candidate_sha: str) -> dict[str, object]:
    """Return FAST only for a cumulative README.md-only modification."""
    repo = repo.resolve()
    production = _exact_commit(repo, production_sha, "production_sha")
    main = _exact_commit(repo, main_sha, "main_sha")
    candidate = _exact_commit(repo, candidate_sha, "candidate_sha")
    if _git(repo, "rev-parse", "HEAD") != candidate:
        raise CandidateBlocked("candidate SHA must equal the checked-out HEAD")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CandidateBlocked("worktree must be clean before candidate classification")
    if not _is_ancestor(repo, production, candidate):
        raise CandidateBlocked("production SHA is not an ancestor of candidate")
    if not _is_ancestor(repo, main, candidate):
        raise CandidateBlocked("current main SHA is not an ancestor of candidate")
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--name-status", "-z", "--no-renames",
             f"{production}..{candidate}", "--"],
            cwd=repo, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise CandidateBlocked("Git could not read the cumulative production diff") from exc
    try:
        entries = raw.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise CandidateBlocked("candidate path inventory is not valid UTF-8") from exc
    if not entries or entries.pop() != "" or len(entries) % 2:
        raise CandidateBlocked("candidate path inventory is malformed")
    changes = sorted((status, path) for status, path in zip(entries[::2], entries[1::2]))
    if not changes:
        raise CandidateBlocked("candidate has no changes from production")
    if any(status not in {"M"} for status, _ in changes):
        mode = "FULL"
        reason = "candidate includes additions, deletions, copies, or other status changes"
    elif all(path == "README.md" for _, path in changes):
        mode = "FAST"
        reason = "cumulative production diff modifies only README.md"
    else:
        mode = "FULL"
        reason = "cumulative production diff includes a protected or non-README path"
    return {
        "schema": "gpoint-prepush-classification/v1",
        "mode": mode,
        "reason": reason,
        "production_sha": production,
        "main_sha": main,
        "candidate_sha": candidate,
        "changed_paths": [path for _, path in changes],
        "publication_authorization": "NOT_EVALUATED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--production-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    args = parser.parse_args()
    try:
        result = classify(args.repo, args.production_sha, args.main_sha,
                          args.candidate_sha)
    except CandidateBlocked as exc:
        print(json.dumps({
            "schema": "gpoint-prepush-classification/v1",
            "mode": "BLOCK",
            "reason": str(exc),
            "production_sha": args.production_sha,
            "main_sha": args.main_sha,
            "candidate_sha": args.candidate_sha,
            "publication_authorization": "NOT_EVALUATED",
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
