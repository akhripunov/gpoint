"""Fail-closed guard against reading quarantined legacy Benchmark V1 plaintext."""
from __future__ import annotations

import os
from pathlib import Path
import sys


LEGACY_CORPUS_BASENAME = "agata-benchmark-v1.jsonl"


def is_legacy_corpus_path(value: object) -> bool:
    if isinstance(value, int):
        return False
    try:
        path = Path(os.fsdecode(os.fspath(value))).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    return path.name == LEGACY_CORPUS_BASENAME


def _deny_legacy_open(event: str, args: tuple[object, ...]) -> None:
    if event == "open" and args and is_legacy_corpus_path(args[0]):
        raise PermissionError("legacy Benchmark V1 plaintext read blocked by Conversation Lab test guard")


def install_legacy_corpus_read_guard() -> None:
    """Install a process-wide audit hook before unittest discovery starts."""
    sys.addaudithook(_deny_legacy_open)
