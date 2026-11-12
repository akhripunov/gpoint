"""Fast pre-push gate: modes, timings and fail-closed behaviour."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepush.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo, text=True).strip()


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    (work / "apps").mkdir()
    (work / "apps" / "api").mkdir()
    (work / "apps" / "api" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    production = git(work, "rev-parse", "HEAD")
    git(work, "remote", "add", "origin", str(work))
    git(work, "fetch", "-q", "origin", "main")
    return work, production


def run(repo: Path, production: str, *extra: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--production-sha", production,
         "--no-fetch", "--json", *extra],
        text=True, capture_output=True)
    return result.returncode, json.loads(result.stdout or "{}")


def test_readme_only_candidate_is_fast_and_timed(repo):
    work, production = repo
    (work / "README.md").write_text("docs only\n", encoding="utf-8")
    git(work, "commit", "-qam", "docs")
    code, report = run(work, production)
    assert code == 0
    assert report["status"] == "READY"
    assert report["mode"] == "FAST"
    assert set(report["timings"]) == {"SYNC", "CLASSIFY", "LOCAL_CHECKS", "TOTAL"}
    assert report["timings"]["TOTAL"] >= 0
    assert "git-diff-check" in report["checks_run"]


def test_runtime_change_is_full_mode(repo):
    work, production = repo
    (work / "apps" / "api" / "main.ts").write_text("export const x = 2;\n", encoding="utf-8")
    git(work, "commit", "-qam", "runtime")
    code, report = run(work, production)
    assert code == 0
    assert report["mode"] == "FULL"


def test_broken_python_syntax_blocks_before_push(repo):
    work, production = repo
    (work / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    git(work, "add", "broken.py")
    git(work, "commit", "-qm", "broken")
    code, report = run(work, production)
    assert code == 1
    assert report["status"] == "BLOCK"
    assert any("broken.py" in failure for failure in report["failures"])


def test_dirty_worktree_blocks(repo):
    work, production = repo
    (work / "README.md").write_text("dirty\n", encoding="utf-8")
    code, report = run(work, production)
    assert code == 2
    assert report["status"] == "BLOCK"
    assert "clean" in report["reason"]
