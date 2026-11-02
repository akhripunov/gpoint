from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github/workflows/gpoint-eros-pilot-tunnel-postcheck.yml"


def test_postcheck_runs_only_after_exact_native_apply_workflow_completion():
    workflow = yaml.safe_load(PATH.read_text())
    trigger = workflow.get("on", workflow.get(True))["workflow_run"]
    assert trigger["workflows"] == ["GPoint Native Release"]
    assert trigger["types"] == ["completed"]
    assert "main" in trigger["branches"]
    jobs = workflow["jobs"]
    detect = jobs["detect-successful-pilot-apply"]
    detector = "\n".join(step.get("run", "") for step in detect["steps"])
    detector_env = detect["steps"][0]["env"]
    assert detector_env["EVENT_NAME"] == "${{ github.event.workflow_run.event }}"
    assert "Protected Native Kamal synthetic Eros Pilot Apply" in detector
    assert "conclusion')=='success'" in detector
    assert detector_env["EVENT_ACTOR"] == "${{ github.event.workflow_run.actor.login }}"
    assert detector_env["REPOSITORY_OWNER"] == "${{ github.repository_owner }}"
    assert 'test "$EVENT_NAME" = workflow_dispatch' in detector
    assert 'test "$EVENT_BRANCH" = main' in detector
    assert 'test "$EVENT_ACTOR" = "$REPOSITORY_OWNER"' in detector
    postcheck = jobs["verify-tunnel-after-workflow-completion"]
    assert "apply_succeeded == 'true'" in postcheck["if"]
    assert postcheck["runs-on"] == ["self-hosted", "macOS", "gpoint-production"]
    assert postcheck["permissions"] == {"contents": "read"}
    assert "workflow_run.head_sha" in str(postcheck["steps"])
    assert not any("actions/checkout" in step.get("uses", "")
                   for step in postcheck["steps"])
    assert any('"$HOME/Library/Application Support/GPoint/Eros Pilot/eros-pilot-mac-tunnel-service.sh" verify' in step.get("run", "")
               for step in postcheck["steps"])
