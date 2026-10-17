import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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


class RecoveryDiagnosticsSelectionTests(unittest.TestCase):
    def test_read_only_diagnostics_needs_no_production_approval(self):
        SELECTOR.validate_request(SELECTOR.RECOVERY_CANDIDATE_SHA, 'refs/heads/main',
                                  'recovery-diagnostics', 'false')
        with self.assertRaisesRegex(RuntimeError, 'cannot accept production approval'):
            SELECTOR.validate_request(SELECTOR.RECOVERY_CANDIDATE_SHA, 'refs/heads/main',
                                      'recovery-diagnostics', 'true')

    def test_diagnostics_requires_exact_incident_and_current_main_executor(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(SELECTOR.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            SELECTOR.validate_recovery_executor(SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40, 'e' * 40, root)
        with self.assertRaisesRegex(RuntimeError, 'pinned'):
            SELECTOR.validate_recovery_executor('f' * 40, 'e' * 40, 'e' * 40, root)
        with self.assertRaisesRegex(RuntimeError, 'current main'):
            SELECTOR.validate_recovery_executor(SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40, 'f' * 40, root)

class RecoveryFinalizationAuthorizationTests(unittest.TestCase):
    def test_mode_requires_exact_candidate_approval(self):
        SELECTOR.validate_request(SHA, 'refs/heads/main', 'recovery-finalization', 'true')
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            SELECTOR.validate_request(SHA, 'refs/heads/main', 'recovery-finalization', 'false')

    def test_only_original_failure_and_explicit_baseline_risk_decision_are_accepted(self):
        evidence = {'RECOVERY_FAILURE_RUN_ID': '39010750184', 'RECOVERY_FAILURE_RUN_ATTEMPT': '1',
            'RECOVERY_DB_RISK_ACCEPTED': 'true', 'RECOVERY_DB_RISK_BASELINE_SHA': 'a' * 40,
            'RECOVERY_DB_RISK_DECISION_ID': 'ME38_DB_RISK_01', 'CREATE_RECOVERY_BACKUP': 'true',
            'GITHUB_ACTOR': 'akhripunov'}
        self.assertEqual(SELECTOR.validate_recovery_finalization_inputs(SHA, evidence)['baseline_sha'], 'a' * 40)
        for key, value in [('RECOVERY_FAILURE_RUN_ID', '1'), ('RECOVERY_FAILURE_RUN_ATTEMPT', '2'),
                           ('RECOVERY_DB_RISK_ACCEPTED', 'false'), ('RECOVERY_DB_RISK_BASELINE_SHA', 'bad'),
                           ('RECOVERY_DB_RISK_DECISION_ID', ''), ('CREATE_RECOVERY_BACKUP', 'false'),
                           ('GITHUB_ACTOR', 'other-user')]:
            with self.subTest(key=key):
                invalid = dict(evidence); invalid[key] = value
                with self.assertRaisesRegex(RuntimeError, 'exact incident'):
                    SELECTOR.validate_recovery_finalization_inputs(SHA, invalid)

    def test_recovery_executor_must_be_current_main_and_contain_exact_incident_candidate(self):
        root = Path(__file__).resolve().parents[1]
        with patch.object(SELECTOR.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            SELECTOR.validate_recovery_executor(SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40, 'e' * 40, root)
            self.assertEqual(run.call_args.args[0], ['git', 'merge-base', '--is-ancestor',
                SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40])
        with self.assertRaisesRegex(RuntimeError, 'pinned'):
            SELECTOR.validate_recovery_executor('f' * 40, 'e' * 40, 'e' * 40, root)
        with self.assertRaisesRegex(RuntimeError, 'current main'):
            SELECTOR.validate_recovery_executor(SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40, 'f' * 40, root)
        with patch.object(SELECTOR.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, 'ancestor'):
                SELECTOR.validate_recovery_executor(SELECTOR.RECOVERY_CANDIDATE_SHA, 'e' * 40, 'e' * 40, root)

    def test_original_prepare_identity_is_read_from_verified_immutable_failure_asset(self):
        def fake_run(command, **kwargs):
            output = Path(command[-1])
            output.write_text(__import__('json').dumps({'prepare': {'runId': '39001890156', 'attempt': '1',
                'candidateSha': SELECTOR.RECOVERY_CANDIDATE_SHA}}))
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(SELECTOR.subprocess, 'run', side_effect=fake_run) as run:
            self.assertEqual(SELECTOR.original_prepare_identity(SELECTOR.RECOVERY_CANDIDATE_SHA,
                Path('/repo'), {'GITHUB_TOKEN': 'not-read-by-test'}), ('39001890156', '1'))
        self.assertEqual(run.call_args.args[0][1:5], ['scripts/release_assets.py', 'read-recovery-failure',
            SELECTOR.RECOVERY_CANDIDATE_SHA, SELECTOR.RECOVERY_FAILURE_RUN_ID])

    def test_original_prepare_identity_rejects_unbound_or_missing_prepare(self):
        for prepare in [{'runId': '1', 'attempt': '1', 'candidateSha': 'f' * 40}, None]:
            def fake_run(command, prepare=prepare, **kwargs):
                Path(command[-1]).write_text(__import__('json').dumps({'prepare': prepare}))
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(SELECTOR.subprocess, 'run', side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, 'exact Prepare'):
                    SELECTOR.original_prepare_identity(SELECTOR.RECOVERY_CANDIDATE_SHA,
                        Path('/repo'), {})


if __name__ == '__main__':
    unittest.main()
