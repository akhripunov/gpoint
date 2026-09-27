#!/usr/bin/env python3
"""Install or remove the isolated GPoint AGATA worker LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

LABEL = "one.gpoint.agata-worker"
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/GPoint/agata-worker"
PLIST = HOME / f"Library/LaunchAgents/{LABEL}.plist"
SOURCE = Path(__file__).with_name("gpoint_mac_worker.py")
INSTALLED = SUPPORT / "gpoint_mac_worker.py"


def install() -> None:
    config = HOME / ".config/gpoint/agata-worker.env"
    if not config.is_file() or config.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    SUPPORT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (HOME / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    (HOME / "Library/Logs/GPoint").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, INSTALLED)
    INSTALLED.chmod(0o700)
    document = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED), "watch"],
        "WorkingDirectory": str(SUPPORT),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(HOME / "Library/Logs/GPoint/agata-worker.log"),
        "StandardErrorPath": str(HOME / "Library/Logs/GPoint/agata-worker.error.log"),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "remove"))
    action = parser.parse_args().action
    if action == "install":
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
