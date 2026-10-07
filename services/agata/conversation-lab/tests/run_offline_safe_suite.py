"""Run Conversation Lab offline tests with legacy corpus reads denied."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

from legacy_corpus_guard import install_legacy_corpus_read_guard


TESTS = Path(__file__).resolve().parent


def main() -> int:
    install_legacy_corpus_read_guard()
    suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
