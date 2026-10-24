from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
SELECTOR = (ROOT / 'scripts/select-release-run.py').read_text()


class ErosAclWorkflowTests(unittest.TestCase):
    def test_acl_modes_are_separate_from_normal_and_pilot_apply(self):
        self.assertIn('options: [preflight, production, task-scoped-auto, reconcile-authorization, eros-pilot, eros-acl-plan, eros-acl-apply, eros-acl-rollback]', WORKFLOW)
        self.assertRegex(WORKFLOW, r'approve_eros_acl:[\s\S]{0,200}default: false')
        deploy = WORKFLOW.split('  deploy:\n', 1)[1].split('  deploy-eros-pilot:', 1)[0]
        self.assertIn("inputs.mode != 'eros-acl-plan'", deploy)
        self.assertIn("inputs.mode != 'eros-acl-apply'", deploy)
        self.assertIn("inputs.mode != 'eros-acl-rollback'", deploy)
        acl = WORKFLOW.split('  deploy-eros-acl:', 1)[1].split('  reconcile-authorization:', 1)[0]
        self.assertIn("if: inputs.mode == 'eros-acl-plan' || inputs.mode == 'eros-acl-apply' || inputs.mode == 'eros-acl-rollback'", acl)
        self.assertIn('runs-on: [self-hosted, macOS, gpoint-production]', acl)
        self.assertIn('scripts/release-runner-readiness.mjs', acl)
        self.assertIn('scripts/select-release-run.py', acl)
        self.assertIn('scripts/prepared_artifact_cache.py', acl)
        self.assertIn('scripts/eros-postgres-acl-deploy.mjs', acl)
        self.assertNotIn('automated-release.mjs', acl)
        self.assertNotIn('gsales', acl.lower())
        self.assertRegex(acl, r'permissions:\n      contents: read\n      actions: read\n      packages: read')

    def test_linear_publish_does_not_report_acl_operation_as_release(self):
        self.assertIn('deploy-eros-acl, reconcile-authorization]', WORKFLOW)
        self.assertRegex(WORKFLOW, r'if: always\(\) && github\.ref == \'refs/heads/main\' && inputs\.mode != \'eros-acl-plan\' && inputs\.mode != \'eros-acl-apply\' && inputs\.mode != \'eros-acl-rollback\'')

    def test_selector_has_exact_sha_owner_gate_for_apply_only(self):
        self.assertIn("mode in ('eros-acl-apply', 'eros-acl-rollback') and eros_acl_approval != 'true'", SELECTOR)
        self.assertIn("'eros-acl-rollback')", SELECTOR)
        self.assertNotIn("mode in ('eros-acl-plan', 'eros-acl-apply') and", SELECTOR)


if __name__ == '__main__':
    unittest.main()
