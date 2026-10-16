import json
import unittest
from pathlib import Path

from scripts.unified_release_v1.core import (
    STANDARD_VERSION,
    UnifiedReleaseError,
    load_project_config,
    validate_standard_contract,
    validate_project_config,
)


ROOT = Path(__file__).resolve().parents[1]


class UnifiedReleaseV1ContractTests(unittest.TestCase):
    def test_project_configuration_conforms_to_required_v1_lifecycle(self):
        config = load_project_config(ROOT / "scripts" / "unified_release_v1" / "project.json")
        validate_project_config(config)
        self.assertEqual(config["standard_version"], STANDARD_VERSION)
        self.assertEqual(
            config["lifecycle"],
            ["COALESCE", "PREPARE", "AUTHORIZE", "PREFLIGHT", "PULL", "APPLY", "VERIFY", "EVIDENCE"],
        )

    def test_versioned_standard_contract_is_complete(self):
        contract = load_project_config(ROOT / "scripts" / "unified_release_v1" / "contract.json")
        validate_standard_contract(contract)

    def test_missing_security_gate_fails_closed(self):
        config = json.loads((ROOT / "scripts" / "unified_release_v1" / "project.json").read_text())
        config["project_gates"] = [gate for gate in config["project_gates"] if gate != "rollback_readiness"]
        with self.assertRaises(UnifiedReleaseError):
            validate_project_config(config)

    def test_owner_release_task_authorization_contract_is_explicit(self):
        contract = load_project_config(ROOT / "scripts" / "unified_release_v1" / "contract.json")
        authorization = contract["authorization"]
        self.assertTrue(authorization["owner_release_task_authorizes_full_cycle"])
        self.assertTrue(authorization["sha_resolved_after_integration"])
        self.assertFalse(authorization["repeat_owner_approval_required"])
        self.assertTrue(authorization["development_only_disallows_production"])
        self.assertTrue(authorization["authorization_registered_before_implementation"])
        self.assertIn("release_infrastructure", authorization["risk_contract_required_for"])
        authorization["repeat_owner_approval_required"] = True
        with self.assertRaises(UnifiedReleaseError):
            validate_standard_contract(contract)

    def test_declared_image_evidence_and_completion_contracts_fail_closed_when_weakened(self):
        original = load_project_config(ROOT / "scripts" / "unified_release_v1" / "contract.json")
        for field in ("image_transports", "required_evidence", "completion_requires"):
            weakened = json.loads(json.dumps(original))
            weakened[field] = weakened[field][:-1]
            with self.subTest(field=field), self.assertRaises(UnifiedReleaseError):
                validate_standard_contract(weakened)

    def test_archive_fallback_is_retained_during_migration(self):
        config = load_project_config(ROOT / "scripts" / "unified_release_v1" / "project.json")
        self.assertIn("ghcr-oci", config["image_transports"])
        self.assertIn("archive", config["image_transports"])
        self.assertTrue(config["archive_fallback_required_until_e2e_pass"])

    def test_project_uses_its_own_workflows_and_scoped_runner(self):
        config = load_project_config(ROOT / "scripts" / "unified_release_v1" / "project.json")
        self.assertTrue((ROOT / config["coalesce_workflow"]).is_file())
        self.assertTrue((ROOT / config["deploy_workflow"]).is_file())
        workflow = (ROOT / config["coalesce_workflow"]).read_text()
        self.assertIn("runs-on: [self-hosted, macOS, gpoint-production]", workflow)
        self.assertIn("needs.coalesce.outputs.result == 'WINNER'", workflow)
        trigger = workflow.split("\npermissions:", 1)[0]
        self.assertNotIn("workflow_dispatch:", trigger)
        self.assertIn("branches: [main]", trigger)
        prepare = workflow.split("  prepare:\n", 1)[1].split("    steps:\n", 1)[0]
        self.assertIn("github.ref == 'refs/heads/main'", prepare)
        self.assertIn("needs.coalesce.result == 'success'", prepare)
        self.assertIn("permissions:\n      contents: write\n      actions: read\n      packages: write", prepare)
        self.assertIn("docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9", workflow)

    def test_deploy_workflow_keeps_preflight_and_apply_in_project_executor(self):
        config = load_project_config(ROOT / "scripts" / "unified_release_v1" / "project.json")
        workflow = (ROOT / config["deploy_workflow"]).read_text()
        self.assertIn("gpoint-production", workflow)
        self.assertIn("IMAGE_TRANSPORT", workflow)
        release = (ROOT / "scripts" / "release-gpoint.mjs").read_text()
        self.assertIn("--dry-run", release)
        self.assertIn("preflight", release.lower())


if __name__ == "__main__":
    unittest.main()
