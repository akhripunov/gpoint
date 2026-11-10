#!/usr/bin/env python3
"""FAST/FULL local pre-push gate.

Reuses the same changed-surface classifier as CI, so a local run cannot invent a
different scope than GitHub will. FAST runs only the checks the touched surface
needs; FULL (release infrastructure, unknown paths, database) adds the full
Python suite that CI runs for the same surface.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from native_change_surface import changed_paths, classify  # noqa: E402

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"g1\.[A-Za-z0-9._-]{20,}"),
)
PROTECTED = ("AGENTS.md",)


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def added_lines(base, head):
    diff = run(["git", "diff", "-U0", base, head])
    return [line[1:] for line in diff.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    failures = []
    # 1. The candidate must be based on the current remote main.
    if run(["git", "rev-parse", "--verify", "--quiet", args.base]).returncode != 0:
        failures.append(f"{args.base} does not exist; run git fetch origin")
    else:
        behind = run(["git", "rev-list", "--count", f"{args.head}..{args.base}"]).stdout.strip()
        if behind not in {"", "0"}:
            failures.append(f"{args.head} is {behind} commit(s) behind {args.base}")

    # 2. Secrets and protected files in the candidate diff.
    for line in added_lines(args.base, args.head):
        for pattern in SECRET_PATTERNS:
            if pattern.search(line):
                failures.append(f"possible secret in added line: {pattern.pattern}")
                break
    for path in changed_paths(args.base, args.head) if run(
            ["git", "rev-parse", "--verify", "--quiet", args.base]).returncode == 0 else []:
        if path in PROTECTED:
            print(f"PROTECTED_PATH_CHANGED={path} (FULL)")

    surface = classify(changed_paths(args.base, args.head)) if not failures else classify([])
    mode = "FULL" if surface["full_ci"] else "FAST"
    checks = ["secrets", "protected-paths", "syntax"]
    if mode == "FULL":
        checks.append("python-suite")
    if surface["miniapp"]:
        checks.append("miniapp-tests")
    if surface["eros"]:
        checks.append("eros-e2e")

    print(f"PREPUSH_MODE={mode} files={surface['files']} checks={','.join(checks)}")
    if args.dry_run:
        return 0 if not failures else 1

    # 3. Syntax of changed Python files, then the suite selected by the surface.
    changed = changed_paths(args.base, args.head)
    for path in (p for p in changed if p.endswith(".py") and Path(p).is_file()):
        try:
            ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        except SyntaxError:
            failures.append(f"python syntax error: {path}")

    if mode == "FULL":
        workflow = Path(".github/workflows/gpoint-native-release.yml").read_text()
        match = re.search(r"python -m pytest -q\n((?:\s+tests/[^\n]+\n)+)", workflow)
        if not match:
            failures.append("cannot read the CI python suite from the workflow")
        else:
            files = match.group(1).split()
            if run([sys.executable, "-m", "pytest", "-q", *files]).returncode != 0:
                failures.append("CI python suite failed")
    else:
        local_tests = [p for p in changed if p.startswith("tests/test_") and p.endswith(".py")]
        if local_tests and run([sys.executable, "-m", "pytest", "-q", *local_tests]).returncode != 0:
            failures.append("changed test files failed")

    for failure in failures:
        print(f"PREPUSH_BLOCKED={failure}", file=sys.stderr)
    print("PREPUSH=PASS" if not failures else "PREPUSH=FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
