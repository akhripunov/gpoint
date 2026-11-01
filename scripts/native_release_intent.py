#!/usr/bin/env python3
"""Fail-closed trigger classification for allowlisted ME-52 rehearsal setup pushes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


PREP_MARKER = "GPOINT_MIGRATION_REHEARSAL_PREP=1"
PREP_ONLY_PATHS = frozenset({
    ".github/workflows/gpoint-native-release.yml",
    "scripts/native_release_intent.py",
    "tests/test_kamal_native_automatic_release.py",
    "tests/test_native_release_intent.py",
})


def classify_push(*, event_name: str, ref: str, commit_message: str,
                  changed_paths: set[str] | frozenset[str]) -> str:
    if event_name != "push" or ref != "refs/heads/main":
        return "NO_DEPLOY"
    marker_lines = [line for line in commit_message.splitlines()
                    if line.startswith("GPOINT_MIGRATION_REHEARSAL_PREP=")]
    if not marker_lines:
        return "DEPLOY"
    if marker_lines != [PREP_MARKER]:
        return "BLOCK"
    if changed_paths != PREP_ONLY_PATHS:
        return "BLOCK"
    return "PREPARE_ONLY"


def classify_dispatch(*, ref: str, candidate_sha: str, github_sha: str, actor: str,
                      repository_owner: str, migration_rehearsal: bool,
                      approve_production: bool) -> tuple[str, bool]:
    if migration_rehearsal:
        exact = bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and candidate_sha == github_sha
        owner = bool(repository_owner) and actor.lower() == repository_owner.lower()
        if (ref == "refs/heads/main" and exact and owner and not approve_production):
            return "REHEARSAL", False
        return "INVALID_REHEARSAL", False
    if (ref == "refs/heads/main"
            and re.fullmatch(r"[0-9a-f]{40}", candidate_sha)
            and candidate_sha == github_sha):
        return "DEPLOY", True
    return "NO_DEPLOY", False


def changed_paths(before: str, sha: str, *, repo: Path = Path.cwd()) -> set[str]:
    if (not re.fullmatch(r"[0-9a-f]{40}", before)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or before == "0" * 40):
        raise ValueError("push range must contain exact nonzero commit SHAs")
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{before}..{sha}"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    paths = set(filter(None, result.stdout.splitlines()))
    if not paths:
        raise ValueError("push range contains no changed paths")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--before", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--repository-owner", default="")
    parser.add_argument("--migration-rehearsal", action="store_true")
    parser.add_argument("--approve-production", action="store_true")
    args = parser.parse_args()

    try:
        if args.event == "push":
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref,
                                 commit_message=args.message, changed_paths=paths)
            deploy = mode == "DEPLOY"
        elif args.event == "workflow_dispatch":
            mode, deploy = classify_dispatch(
                ref=args.ref, candidate_sha=args.candidate_sha, github_sha=args.sha,
                actor=args.actor, repository_owner=args.repository_owner,
                migration_rehearsal=args.migration_rehearsal,
                approve_production=args.approve_production,
            )
        else:
            mode, deploy = "NO_DEPLOY", False
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print("NATIVE_RELEASE_INTENT=BLOCK", file=sys.stderr)
        print(f"reason={str(error)[:180]}", file=sys.stderr)
        return 2

    print(f"mode={mode}")
    print(f"deploy={'true' if deploy else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
