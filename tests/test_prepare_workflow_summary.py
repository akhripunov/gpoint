"""Regression coverage for shell scripts embedded in the Prepare workflow."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class PrepareWorkflowSummaryTests(unittest.TestCase):
    def test_owner_summary_script_has_valid_shell_syntax(self) -> None:
        workflow = Path(__file__).parents[1] / ".github/workflows/gpoint-prepare.yml"
        text = workflow.read_text(encoding="utf-8")
        owner_summary = text.split("      - name: Owner summary\n", maxsplit=1)[1]
        run_block = owner_summary.split("        run: |\n", maxsplit=1)[1].split(
            "\n\n  publish:", maxsplit=1
        )[0]
        script = "\n".join(line[10:] if line.startswith("          ") else line for line in run_block.splitlines())

        result = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
