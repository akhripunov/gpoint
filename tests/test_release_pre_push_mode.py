import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = REPO_ROOT / "scripts" / "classify-release-prepush.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("initial\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def classify(repo: Path, baseline: str, candidate: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(CLASSIFIER), "--repo", str(repo),
         "--production-sha", baseline, "--main-sha", baseline,
         "--candidate-sha", candidate],
        cwd=REPO_ROOT, text=True, capture_output=True, check=False,
    )


def commit_file(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def test_readme_only_candidate_is_fast(tmp_path):
    repo, baseline = make_repo(tmp_path)
    candidate = commit_file(repo, "README.md", "one harmless note\n", "docs")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 0
    assert '"mode": "FAST"' in result.stdout
    assert '"changed_paths": ["README.md"]' in result.stdout


def test_code_change_is_full(tmp_path):
    repo, baseline = make_repo(tmp_path)
    candidate = commit_file(repo, "apps/api/src/main.ts", "export {};\n", "code")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 0
    assert '"mode": "FULL"' in result.stdout


def test_readme_on_top_of_accumulated_code_change_is_full(tmp_path):
    repo, baseline = make_repo(tmp_path)
    commit_file(repo, "apps/api/src/main.ts", "export {};\n", "code")
    candidate = commit_file(repo, "README.md", "extra note\n", "docs")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 0
    assert '"mode": "FULL"' in result.stdout
    assert '"apps/api/src/main.ts"' in result.stdout


def test_release_instruction_change_is_full(tmp_path):
    repo, baseline = make_repo(tmp_path)
    candidate = commit_file(repo, "AGENTS.md", "protected rules\n", "rules")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 0
    assert '"mode": "FULL"' in result.stdout


def test_candidate_outside_current_main_lineage_is_blocked(tmp_path):
    repo, baseline = make_repo(tmp_path)
    git(repo, "checkout", "--orphan", "unrelated")
    git(repo, "rm", "-rf", ".")
    (repo / "README.md").write_text("unrelated\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "unrelated")
    candidate = git(repo, "rev-parse", "HEAD")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 2
    assert "BLOCK" in result.stdout


def test_dirty_checkout_is_blocked(tmp_path):
    repo, baseline = make_repo(tmp_path)
    candidate = commit_file(repo, "README.md", "one harmless note\n", "docs")
    (repo / "README.md").write_text("uncommitted\n")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 2
    assert "clean" in result.stdout


def test_candidate_must_be_checked_out_head(tmp_path):
    repo, baseline = make_repo(tmp_path)
    candidate = commit_file(repo, "README.md", "one harmless note\n", "docs")
    commit_file(repo, "README.md", "later change\n", "later")

    result = classify(repo, baseline, candidate)

    assert result.returncode == 2
    assert "HEAD" in result.stdout
