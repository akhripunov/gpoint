import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('select-release-run.py')
SPEC = importlib.util.spec_from_file_location('select_release_run', SCRIPT)
SELECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECTOR)

SHA = 'f34a73831c35d0d872ef5704e2167e24f5f96875'
PREPARE_ID = '37578150797'
PREFLIGHT_ID = '37579918089'


def prepare_run():
    return {'id': int(PREPARE_ID), 'run_attempt': 2}


def preflight_run(sha=SHA):
    return {
        'id': int(PREFLIGHT_ID),
        'run_attempt': 1,
        'run_number': int(PREFLIGHT_ID),
        'head_sha': sha,
        'head_branch': 'main',
        'event': 'workflow_dispatch',
        'display_title': f'GPoint preflight {sha} by github-actions[bot]',
        'status': 'completed',
        'conclusion': 'success',
    }


def canonical_preflight_receipt(**overrides):
    # Shape emitted by automated-release.mjs after a successful canonical
    # dry-run. It is immutable evidence from the published release result.
    receipt = {
        'candidateSha': SHA,
        'automationRunId': PREFLIGHT_ID,
        'automationRunAttempt': '1',
        'mode': 'preflight',
        'preflight': 'PASS',
        'releaseState': 'READY_FOR_OWNER',
        'status': 'PLANNED',
        'productionChanged': False,
        'dryRun': True,
        'applicationVerification': 'NOT_DEPLOYED',
        'authorizationMode': 'EXACT_SHA',
        'authorizationConsumed': False,
        'prepareRunId': PREPARE_ID,
        'prepareRunAttempt': '2',
    }
    receipt.update(overrides)
    return receipt


class ReadyPreflightEvidenceTests(unittest.TestCase):
    def validate(self, receipt, runs=None, sha=SHA):
        return SELECTOR.validate_ready_preflight(
            sha,
            prepare_run(),
            [preflight_run()] if runs is None else runs,
            lambda *_: receipt,
        )

    def test_successful_canonical_ready_receipt_is_accepted_by_production_validator(self):
        selected = self.validate(canonical_preflight_receipt())
        self.assertEqual(selected['id'], int(PREFLIGHT_ID))

    def test_failed_preflight_receipt_is_rejected(self):
        receipt = canonical_preflight_receipt(
            status='FAILED_PRE_SWITCH', releaseState='FAILED_PRE_SWITCH')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_blocked_preflight_receipt_is_rejected(self):
        receipt = canonical_preflight_receipt(status='BLOCKED', releaseState='BLOCKED')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_receipt_without_successful_preflight_evidence_is_rejected(self):
        receipt = canonical_preflight_receipt()
        receipt.pop('preflight')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_non_pass_preflight_evidence_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(preflight='FAILED'))

    def test_incomplete_receipt_is_rejected(self):
        receipt = canonical_preflight_receipt()
        receipt.pop('automationRunAttempt')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_receipt_for_another_sha_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(candidateSha='0' * 40))

    def test_stale_preflight_run_is_not_selected_for_current_candidate(self):
        with self.assertRaisesRegex(RuntimeError, 'successful READY_FOR_OWNER preflight'):
            self.validate(canonical_preflight_receipt(), runs=[preflight_run('0' * 40)])

    def test_stale_candidate_is_rejected_when_main_has_advanced(self):
        with self.assertRaisesRegex(RuntimeError, 'no longer origin/main'):
            SELECTOR.validate_current_main(SHA, '0' * 40)

    def test_missing_production_unchanged_safety_evidence_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(productionChanged=True))

    def test_missing_dry_run_evidence_is_rejected(self):
        receipt = canonical_preflight_receipt()
        receipt.pop('dryRun')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_preflight_receipt_cannot_claim_application_was_deployed(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(applicationVerification='DEPLOYED'))

    def test_missing_exact_sha_authorization_evidence_is_rejected(self):
        receipt = canonical_preflight_receipt()
        receipt.pop('authorizationMode')
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(receipt)

    def test_consumed_authorization_evidence_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(authorizationConsumed=True))

    def test_prepare_run_identity_mismatch_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            self.validate(canonical_preflight_receipt(prepareRunAttempt='1'))


if __name__ == '__main__':
    unittest.main()
