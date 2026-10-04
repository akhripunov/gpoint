#!/usr/bin/env python3
"""Install the separate GPoint Codex inference worker LaunchAgent."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

LABEL = "one.gpoint.agata-codex-worker"
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
SOURCE = Path(__file__).with_name("gpoint_mac_worker.py")
LAB_QUEUE_SOURCE = Path(__file__).with_name("lab_queue.py")
WRAPPER_SOURCE = Path(__file__).with_name("run_codex_worker.sh")
PYTHON = SUPPORT / ".venv/bin/python3.12"
CODEX_APP_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


def prepare() -> None:
    if not CODEX_APP_PATH.is_file():
        raise RuntimeError("codex_cli_missing")
    if not PYTHON.is_file():
        raise RuntimeError("dedicated_python_312_venv_missing")
    version = subprocess.run(
        [str(PYTHON), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if version != "3.12":
        raise RuntimeError("dedicated_python_312_required")
    subprocess.run([str(PYTHON), "-c", "import httpx, certifi"], check=True, capture_output=True)
    if not SOURCE.is_file() or not LAB_QUEUE_SOURCE.is_file() or not WRAPPER_SOURCE.is_file():
        raise RuntimeError("worker_candidate_bundle_incomplete")

    # Preparation validates the candidate bundle only. It must never overwrite
    # the installed worker while a LaunchAgent may still be running.


def install() -> None:
    raise RuntimeError("owner_approved_transition_controller_required")


def remove() -> None:
    raise RuntimeError("explicit_rollback_controller_required")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "install", "remove"))
    action = parser.parse_args().action
    if action == "prepare":
        prepare()
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
