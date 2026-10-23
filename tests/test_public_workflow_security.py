"""Public workflow supply-chain regressions."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_prepare_runs_public_security_checks_after_pip_bootstrap():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gpoint-prepare.yml").read_text())
    steps = next(
        job["steps"] for job in workflow["jobs"].values()
        if any(step.get("name") == "Install AGATA test dependencies" for step in job.get("steps", []))
    )
    install = next(step["run"] for step in steps if step.get("name") == "Install AGATA test dependencies")
    assert "python -m pip install --no-cache-dir pip==26.2.2" in install
    assert install.index("pip==26.2.2") < install.index("'./services/agata[test]'")
    checks = next(step["run"] for step in steps if step.get("name") == "Test release preflight coordinator")
    assert "python3 -m pytest -q tests/test_public_workflow_security.py" in checks


def test_python_images_upgrade_pip_before_installing_project():
    for service in ("agata", "gsales"):
        dockerfile = (ROOT / "services" / service / "Dockerfile").read_text()
        bootstrap = "RUN python -m pip install --no-cache-dir pip==26.2.2"
        assert bootstrap in dockerfile, service
        assert dockerfile.index(bootstrap) < dockerfile.index("RUN pip install"), service


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


def test_host_ssh_policy_preserves_keys_and_disables_password_login():
    policy = ROOT / "deploy/security/sshd-key-only.conf"
    settings = {}
    for line in policy.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, value = line.split(maxsplit=1)
            assert name.lower() not in settings
            settings[name.lower()] = value
    assert settings == {
        "pubkeyauthentication": "yes",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "permitrootlogin": "prohibit-password",
        "permitemptypasswords": "no",
        "permituserenvironment": "no",
        "x11forwarding": "no",
    }
