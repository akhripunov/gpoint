import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_assets as ASSETS  # noqa: E402


class ImmutableReleaseTests(unittest.TestCase):
    def test_privileged_prepare_is_main_push_only_and_branch_jobs_are_not_dispatchable(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        trigger = workflow.split('\npermissions:', 1)[0]
        self.assertIn('push:', trigger)
        self.assertNotIn('workflow_dispatch:', trigger)
        self.assertIn('permissions:\n  contents: read\n  actions: read', workflow)
        prepare = workflow.split('  prepare:\n', 1)[1].split('    steps:\n', 1)[0]
        self.assertIn("github.ref == 'refs/heads/main'", prepare)
        self.assertNotIn("github.ref != 'refs/heads/main'", prepare)
        self.assertIn('permissions:\n      contents: write\n      actions: read\n      packages: write', prepare)

    def test_privileged_prepare_checkout_does_not_persist_write_credentials(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        prepare_job = workflow.split('  prepare:\n', 1)[1].split('\n  publish:\n', 1)[0]
        checkout = prepare_job.split('- uses: actions/checkout@v4', 1)[1].split('\n      - ', 1)[0]
        self.assertIn('persist-credentials: false', checkout)

    def test_privileged_deploy_checkout_does_not_persist_write_credentials(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        deploy_job = workflow.split('  deploy:\n', 1)[1].split('\n  publish:\n', 1)[0]
        checkout = deploy_job.split('- uses: actions/checkout@v4', 1)[1].split('\n      - ', 1)[0]
        self.assertIn('persist-credentials: false', checkout)

    def fixture(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('prepare', sha, '55', '2')
        release = {'tag_name': tag, 'draft': False, 'immutable': True,
                   'assets': [{'id': 99, 'name': 'bundle.zip', 'state': 'uploaded',
                               'size': 123, 'digest': 'sha256:' + 'b' * 64}]}
        ref = {'object': {'type': 'commit', 'sha': sha}}
        def github(route):
            return ref if route.startswith('git/ref/') else release
        return sha, release, ref, github

    def test_exact_immutable_release_selects_digest_and_asset(self):
        sha, _, _, github = self.fixture()
        self.assertEqual(ASSETS.resolve_asset('prepare', sha, '55', '2', 'bundle.zip', github)['id'], 99)

    def test_oci_prepare_download_bundle_is_receipt_only_and_archive_is_separate_fallback(self):
        import json
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'receipt.json').write_text(json.dumps({'schema': 3, 'transport': 'ghcr-oci'}))
            (source / 'images.tar.gz').write_bytes(b'fallback image archive')
            primary, fallback = ASSETS.build_oci_prepare_assets(source, root)
            with zipfile.ZipFile(primary) as archive:
                self.assertEqual(archive.namelist(), ['receipt.json'])
            with zipfile.ZipFile(fallback) as archive:
                self.assertEqual(set(archive.namelist()), {'receipt.json', 'images.tar.gz'})

    def test_draft_mutable_tag_mismatch_and_moved_tag_are_rejected(self):
        for field, value in [('draft', True), ('immutable', False), ('tag_name', 'wrong')]:
            sha, release, _, github = self.fixture()
            release[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                ASSETS.resolve_asset('prepare', sha, '55', '2', 'bundle.zip', github)
        sha, _, ref, github = self.fixture()
        ref['object']['sha'] = 'c' * 40
        with self.assertRaises(RuntimeError):
            ASSETS.resolve_asset('prepare', sha, '55', '2', 'bundle.zip', github)

    def test_missing_duplicate_unuploaded_and_digestless_asset_fail_closed(self):
        for change in ('missing', 'duplicate', 'pending', 'digest', 'identity'):
            sha, release, _, github = self.fixture()
            if change == 'missing':
                release['assets'] = []
            if change == 'duplicate':
                release['assets'] *= 2
            if change == 'pending':
                release['assets'][0]['state'] = 'new'
            if change == 'digest':
                release['assets'][0]['digest'] = ''
            if change == 'identity':
                release['assets'][0]['id'] = 'bad'
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                ASSETS.resolve_asset('prepare', sha, '55', '2', 'bundle.zip', github)

    def test_attempt_kind_and_sha_have_distinct_validated_names(self):
        self.assertNotEqual(ASSETS.release_tag('prepare', 'a'*40, '55', '1'),
                            ASSETS.release_tag('prepare', 'a'*40, '55', '2'))
        for args in [('prepare', 'main', '55', '1'), ('prepare', 'a'*40, '../55', '1'),
                     ('other', 'a'*40, '55', '1')]:
            with self.assertRaises(RuntimeError):
                ASSETS.release_tag(*args)

    def test_read_result_passes_immutable_asset_size_to_range_downloader(self):
        import prepared_artifact_cache as cache
        asset = {'id': 72, 'size': 1234, 'digest': 'sha256:' + 'b' * 64}
        with patch.object(ASSETS, 'find_deploy_evidence', return_value=('2', asset)), \
             patch.object(cache, 'download', side_effect=RuntimeError('stop after downloader call')) as download:
            with self.assertRaisesRegex(RuntimeError, 'stop after downloader call'):
                ASSETS.read_result('a' * 40, '55', '2', result_path='/tmp/result.json', token='unused')
        self.assertEqual(download.call_args.kwargs['expected_size'], 1234)

    def test_stale_deploy_result_from_previous_attempt_cannot_be_republished(self):
        data = {'candidateSha': 'a'*40, 'automationRunId': '55', 'automationRunAttempt': '1'}
        ASSETS.validate_result(data, 'a'*40, '55', '1')
        for sha, run_id, attempt in [('a'*40, '55', '2'), ('b'*40, '55', '1'), ('a'*40, '56', '1')]:
            with self.assertRaises(RuntimeError):
                ASSETS.validate_result(data, sha, run_id, attempt)

    def test_publish_rerun_uses_actual_successful_deploy_attempt(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('evidence', sha, '55', '1')
        release = {'tag_name': tag, 'draft': False, 'immutable': True,
                   'assets': [{'id': 99, 'name': 'result.zip', 'state': 'uploaded',
                               'size': 123, 'digest': 'sha256:' + 'b' * 64}]}
        def api(route):
            if route.endswith('-2') and route.startswith('releases/tags/'):
                return None
            if route == 'actions/runs/55/attempts/2/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'publish', 'conclusion': 'failure'}]}
            if route == 'releases/tags/' + tag:
                return release
            if route == 'git/ref/tags/' + tag:
                return {'object': {'type': 'commit', 'sha': sha}}
            if route == 'actions/runs/55/attempts/1/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                                                    'conclusion': 'success', 'head_sha': sha}]}
            raise AssertionError(route)
        attempt, asset = ASSETS.find_deploy_evidence(sha, '55', '2', api)
        self.assertEqual(attempt, '1')
        self.assertEqual(asset['id'], 99)

    def test_failed_newer_deploy_must_not_borrow_older_evidence(self):
        sha = 'a' * 40
        def api(route):
            if route.endswith('-2') and route.startswith('releases/tags/'):
                return None
            if route == 'actions/runs/55/attempts/2/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'conclusion': 'failure'}]}
            raise AssertionError('Must fail before inspecting previous evidence: ' + route)
        with self.assertRaisesRegex(RuntimeError, 'Newer Deploy attempt'):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)

    def test_successful_or_running_newer_deploy_missing_evidence_fails_closed(self):
        sha = 'a' * 40
        for status, conclusion in [('completed', 'success'), ('in_progress', None),
                                   ('queued', None), ('completed', 'cancelled')]:
            def api(route):
                if route.startswith('releases/tags/'):
                    return None
                if route == 'actions/runs/55/attempts/2/jobs?per_page=100':
                    return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': status,
                                                        'conclusion': conclusion, 'head_sha': sha,
                                                        'started_at': '2026-10-14T03:58:00Z',
                                                        'completed_at': '2026-10-14T04:00:00Z'}]}
                if route == 'actions/runs/55/attempts/2':
                    return {'run_attempt': 2, 'run_started_at': '2026-10-14T03:57:53Z'}
                raise AssertionError('Must not read old evidence: ' + route)
            with self.subTest(status=status, conclusion=conclusion), self.assertRaisesRegex(
                    RuntimeError, 'Newer Deploy attempt'):
                ASSETS.find_deploy_evidence(sha, '55', '2', api)

    def test_carried_over_successful_job_does_not_require_new_evidence(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('evidence', sha, '55', '1')
        release = {'tag_name': tag, 'draft': False, 'immutable': True,
                   'assets': [{'id': 9, 'name': 'result.zip', 'state': 'uploaded',
                               'size': 100, 'digest': 'sha256:' + 'b' * 64}]}
        def api(route):
            if route == 'releases/tags/' + tag:
                return release
            if route.startswith('releases/tags/'):
                return None
            if route == 'git/ref/tags/' + tag:
                return {'object': {'type': 'commit', 'sha': sha}}
            if route == 'actions/runs/55/attempts/2/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha,
                    'started_at': '2026-10-14T03:51:08Z',
                    'completed_at': '2026-10-14T03:56:39Z'}]}
            if route == 'actions/runs/55/attempts/2':
                return {'run_attempt': 2, 'run_started_at': '2026-10-14T03:57:53Z'}
            if route == 'actions/runs/55/attempts/1/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha}]}
            raise AssertionError(route)
        attempt, asset = ASSETS.find_deploy_evidence(sha, '55', '2', api)
        self.assertEqual((attempt, asset['id']), ('1', 9))

    def test_reexecuted_deploy_missing_evidence_never_uses_older_artifact(self):
        sha = 'a' * 40
        def api(route):
            if route.startswith('releases/tags/'):
                return None
            if route == 'actions/runs/55/attempts/2/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha,
                    'started_at': '2026-10-14T03:58:00Z',
                    'completed_at': '2026-10-14T04:00:00Z'}]}
            if route == 'actions/runs/55/attempts/2':
                return {'run_attempt': 2, 'run_started_at': '2026-10-14T03:57:53Z'}
            raise AssertionError(route)
        with self.assertRaisesRegex(RuntimeError, 'Newer Deploy attempt'):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)

    def test_missing_timing_identity_fails_closed(self):
        sha = 'a' * 40
        def api(route):
            if route.startswith('releases/tags/'):
                return None
            if route.endswith('/jobs?per_page=100'):
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha}]}
            if route == 'actions/runs/55/attempts/2':
                return {'run_attempt': 2, 'run_started_at': '2026-10-14T03:57:53Z'}
            raise AssertionError(route)
        with self.assertRaisesRegex(RuntimeError, 'timing evidence invalid'):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)

    def test_prepare_publish_only_rerun_uses_actual_build_attempt(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('prepare', sha, '55', '1')
        release = {'tag_name': tag, 'draft': False, 'immutable': True,
                   'assets': [{'id': 87, 'name': 'bundle.zip', 'state': 'uploaded',
                               'size': 444, 'digest': 'sha256:' + 'c' * 64}]}
        def api(route):
            if route == 'releases/tags/' + tag:
                return release
            if route.startswith('releases/tags/'):
                return None
            if route == 'git/ref/tags/' + tag:
                return {'object': {'type': 'commit', 'sha': sha}}
            if route == 'actions/runs/55/attempts/2':
                return {'run_attempt': 2, 'run_started_at': '2026-10-14T04:51:39Z'}
            if route == 'actions/runs/55/attempts/1':
                return {'run_attempt': 1, 'run_started_at': '2026-10-14T04:38:31Z'}
            if route.endswith('/jobs?per_page=100'):
                return {'total_count': 1, 'jobs': [{'name': 'prepare', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha, 'started_at': '2026-10-14T04:38:34Z',
                    'completed_at': '2026-10-14T04:46:48Z'}]}
            raise AssertionError(route)
        attempt, asset = ASSETS.find_prepare_evidence(sha, '55', '2', api)
        self.assertEqual((attempt, asset['id']), ('1', 87))

    def test_prepare_new_execute_without_bundle_rejected(self):
        sha = 'a' * 40
        def api(route):
            if route.startswith('releases/tags/'):
                return None
            if route == 'actions/runs/55/attempts/2':
                return {'run_attempt': 2, 'run_started_at': '2026-10-14T04:51:39Z'}
            if route.endswith('/jobs?per_page=100'):
                return {'total_count': 1, 'jobs': [{'name': 'prepare', 'status': 'completed',
                    'conclusion': 'success', 'head_sha': sha, 'started_at': '2026-10-14T04:52:00Z',
                    'completed_at': '2026-10-14T04:55:00Z'}]}
            raise AssertionError(route)
        with self.assertRaisesRegex(RuntimeError, 'no immutable bundle'):
            ASSETS.find_prepare_evidence(sha, '55', '2', api)

    def test_immutable_evidence_mismatch_fails_closed(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('evidence', sha, '55', '1')
        base = {'tag_name': tag, 'draft': False, 'immutable': True,
                'assets': [{'id': 99, 'name': 'result.zip', 'state': 'uploaded',
                            'size': 123, 'digest': 'sha256:' + 'b' * 64}]}
        def api(route):
            if route == 'releases/tags/' + tag:
                return base
            if route.startswith('releases/tags/'):
                return None
            if route.startswith('git/ref/'):
                return {'object': {'type': 'commit', 'sha': sha}}
            if route.endswith('/jobs?per_page=100'):
                return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                                                    'conclusion': 'failure', 'head_sha': sha}]}
            raise AssertionError(route)
        with self.assertRaises(RuntimeError):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)
        base['immutable'] = False
        with self.assertRaises(RuntimeError):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)
        base['immutable'] = True
        base['assets'] = []
        with self.assertRaises(RuntimeError):
            ASSETS.find_deploy_evidence(sha, '55', '2', api)

    def test_missing_evidence_and_unexpected_api_error_fail_closed(self):
        sha = 'a' * 40
        def missing(route):
            return {'total_count': 0, 'jobs': []} if route.endswith('/jobs?per_page=100') else None
        with self.assertRaisesRegex(RuntimeError, 'No immutable evidence'):
            ASSETS.find_deploy_evidence(sha, '55', '3', missing)
        def api_error(route):
            raise ConnectionError('read failed')
        with self.assertRaises(ConnectionError):
            ASSETS.find_deploy_evidence(sha, '55', '2', api_error)

    def test_ci_never_requires_repository_administration_permission(self):
        source = (ROOT / 'scripts/release_assets.py').read_text()
        self.assertNotIn("github('immutable-releases')", source)
        self.assertIn('resolve_asset(kind, sha, run_id, attempt, archive.name, github)', source)

    def test_gpoint_workflows_publish_all_evidence_without_actions_storage(self):
        prepare = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        deploy = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        for workflow in (prepare, deploy):
            self.assertNotIn('actions/upload-artifact', workflow)
            self.assertNotIn('actions/download-artifact', workflow)
        self.assertIn('release_assets.py publish prepare', prepare)
        self.assertIn('release_assets.py publish evidence', deploy)
        self.assertIn('release_assets.py read-result', deploy)

if __name__ == '__main__':
    unittest.main()
