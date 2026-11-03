import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verify-kamal-optional-agata-role.sh"


def _fake_docker(tmp_path: Path, names: list[str]) -> dict[str, str]:
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/env python3\nimport os\nprint(os.environ.get('FAKE_NAMES', ''))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_NAMES"] = "\n".join(names)
    return env


def _run(tmp_path: Path, role: str, names: list[str]):
    return subprocess.run(["bash", str(VERIFIER), role], env=_fake_docker(tmp_path, names),
                          check=False, capture_output=True, text=True)


def test_optional_agata_role_is_explicitly_classified_when_absent_or_single(tmp_path):
    absent = _run(tmp_path, "api", [])
    assert absent.returncode == 0
    assert absent.stdout.strip() == "ROLE_PRESENT=false"

    present = _run(tmp_path, "memory-worker", ["container-id"])
    assert present.returncode == 0
    assert present.stdout.strip() == "ROLE_PRESENT=true"


def test_optional_agata_role_rejects_duplicates_and_unrecognized_role(tmp_path):
    duplicate = _run(tmp_path, "api", ["a", "b"])
    assert duplicate.returncode != 0
    invalid = _run(tmp_path, "gpoint-core-api", [])
    assert invalid.returncode != 0
