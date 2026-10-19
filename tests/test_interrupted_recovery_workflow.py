import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/gpoint-recover-interrupted.yml'
CACHE = ROOT / 'scripts/prepared_artifact_cache.py'


class InterruptedRecoveryWorkflowTests(unittest.TestCase):
    def test_owner_only_exact_original_deployment_no_apply(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        fields = workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(set(fields), {'candidate', 'deployment_id', 'original_run_id',
                                      'prepare_run_id', 'prepare_run_attempt', 'approve_recovery'})
        self.assertFalse(fields['approve_recovery']['default'])
        self.assertEqual(workflow['concurrency']['group'], 'gpoint-production-release')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        owner = workflow['jobs']['validate']
        self.assertEqual(owner['runs-on'], 'ubuntu-latest')
        self.assertIn('[ "$RECOVERY_APPROVED" = "true" ]',
                      owner['steps'][0]['run'])
        self.assertIn('[ "$GITHUB_ACTOR" = "akhripunov" ]',
                      owner['steps'][0]['run'])
        self.assertIn('[ "$GITHUB_TRIGGERING_ACTOR" = "akhripunov" ]',
                      owner['steps'][0]['run'])
        job = workflow['jobs']['recover']
        self.assertEqual(job['needs'], 'validate')
        self.assertEqual(job['runs-on'], ['self-hosted', 'macOS', 'gpoint-production'])
        self.assertEqual(job['permissions'], {
            'contents': 'read', 'actions': 'read', 'packages': 'write', 'deployments': 'write'})
        self.assertEqual(job['env']['RELEASE_MODE'], 'interrupted-recovery')
        steps = job['steps']
        names = [step.get('name') for step in steps]
        self.assertEqual(steps[1]['with']['ref'], '${{ github.sha }}')
        self.assertEqual(steps[names.index('Verify authorized Mac release runner')]['env']['CANDIDATE_SHA'],
                         '${{ github.sha }}')
        self.assertLess(names.index('Retrieve immutable original Prepare receipt and verify GitHub logs'),
                        names.index('Independently attest installed Docker images, healthy services and migrations'))
        self.assertLess(names.index('Independently attest installed Docker images, healthy services and migrations'),
                        names.index('Publish independently verified recovery evidence'))
        self.assertLess(names.index('Publish independently verified recovery evidence'),
                        names.index('Verify published recovery artifact and finalize same Deployment'))
        self.assertIn('RECOVERY_EVIDENCE_DIGEST=$digest',
                      steps[names.index('Publish independently verified recovery evidence')]['run'])
        self.assertNotIn('scripts/automated-release.mjs', WORKFLOW.read_text())
        self.assertNotIn('pnpm release:gpoint', WORKFLOW.read_text())
        self.assertNotIn('docker compose up', WORKFLOW.read_text())
        self.assertNotIn('deploy:gpoint', WORKFLOW.read_text())

    def test_prepare_runs_recovery_contract_regressions(self):
        self.assertIn('scripts/recover-interrupted-apply.test.mjs', (ROOT / 'package.json').read_text())
        self.assertIn('python3 -m unittest tests.test_interrupted_recovery_workflow',
                      (ROOT / '.github/workflows/gpoint-prepare.yml').read_text())

    def test_successful_recovery_triggers_canonical_linear_reconciliation(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-linear-reconcile.yml').read_text())
        events = workflow.get('on', workflow.get(True))
        self.assertIn('GPoint — Interrupted Apply recovery', events['workflow_run']['workflows'])
        self.assertEqual(events['workflow_run']['types'], ['completed'])

    def test_recovery_executor_is_distinct_from_prepared_candidate_and_stays_on_main(self):
        from scripts.prepared_artifact_cache import validate_current_main_candidate as validate
        candidate, executor = 'a' * 40, 'b' * 40
        is_ancestor = lambda a, b: a == candidate and b == executor
        validate(candidate, 'interrupted-recovery', executor, executor, is_ancestor)
        with self.assertRaises(RuntimeError):
            validate(candidate, 'production', executor, executor, is_ancestor)
        with self.assertRaises(RuntimeError):
            validate(candidate, 'interrupted-recovery', executor, candidate, is_ancestor)
        with self.assertRaises(RuntimeError):
            validate(candidate, 'interrupted-recovery', executor, executor, lambda *_: False)
        with self.assertRaises(RuntimeError):
            validate(candidate, 'interrupted-recovery', candidate, candidate, lambda *_: True)


if __name__ == '__main__':
    unittest.main()
