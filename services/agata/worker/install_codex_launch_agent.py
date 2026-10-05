#!/usr/bin/env python3
"""Install the separate GPoint Codex inference worker LaunchAgent."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

LABEL = "one.gpoint.agata-codex-worker"
HOME = Path.home()
RUNTIME_ROOT = HOME / "Downloads/Codex/Workers/AGATA Workers"
SOURCES = tuple(Path(__file__).with_name(name) for name in (
    "gpoint_mac_worker.py", "agata_worker_pool.py", "lab_queue.py",
    "lab_fixture_manifest.json", "requirements.txt", "run_codex_worker.sh",
))
CODEX_APP_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


def prepare() -> None:
    if not CODEX_APP_PATH.is_file():
        raise RuntimeError("codex_cli_missing")
    python = subprocess.run(["python3.12", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                            check=True, capture_output=True, text=True).stdout.strip()
    if python != "3.12":
        raise RuntimeError("dedicated_python_312_required")
    if not all(path.is_file() for path in SOURCES):
        raise RuntimeError("worker_candidate_bundle_incomplete")

    # Preparation validates the candidate bundle only. It must never overwrite
    # the installed worker while a LaunchAgent may still be running.


def install() -> None:
    prepare()
    transition("apply")


def remove() -> None:
    transition("rollback")


def transition(action: str) -> None:
    script = Path(__file__).with_name("migrate_worker_runtime.py")
    subprocess.run([sys.executable, str(script), action], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "plan", "install", "remove"))
    action = parser.parse_args().action
    if action in {"prepare", "plan"}:
        prepare()
        if action == "plan":
            transition("plan")
    elif action == "install":
        install()
    else:
        remove()
    print(f"{LABEL} {action} verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
