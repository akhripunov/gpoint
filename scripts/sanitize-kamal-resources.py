#!/usr/bin/env python3
"""Project docker stats output to safe per-container CPU and memory facts."""
from decimal import Decimal, InvalidOperation
import json
import re
import sys


UNITS = {"B": 1, "kB": 1000, "KiB": 1024, "MB": 1000**2, "MiB": 1024**2,
         "GB": 1000**3, "GiB": 1024**3, "TB": 1000**4, "TiB": 1024**4}


def number(value, label):
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"Docker stats {label} is invalid") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Docker stats {label} is invalid")
    return float(result)


def bytes_value(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KiB|MB|MiB|GB|GiB|TB|TiB)\s*", value)
    if not match:
        raise ValueError("Docker stats memory value is invalid")
    return int(Decimal(match.group(1)) * UNITS[match.group(2)])


def project(rows):
    facts = []
    seen_names = set()
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Docker stats row is invalid")
        name = row.get("Name")
        container_id = row.get("ID")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Docker stats container name is invalid")
        if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise ValueError("Docker stats container identity is invalid")
        if name in seen_names or container_id in seen_ids:
            raise ValueError("Docker stats contains duplicate container identities")
        seen_names.add(name)
        seen_ids.add(container_id)
        memory = row.get("MemUsage", "").split(" / ")
        if len(memory) != 2:
            raise ValueError("Docker stats memory usage is invalid")
        cpu = row.get("CPUPerc", "")
        mem_percent = row.get("MemPerc", "")
        if not isinstance(cpu, str) or not cpu.endswith("%") or not isinstance(mem_percent, str) or not mem_percent.endswith("%"):
            raise ValueError("Docker stats percentage is invalid")
        pids = row.get("PIDs", "")
        if not isinstance(pids, str) or not re.fullmatch(r"[0-9]+", pids):
            raise ValueError("Docker stats PID count is invalid")
        facts.append({
            "containerId": container_id,
            "containerName": name,
            "cpuPercent": number(cpu[:-1], "CPU percentage"),
            "memoryUsedBytes": bytes_value(memory[0]),
            "memoryLimitBytes": bytes_value(memory[1]),
            "memoryPercent": number(mem_percent[:-1], "memory percentage"),
            "pids": int(pids),
        })
    if not facts:
        raise ValueError("Docker stats sample is empty")
    return sorted(facts, key=lambda item: item["containerName"])


def main():
    try:
        rows = [json.loads(line) for line in sys.stdin if line.strip()]
        print(json.dumps(project(rows), sort_keys=True, separators=(",", ":")))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        print(f"DOCKER_RESOURCE_SAMPLE_BLOCKED={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
