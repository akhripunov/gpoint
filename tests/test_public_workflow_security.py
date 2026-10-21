"""Public workflow supply-chain regressions."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_public_workflow_actions_are_pinned_to_full_commit_shas():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if not action or action.startswith("./"):
                    continue
                reference = action.rsplit("@", 1)[-1]
                assert len(reference) == 40 and all(c in "0123456789abcdef" for c in reference), (
                    f"{path.name}:{job_name} uses mutable/non-SHA action ref {action}"
                )
