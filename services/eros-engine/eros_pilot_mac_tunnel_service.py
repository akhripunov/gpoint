#!/usr/bin/env python3
"""Build the narrow launchd manifest for the loopback-only Eros Ollama tunnel."""
from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path


LABEL = "com.gpoint.eros-pilot-ollama-tunnel"
TARGET = re.compile(r"^root@(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\d{1,3}(?:\.\d{1,3}){3})$")


def _absolute(value: str, field: str) -> str:
    path = Path(value)
    if not path.is_absolute() or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be an absolute path")
    return str(path)


def launchd_running_pid(output: str) -> str:
    states = re.findall(r"^\s*state = ([^\r\n]+)$", output, re.MULTILINE)
    pids = re.findall(r"^\s*pid = ([0-9]+)$", output, re.MULTILINE)
    if states != ["running"] or len(pids) != 1 or int(pids[0]) <= 1:
        raise ValueError("LaunchDaemon is loaded but its tunnel process is not running")
    return pids[0]


def build_plist(*, username: str, home: str, target: str, identity: str,
                known_hosts: str, script: str, node: str, source_sha: str) -> dict:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", username):
        raise ValueError("launchd username is invalid")
    if not TARGET.fullmatch(target):
        raise ValueError("VPS SSH target must be root@hostname or root@IPv4")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source SHA must be a full lowercase commit SHA")
    home = _absolute(home, "home")
    identity = _absolute(identity, "identity")
    known_hosts = _absolute(known_hosts, "known_hosts")
    script = _absolute(script, "script")
    node = _absolute(node, "node")
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", script],
        "UserName": username,
        "WorkingDirectory": home,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "Umask": 0o077,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {
            "HOME": home,
            "PATH": f"{Path(node).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "EROS_PILOT_NODE_BIN": node,
            "EROS_PILOT_SOURCE_SHA": source_sha,
            "EROS_PILOT_VPS_SSH_TARGET": target,
            "GPOINT_SSH_IDENTITY_FILE": identity,
            "GPOINT_KNOWN_HOSTS_FILE": known_hosts,
        },
    }


def main() -> int:
    if sys.argv[1:] == ["--launchd-pid"]:
        try:
            print(launchd_running_pid(sys.stdin.read()))
        except ValueError as error:
            print(f"EROS_PILOT_LAUNCHD_BLOCKED={error}", file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        plistlib.dump(build_plist(**vars(args)), sys.stdout.buffer, fmt=plistlib.FMT_XML, sort_keys=True)
    except (ValueError, OSError) as error:
        print(f"EROS_PILOT_LAUNCHD_PLIST_BLOCKED={error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
