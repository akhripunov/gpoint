import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('select_release_run', Path(__file__).with_name('select-release-run.py'))
SELECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECTOR)
SHA = 'a' * 40


class PrepareSelectorTests(unittest.TestCase):
    def test_selects_newest_successful_prepare_for_exact_main_sha(self):
        runs = [
            {'id': 1, 'run_attempt': 1, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push', 'path': '.github/workflows/gpoint-prepare.yml', 'status': 'completed', 'conclusion': 'success'},
            {'id': 2, 'run_attempt': 1, 'head_sha': SHA, 'head_branch': 'feature', 'event': 'push', 'status': 'completed', 'conclusion': 'success'},
        ]
        selected = SELECTOR.select_run(SHA, runs)
        self.assertEqual(selected['id'], 1)
        self.assertEqual(selected['run_attempt'], 1)

    def test_newer_failed_prepare_blocks_older_success(self):
        runs = [
            {'id': 1, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push', 'status': 'completed', 'conclusion': 'success'},
            {'id': 2, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push', 'status': 'completed', 'conclusion': 'failure'},
        ]
        with self.assertRaisesRegex(RuntimeError, 'not successful'):
            SELECTOR.select_run(SHA, runs)

    def test_foreign_sha_or_event_cannot_supply_prepare(self):
        runs = [{'id': 1, 'head_sha': 'b' * 40, 'head_branch': 'main', 'event': 'schedule', 'status': 'completed', 'conclusion': 'success'}]
        with self.assertRaisesRegex(RuntimeError, 'No preparation'):
            SELECTOR.select_run(SHA, runs)

    def test_unexpected_prepare_workflow_is_rejected(self):
        runs = [{'id': 1, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push', 'path': '.github/workflows/other.yml', 'status': 'completed', 'conclusion': 'success'}]
        with self.assertRaisesRegex(RuntimeError, 'workflow identity'):
            SELECTOR.select_run(SHA, runs)

    def test_request_requires_exact_main_and_authorized_mode(self):
        SELECTOR.validate_request(SHA, 'refs/heads/main', 'preflight', 'false')
        SELECTOR.validate_request(SHA, 'refs/heads/main', 'production', 'true')
        for args in [('bad', 'refs/heads/main', 'preflight', 'false'),
                     (SHA, 'refs/heads/feature', 'preflight', 'false'),
                     (SHA, 'refs/heads/main', 'production', 'false')]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                SELECTOR.validate_request(*args)

    def test_expected_prepare_run_and_attempt_are_bound_together(self):
        selected = {'id': 42, 'run_attempt': 3}
        SELECTOR.validate_expected_run(selected, '42', '3')
        for run_id, attempt in [('42', ''), ('42', '2'), ('', '3'), ('bad', '3')]:
            with self.subTest(run_id=run_id, attempt=attempt), self.assertRaises(RuntimeError):
                SELECTOR.validate_expected_run(selected, run_id, attempt)

    def test_candidate_must_still_be_current_main(self):
        SELECTOR.validate_current_main(SHA, SHA)
        with self.assertRaisesRegex(RuntimeError, 'no longer origin/main'):
            SELECTOR.validate_current_main(SHA, 'b' * 40)


if __name__ == '__main__':
    unittest.main()
