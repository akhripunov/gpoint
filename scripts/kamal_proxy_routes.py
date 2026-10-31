#!/usr/bin/env python3
"""Strict parser for the Kamal Proxy v0.9.2 kamal-proxy list table."""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HEADER = ("Service", "Host", "Path", "Target", "State", "TLS")


def parse_routes(output: str) -> list[dict[str, str]]:
    """Parse the pinned proxy's styled table and reject unknown output."""
    if not isinstance(output, str):
        raise ValueError("proxy route output is not text")
    lines = [line.strip() for line in ANSI.sub("", output).splitlines() if line.strip()]
    if not lines or tuple(lines[0].split()) != HEADER:
        raise ValueError("proxy route table header is unknown")
    routes: list[dict[str, str]] = []
    services: set[str] = set()
    for line in lines[1:]:
        cells = line.split()
        if len(cells) != len(HEADER):
            raise ValueError("proxy route table row is incomplete")
        route = dict(zip(HEADER, cells))
        if route["Service"] in services:
            raise ValueError("proxy service has multiple or duplicate route rows")
        if route["TLS"] not in {"yes", "no"}:
            raise ValueError("proxy TLS state is unknown")
        if not route["Host"] or not route["Path"].startswith("/") or not route["State"]:
            raise ValueError("proxy route identity is incomplete")
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", route["Target"]):
            raise ValueError("proxy target format is unknown")
        services.add(route["Service"])
        routes.append(route)
    return routes


def validate_gpoint_routes(
    routes: list[dict[str, str]], candidate_targets: dict[str, tuple[str, int]],
    host: str = "gpoint.one",
) -> list[str]:
    """Authorize removal only for exact GPoint routes targeting this attempt."""
    expected = {
        "gpoint-core": ("/api", 3000),
        "gpoint-miniapp": ("/", 80),
    }
    if not set(candidate_targets).issubset(expected):
        raise ValueError("unexpected candidate service identity")
    if not routes:
        return []
    allowed: dict[str, tuple[str, int]] = {}
    for service, (container_id, port) in candidate_targets.items():
        if not re.fullmatch(r"[0-9a-f]{64}", container_id) or port != expected[service][1]:
            raise ValueError("candidate target identity is invalid")
        allowed[service] = (container_id[:12], port)
    services = []
    for route in routes:
        service = route["Service"]
        if service not in expected or service not in allowed:
            raise ValueError("proxy contains a route outside the candidate attempt")
        path, port = expected[service]
        target_id, target_port = allowed[service]
        if (route["Host"] != host or route["Path"] != path or route["TLS"] != "yes"
                or route["State"] != "running"
                or route["Target"] != f"{target_id}:{target_port}"):
            raise ValueError("proxy route does not match the exact candidate")
        services.append(service)
    return sorted(services)
