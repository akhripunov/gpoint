#!/usr/bin/env python3
"""Install the separate GPoint Codex inference worker LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

LABEL = "one.gpoint.agata-codex-worker"
LEGACY_LABEL = "one.gpoint.agata-worker"
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/GPoint/agata-codex-worker"
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / f"Library/LaunchAgents/{LABEL}.plist"
SOURCE = Path(__file__).with_name("gpoint_mac_worker.py")
WRAPPER_SOURCE = Path(__file__).with_name("run_codex_worker.sh")
INSTALLED = SUPPORT / "gpoint_mac_worker.py"
WRAPPER = SUPPORT / "run_codex_worker.sh"
PYTHON = SUPPORT / ".venv/bin/python3.12"
LOG_DIR = HOME / "Library/Logs/GPoint"
CODEX_APP_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")


def _loaded(label: str) -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


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

    SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    SUPPORT.chmod(0o700)
    shutil.copy2(SOURCE, INSTALLED)
    INSTALLED.chmod(0o700)
    shutil.copy2(WRAPPER_SOURCE, WRAPPER)
    WRAPPER.chmod(0o700)


def install() -> None:
    if _loaded(LEGACY_LABEL):
        raise RuntimeError("legacy_gpoint_worker_loaded; stop it before installing the single-worker Codex agent")
    if not CONFIG.is_file() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    prepare()
    (HOME / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    document = {
        "Label": LABEL,
        "ProgramArguments": [str(WRAPPER), "watch"],
        "WorkingDirectory": str(SUPPORT),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "GPOINT_AGATA_WORKER_CONFIG": str(CONFIG),
            "GPOINT_AGATA_WORKER_SUPPORT": str(SUPPORT),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG_DIR / "agata-codex-worker.log"),
        "StandardErrorPath": str(LOG_DIR / "agata-codex-worker.error.log"),
    }
    with PLIST.open("wb") as output:
        plistlib.dump(document, output, sort_keys=True)
    PLIST.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)


def remove() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    PLIST.unlink(missing_ok=True)
    INSTALLED.unlink(missing_ok=True)
    WRAPPER.unlink(missing_ok=True)


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
    print(f"{LABEL} {action} complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
