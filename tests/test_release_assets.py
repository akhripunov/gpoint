import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import socket
import ssl
import urllib.error
import hashlib
import json
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_assets as ASSETS  # noqa: E402


class GitHubEvidenceApiRetryTests(unittest.TestCase):
    class Response:
        def __init__(self, body=b'{"ok":true}'):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    def test_temporary_tls_handshake_timeout_recovers_with_bounded_backoff(self):
        calls = []
        delays = []

        def open_url(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise urllib.error.URLError(socket.timeout('TLS handshake timed out'))
            return self.Response()

        with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
             patch('urllib.request.urlopen', side_effect=open_url), \
             patch('time.sleep', side_effect=delays.append), \
             patch('random.uniform', side_effect=lambda _low, high: min(high, 0.25)), \
             patch('builtins.print') as emit:
            result = ASSETS.evidence_api('git/ref/tags/example')

        self.assertEqual(result, {'ok': True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(delays), 1)
        self.assertGreaterEqual(delays[0], 0)
        self.assertLessEqual(delays[0], 5)
        self.assertNotIn('test-token', str(emit.call_args_list))

    def test_persistent_tls_timeout_stops_within_shared_retry_budget(self):
        now = [0.0]
        calls = []
        delays = []

        def open_url(_request, timeout):
            calls.append(timeout)
            now[0] += timeout
            raise urllib.error.URLError(socket.timeout('TLS handshake timed out'))

        def sleep(delay):
            delays.append(delay)
            now[0] += delay

        with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
             patch('urllib.request.urlopen', side_effect=open_url), \
             patch('time.monotonic', side_effect=lambda: now[0]), \
             patch('time.sleep', side_effect=sleep), \
             patch('random.uniform', side_effect=lambda _low, high: high), \
             patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'retry budget exhausted'):
                ASSETS.evidence_api('git/ref/tags/example')

        self.assertGreater(len(calls), 1)
        self.assertLessEqual(now[0], 60.001)
        self.assertTrue(all(0 <= delay <= 5 for delay in delays))
        self.assertTrue(all(0 < timeout <= 20 for timeout in calls))

    def test_authorization_failures_are_not_retried(self):
        for status in (401, 403):
            calls = []

            def open_url(_request, **_kwargs):
                calls.append(status)
                raise urllib.error.HTTPError(
                    'https://api.github.com/', status, 'authorization failure', None, None)

            with self.subTest(status=status), \
                 patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
                 patch('urllib.request.urlopen', side_effect=open_url), \
                 patch('time.sleep') as sleep:
                with self.assertRaises(urllib.error.HTTPError):
                    ASSETS.evidence_api('git/ref/tags/example')
                self.assertEqual(calls, [status])
                sleep.assert_not_called()

    def test_certificate_identity_failure_is_not_retried(self):
        calls = []

        def open_url(_request, **_kwargs):
            calls.append(True)
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))

        with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
             patch('urllib.request.urlopen', side_effect=open_url), \
             patch('time.sleep') as sleep:
            with self.assertRaises(urllib.error.URLError):
                ASSETS.evidence_api('git/ref/tags/example')
        self.assertEqual(calls, [True])
        sleep.assert_not_called()

    def test_real_not_found_is_not_retried_and_release_tag_404_stays_optional(self):
        for route, expected in (
            ('git/ref/tags/example', 'raise'),
            ('releases/tags/gpoint-prepare-' + 'a' * 40 + '-55-1', None),
        ):
            calls = []

            def open_url(_request, **_kwargs):
                calls.append(route)
                raise urllib.error.HTTPError(
                    'https://api.github.com/', 404, 'not found', None, None)

            with self.subTest(route=route), \
                 patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
                 patch('urllib.request.urlopen', side_effect=open_url), \
                 patch('time.sleep') as sleep:
                if expected == 'raise':
                    with self.assertRaises(urllib.error.HTTPError):
                        ASSETS.evidence_api(route)
                else:
                    self.assertIsNone(ASSETS.evidence_api(route))
                self.assertEqual(calls, [route])
                sleep.assert_not_called()

    def test_multiple_evidence_reads_share_one_operation_deadline(self):
        now = [0.0]
        calls = []

        def open_url(_request, timeout):
            calls.append(timeout)
            now[0] += 30
            return self.Response()

        with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}), \
             patch('urllib.request.urlopen', side_effect=open_url), \
             patch('time.monotonic', side_effect=lambda: now[0]), \
             patch('builtins.print'):
            api = ASSETS._api_for_operation(ASSETS.evidence_api)
            self.assertEqual(api('actions/first'), {'ok': True})
            self.assertEqual(api('actions/second'), {'ok': True})
            with self.assertRaisesRegex(RuntimeError, 'retry budget exhausted'):
                api('actions/third')

        self.assertEqual(calls, [20, 20])

    def test_sha_identity_mismatch_is_not_retried(self):
        sha = 'a' * 40
        tag = ASSETS.release_tag('prepare', sha, '55', '1')
        release = {'tag_name': tag, 'draft': False, 'immutable': True, 'assets': []}
        mismatch = {'object': {'type': 'commit', 'sha': 'b' * 40}}
        payloads = [json.dumps(release).encode(), json.dumps(mismatch).encode()]
        calls = []

        def open_url(_request, **_kwargs):
            calls.append(True)
            return self.Response(payloads.pop(0))

        client = ASSETS._EvidenceApiClient(
            'test-token', opener=open_url, sleep=lambda _delay: self.fail('identity mismatch must not retry'))
        with self.assertRaisesRegex(RuntimeError, 'exact candidate'):
            ASSETS.resolve_asset('prepare', sha, '55', '1', 'bundle.zip', client)
        self.assertEqual(len(calls), 2)


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

    def test_recovery_finalization_evidence_publisher_keeps_executor_and_candidate_independent(self):
        executor = 'e' * 40
        candidate = 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c'
        run_id, attempt = '39058724038', '1'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / 'result.json'
            evidence = root / 'evidence'
            evidence.mkdir()
            (evidence / 'recovery.json').write_text('{"eventType":"RECOVERY_FINALIZATION"}\n')
            result.write_text(json.dumps({'candidateSha': candidate, 'automationRunId': run_id,
                                          'automationRunAttempt': attempt}))
            env = {'GITHUB_REPOSITORY': ASSETS.REPO, 'GITHUB_SHA': executor,
                   'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_WORKFLOW_REF': f'{ASSETS.REPO}/.github/workflows/gpoint-deploy.yml@refs/heads/main',
                   'CANDIDATE_SHA': candidate, 'RELEASE_MODE': 'recovery-finalization',
                   'GITHUB_RUN_ID': run_id, 'GITHUB_RUN_ATTEMPT': attempt,
                   'RELEASE_RESULT': str(result), 'GPOINT_RELEASE_EVIDENCE': str(evidence)}
            commands = []
            with patch.dict('os.environ', env, clear=True), \
                 patch.object(ASSETS.subprocess, 'check_output', return_value=executor), \
                 patch.object(ASSETS, 'gh', side_effect=lambda *args: commands.append(args) or ''), \
                 patch.object(ASSETS, 'github', return_value={}), \
                 patch.object(ASSETS, 'resolve_asset', return_value={'digest': 'sha256:' + 'a' * 64}), \
                 patch('prepared_artifact_cache.checksum', return_value='a' * 64):
                ASSETS.publish('evidence', candidate, run_id, attempt)
            self.assertEqual(commands[0][0:3], ('api', '--method', 'POST'))
            self.assertIn(f'ref=refs/tags/{ASSETS.release_tag("evidence", candidate, run_id, attempt)}', commands[0])
            self.assertIn(f'sha={candidate}', commands[0])

            for changed in [
                {'GITHUB_SHA': candidate},
                {'GITHUB_WORKFLOW_REF': f'{ASSETS.REPO}/.github/workflows/other.yml@refs/heads/main'},
                {'GITHUB_EVENT_NAME': 'push'},
                {'CANDIDATE_SHA': executor},
            ]:
                invalid_env = {**env, **changed}
                commands.clear()
                with patch.dict('os.environ', invalid_env, clear=True), \
                     patch.object(ASSETS.subprocess, 'check_output', return_value=executor), \
                     patch.object(ASSETS, 'gh', side_effect=lambda *args: commands.append(args) or ''):
                    with self.assertRaisesRegex(RuntimeError, 'source or independent candidate identity'):
                        ASSETS.publish('evidence', candidate, run_id, attempt)
                self.assertEqual(commands, [])

    def test_ordinary_evidence_publisher_still_requires_exact_candidate_checkout(self):
        candidate = 'd' * 40
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': ASSETS.REPO,
                                      'GITHUB_SHA': 'e' * 40,
                                      'RELEASE_MODE': 'production'}, clear=True), \
             patch.object(ASSETS.subprocess, 'check_output', return_value='e' * 40), \
             patch.object(ASSETS, 'gh') as gh:
            with self.assertRaisesRegex(RuntimeError, 'Only exact GPoint workflow source'):
                ASSETS.publish('evidence', candidate, '55', '1')
        gh.assert_not_called()

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
        diagnostic_job = deploy.split('  recovery-diagnostics:\n', 1)[1].split('\n  publish-recovery-diagnostics:', 1)[0]
        deploy_without_diagnostics = deploy.replace('  recovery-diagnostics:\n' + diagnostic_job + '\n  publish:', '  publish:', 1)
        self.assertNotIn('actions/upload-artifact', prepare)
        self.assertNotIn('actions/download-artifact', prepare)
        self.assertNotIn('actions/upload-artifact', deploy_without_diagnostics)
        self.assertNotIn('actions/download-artifact', deploy_without_diagnostics)
        self.assertNotIn('actions/upload-artifact', diagnostic_job)
        self.assertNotIn('actions/download-artifact', diagnostic_job)
        self.assertIn('package-recovery-diagnostics', diagnostic_job)
        diagnostic_publisher = deploy.split('  publish-recovery-diagnostics:\n', 1)[1].split('\n  publish:', 1)[0]
        self.assertIn('contents: write', diagnostic_publisher)
        self.assertIn('publish-recovery-diagnostics', diagnostic_publisher)
        self.assertNotIn('GHCR_READ_TOKEN', diagnostic_publisher)
        self.assertIn('release_assets.py publish prepare', prepare)
        self.assertIn('release_assets.py publish evidence', deploy)
        self.assertIn('release_assets.py read-result', deploy)

    def test_recovery_diagnostic_tag_is_unique_and_separate_from_production_namespaces(self):
        executor = 'a' * 40
        one = ASSETS.recovery_diagnostics_tag(executor, '39043470160', '1')
        two = ASSETS.recovery_diagnostics_tag(executor, '39043470160', '2')
        self.assertEqual(one, f'gpoint-recovery-diagnostics-{executor}-39043470160-1')
        self.assertNotEqual(one, two)
        self.assertNotIn('gpoint-production-', one)
        for args in [(executor, '0', '1'), ('bad', '2', '1'), (executor, '2', '0')]:
            with self.assertRaises(RuntimeError):
                ASSETS.recovery_diagnostics_tag(*args)

    def test_recovery_diagnostic_bundle_binds_executor_incident_and_run(self):
        executor = 'a' * 40
        snapshot = self._diagnostic_snapshot()
        result = {'mode': 'recovery-diagnostics', 'status': 'FACTS_COMPLETE',
                  'candidateSha': snapshot['candidateSha'], 'automationRunId': '39043470160',
                  'automationRunAttempt': '1', 'recoverySnapshot': snapshot}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, result_path, manifest_path = root/'snapshot.json', root/'result.json', root/'manifest.json'
            snapshot_path.write_text(json.dumps(snapshot))
            result_path.write_text(json.dumps(result))
            bundle = ASSETS.build_recovery_diagnostics_bundle(snapshot_path, result_path, executor, '39043470160', '1')
            names = set(bundle)
            self.assertEqual(names, {'snapshot.json', 'result.json', 'diagnostic-manifest.json'})
            metadata = json.loads(bundle['diagnostic-manifest.json'])
            self.assertEqual(metadata['executorSha'], executor)
            self.assertEqual(metadata['candidateSha'], snapshot['candidateSha'])
            self.assertEqual(metadata['originalIncidentRunId'], '39010750184')
            with self.assertRaisesRegex(RuntimeError, 'executor identity'):
                ASSETS.build_recovery_diagnostics_bundle(snapshot_path, result_path, snapshot['candidateSha'], '39043470160', '1')
            for field, value in [('automationRunId', '39043470161'), ('automationRunAttempt', '2'),
                                 ('candidateSha', 'f'*40)]:
                altered = dict(result)
                altered[field] = value
                result_path.write_text(json.dumps(altered))
                with self.assertRaisesRegex(RuntimeError, 'does not match snapshot'):
                    ASSETS.build_recovery_diagnostics_bundle(snapshot_path, result_path, executor, '39043470160', '1')

    def test_recovery_diagnostic_package_exports_only_bounded_verified_payload(self):
        executor, run_id, attempt = 'a'*40, '39043470160', '1'
        snapshot = self._diagnostic_snapshot()
        result = {'mode': 'recovery-diagnostics', 'status': 'FACTS_COMPLETE',
                  'candidateSha': snapshot['candidateSha'], 'automationRunId': run_id,
                  'automationRunAttempt': attempt, 'recoverySnapshot': snapshot}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path, result_path = root/'snapshot.json', root/'result.json'
            local_manifest_path, output_path = root/'local-manifest.json', root/'github-output'
            snapshot_path.write_text(json.dumps(snapshot))
            result_path.write_text(json.dumps(result))
            local_manifest_path.write_text(json.dumps({'releaseId': ASSETS.recovery_diagnostics_tag(executor, run_id, attempt),
                'recoverySnapshot': snapshot, 'productionMutationOccurred': False,
                'databaseApplyOccurred': False, 'runtimeSwitchOccurred': False}))
            with patch.dict('os.environ', {'GPOINT_RECOVERY_EXECUTOR_SHA': executor, 'GITHUB_RUN_ID': run_id,
                'GITHUB_RUN_ATTEMPT': attempt, 'GPOINT_RECOVERY_SNAPSHOT_PATH': str(snapshot_path),
                'RELEASE_RESULT': str(result_path), 'GPOINT_RECOVERY_DIAGNOSTIC_MANIFEST': str(local_manifest_path),
                'GITHUB_OUTPUT': str(output_path)}, clear=True):
                ASSETS.package_recovery_diagnostics()
            line = output_path.read_text().strip()
            self.assertTrue(line.startswith('recovery_diagnostic_bundle='))
            payload = __import__('base64').b64decode(line.partition('=')[2], validate=True)
            self.assertLessEqual(len(payload), ASSETS.MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES)
            with zipfile.ZipFile(__import__('io').BytesIO(payload)) as archive:
                self.assertEqual(set(archive.namelist()), {'snapshot.json', 'result.json', 'diagnostic-manifest.json'})

    def test_recovery_diagnostic_publisher_fails_closed_on_existing_tag_and_missing_auth(self):
        executor = 'a' * 40
        bundle = self._diagnostic_bundle(executor)
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': ASSETS.REPO, 'GITHUB_SHA': executor,
                                       'GITHUB_TOKEN': 'test-token'}, clear=True), \
             patch.object(ASSETS.subprocess, 'check_output', return_value=executor):
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                ASSETS.publish_recovery_diagnostics(executor, '39043470160', '1', bundle,
                    api=lambda route: {'tag_name': ASSETS.recovery_diagnostics_tag(executor, '39043470160', '1')}
                    if route.startswith('releases/tags/') else {'object': {'type': 'commit', 'sha': executor}})
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': ASSETS.REPO, 'GITHUB_SHA': executor}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'authorization is unavailable'):
                ASSETS.publish_recovery_diagnostics(executor, '39043470160', '1', bundle)

    def test_recovery_diagnostic_release_is_draft_verified_then_published_and_read_back(self):
        executor, run_id, attempt = 'a'*40, '39043470160', '1'
        bundle = self._diagnostic_bundle(executor)
        files = ASSETS._read_diagnostic_bundle(bundle)
        tag = ASSETS.recovery_diagnostics_tag(executor, run_id, attempt)
        expected = {name: (hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items()}
        uploaded = [{'id': i, 'name': name, 'state': 'uploaded', 'size': size,
                     'digest': 'sha256:' + digest} for i, (name, (digest, size)) in enumerate(expected.items(), 1)]
        routes = []
        release_reads = 0
        tag_created = False
        def api(route):
            nonlocal release_reads
            routes.append(route)
            if route == f'releases/tags/{tag}':
                release_reads += 1
                if release_reads == 1:
                    return None
                return {'tag_name': tag, 'draft': release_reads == 2,
                        'immutable': release_reads >= 3, 'assets': uploaded}
            if route == f'git/ref/tags/{tag}':
                return {'object': {'type': 'commit', 'sha': executor}} if tag_created else None
            raise AssertionError(route)
        commands = []
        def command(*args):
            nonlocal tag_created
            commands.append(args)
            if args[:3] == ('api', '--method', 'POST'):
                tag_created = True
            return ''
        def download(asset, target, token):
            self.assertEqual(token, 'test-token')
            target.write_bytes(files[asset['name']])
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': ASSETS.REPO, 'GITHUB_SHA': executor,
                                       'GITHUB_TOKEN': 'test-token'}, clear=True), \
             patch.object(ASSETS.subprocess, 'check_output', return_value=executor):
            ASSETS.publish_recovery_diagnostics(executor, run_id, attempt, bundle, api, command, download)
        write_commands = [call for call in commands if call[:2] == ('api', '--method') or call[0] == 'release']
        self.assertEqual([call[0] for call in write_commands], ['api', 'release', 'release', 'release'])
        self.assertIn(f'ref=refs/tags/{tag}', write_commands[0])
        self.assertIn(f'sha={executor}', write_commands[0])
        self.assertEqual(write_commands[-1][2], tag)
        self.assertIn('--draft=false', write_commands[-1])
        self.assertGreaterEqual(release_reads, 3)

    def test_recovery_diagnostic_draft_digest_mismatch_blocks_publish(self):
        executor, run_id, attempt = 'a'*40, '39043470160', '1'
        bundle = self._diagnostic_bundle(executor)
        files = ASSETS._read_diagnostic_bundle(bundle)
        tag = ASSETS.recovery_diagnostics_tag(executor, run_id, attempt)
        assets = [{'id': i, 'name': name, 'state': 'uploaded', 'size': len(data),
                   'digest': 'sha256:' + ('0'*64 if name == 'snapshot.json' else hashlib.sha256(data).hexdigest())}
                  for i, (name, data) in enumerate(files.items(), 1)]
        reads, commands, tag_created = 0, [], False
        def api(route):
            nonlocal reads
            if route == f'releases/tags/{tag}':
                reads += 1
                return None if reads == 1 else {'tag_name': tag, 'draft': True, 'immutable': False, 'assets': assets}
            if route == f'git/ref/tags/{tag}':
                return {'object': {'type': 'commit', 'sha': executor}} if tag_created else None
            raise AssertionError(route)
        with patch.dict('os.environ', {'GITHUB_REPOSITORY': ASSETS.REPO, 'GITHUB_SHA': executor,
                                       'GITHUB_TOKEN': 'test-token'}, clear=True), \
             patch.object(ASSETS.subprocess, 'check_output', return_value=executor):
            def command(*args):
                nonlocal tag_created
                commands.append(args)
                if args[:3] == ('api', '--method', 'POST'):
                    tag_created = True
            with self.assertRaisesRegex(RuntimeError, 'checksum or identity mismatch'):
                ASSETS.publish_recovery_diagnostics(executor, run_id, attempt, bundle, api, command)
        self.assertFalse(any(call[0:2] == ('release', 'edit') for call in commands))

    @staticmethod
    def _diagnostic_snapshot():
        body = {'schema': 'gpoint-recovery-read-only-snapshot-v1', 'mode': 'recovery-diagnostics-read-only',
                'status': 'FACTS_COMPLETE', 'candidateSha': 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c',
                'releaseState': 'RECOVERY_DIAGNOSTICS_ONLY', 'factsComplete': True,
                'missingEvidence': [], 'blockers': ['FINALIZATION_REQUIRES_SEPARATE_OWNER_AUTHORIZATION'],
                'checks': {'incidentEvidence': 'PASS', 'ledger': 'PASS', 'runtime': 'PASS',
                    'database': 'PASS', 'workers': 'PASS', 'backup': 'PASS', 'locks': 'PASS',
                    'productionTag': 'PASS', 'mutationBoundary': 'PASS', 'finalization': 'BLOCKED'},
                'incident': {'runId': '39010750184', 'attempt': '1', 'releaseId': 'gpoint-release-20261017T064101334Z',
                             'baselineSha': 'a700198a843c12a84f2e537178de752e3eddd54d',
                             'candidateSha': 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c'},
                'backup': {'status': 'VERIFIED'},
                'finalizationReady': False, 'mutationPolicy': {
                    'allowed': ['read_only_observation', 'write_diagnostic_snapshot_artifact'],
                    'forbidden': ['apply', 'migration', 'rollback',
                    'runtime_switch', 'tag_write', 'ledger_write', 'config_write', 'restart']}}
        body['snapshotSha256'] = ASSETS._stable_json_digest(body)
        return body

    def test_recovery_diagnostic_publisher_rejects_inconsistent_snapshot_semantics(self):
        executor = 'a' * 40
        cases = []
        forged_complete = self._diagnostic_snapshot()
        forged_complete['missingEvidence'] = ['runtime.migrationHeads.gsales']
        forged_complete['snapshotSha256'] = ASSETS._stable_json_digest(
            {key: value for key, value in forged_complete.items() if key != 'snapshotSha256'})
        cases.append(forged_complete)
        mutating_allowlist = self._diagnostic_snapshot()
        mutating_allowlist['mutationPolicy']['allowed'].append('apply')
        mutating_allowlist['snapshotSha256'] = ASSETS._stable_json_digest(
            {key: value for key, value in mutating_allowlist.items() if key != 'snapshotSha256'})
        cases.append(mutating_allowlist)
        unknown_missing = self._diagnostic_snapshot()
        unknown_missing.update({'status': 'INCOMPLETE', 'factsComplete': False,
                                'missingEvidence': ['future.unmapped.field']})
        unknown_missing['snapshotSha256'] = ASSETS._stable_json_digest(
            {key: value for key, value in unknown_missing.items() if key != 'snapshotSha256'})
        cases.append(unknown_missing)
        unknown_blocker = self._diagnostic_snapshot()
        unknown_blocker['blockers'].append('UNRECOGNIZED_BLOCKER')
        unknown_blocker['snapshotSha256'] = ASSETS._stable_json_digest(
            {key: value for key, value in unknown_blocker.items() if key != 'snapshotSha256'})
        cases.append(unknown_blocker)
        for snapshot in cases:
            with self.subTest(snapshot=snapshot['mutationPolicy']['allowed']):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    result = {'mode': 'recovery-diagnostics', 'status': snapshot['status'],
                              'candidateSha': snapshot['candidateSha'], 'automationRunId': '39043470160',
                              'automationRunAttempt': '1', 'recoverySnapshot': snapshot}
                    snapshot_path, result_path = root/'snapshot.json', root/'result.json'
                    snapshot_path.write_text(json.dumps(snapshot))
                    result_path.write_text(json.dumps(result))
                    with self.assertRaisesRegex(RuntimeError, 'snapshot|read-only'):
                        ASSETS.build_recovery_diagnostics_bundle(snapshot_path, result_path,
                            executor, '39043470160', '1')

    def test_recovery_diagnostic_output_limit_has_utf16_quota_margin(self):
        self.assertEqual(ASSETS.MAX_RECOVERY_DIAGNOSTIC_OUTPUT_BYTES * 2,
                         ASSETS.MAX_RECOVERY_DIAGNOSTIC_OUTPUT_UTF16_BYTES)
        self.assertLess(ASSETS.MAX_RECOVERY_DIAGNOSTIC_OUTPUT_UTF16_BYTES, 1024 * 1024)
        oversized = 'A' * (ASSETS.MAX_RECOVERY_DIAGNOSTIC_OUTPUT_BYTES + 1)
        with self.assertRaisesRegex(RuntimeError, 'too large'):
            ASSETS._read_diagnostic_bundle(oversized)

    @classmethod
    def _diagnostic_bundle(cls, executor):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = cls._diagnostic_snapshot()
            result = {'mode': 'recovery-diagnostics', 'status': 'FACTS_COMPLETE',
                      'candidateSha': snapshot['candidateSha'], 'automationRunId': '39043470160',
                      'automationRunAttempt': '1', 'recoverySnapshot': snapshot}
            snapshot_path, result_path = root/'snapshot.json', root/'result.json'
            snapshot_path.write_text(json.dumps(snapshot))
            result_path.write_text(json.dumps(result))
            files = ASSETS.build_recovery_diagnostics_bundle(snapshot_path, result_path, executor, '39043470160', '1')
            payload = root/'input.zip'
            with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_STORED) as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            return __import__('base64').b64encode(payload.read_bytes()).decode()

    def test_failed_post_switch_recovery_evidence_requires_exact_workflow_jobs_and_immutable_assets(self):
        sha, run_id, attempt = 'a' * 40, '39010750184', '1'
        tag = ASSETS.release_tag('evidence', sha, run_id, attempt)
        assets = [{'id': i, 'name': name, 'state': 'uploaded', 'size': 500,
                   'digest': 'sha256:' + str(i) * 64} for i, name in enumerate(('result.zip', 'manifests.zip'), 1)]
        release = {'tag_name': tag, 'draft': False, 'immutable': True, 'assets': assets}
        def api(route):
            if route == f'actions/runs/{run_id}/attempts/{attempt}':
                return {'run_attempt': 1, 'head_sha': sha, 'event': 'workflow_dispatch',
                        'path': '.github/workflows/gpoint-deploy.yml', 'status': 'completed', 'conclusion': 'failure'}
            if route == f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100':
                return {'total_count': 2, 'jobs': [
                    {'name': 'validate', 'status': 'completed', 'conclusion': 'success', 'head_sha': sha},
                    {'name': 'deploy', 'status': 'completed', 'conclusion': 'failure', 'head_sha': sha}]}
            if route == f'releases/tags/{tag}':
                return release
            if route == f'git/ref/tags/{tag}':
                return {'object': {'type': 'commit', 'sha': sha}}
            raise AssertionError(route)
        selected = ASSETS.find_post_switch_failure_evidence(sha, run_id, attempt, api)
        self.assertEqual(selected['resultAsset']['id'], 1)
        self.assertEqual(selected['manifestAsset']['id'], 2)
        for bad_route, bad_value in [('actions/runs/39010750184/attempts/1', {'run_attempt': 2}),
                                     (f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100',
                                      {'total_count': 1, 'jobs': []})]:
            def bad_api(route, bad_route=bad_route, bad_value=bad_value):
                return bad_value if route == bad_route else api(route)
            with self.assertRaises(RuntimeError):
                ASSETS.find_post_switch_failure_evidence(sha, run_id, attempt, bad_api)

    def test_read_post_switch_failure_checks_asset_hashes_and_manifest_identity(self):
        sha, run_id, attempt = 'a' * 40, '39010750184', '1'
        result_name = f'gpoint-release-result-{run_id}-{attempt}.json'
        release_id = 'gpoint-release-20261017T064101334Z'
        result = {'candidateSha': sha, 'automationRunId': run_id, 'automationRunAttempt': attempt,
                  'status': 'RECOVERY_REQUIRED', 'releaseState': 'RECOVERY_REQUIRED', 'failureStage': 'POST_SWITCH',
                  'releaseId': release_id, 'previousProductionSha': 'b' * 40, 'productionBaselineSha': 'b' * 40,
                  'productionRef': None, 'prepareRunId': '39009939478', 'prepareRunAttempt': '1',
                  'failureReason': 'GitHub production tag push failed on a non-transient error; refusing retry'}
        manifest = {'candidateSha': sha, 'releaseId': release_id, 'status': 'RECOVERY_REQUIRED',
                    'failureStage': 'POST_SWITCH', 'previousProductionSha': 'b' * 40,
                    'productionShaAfter': None, 'productionRef': None, 'failureReason': result['failureReason']}
        def zip_bytes(files):
            import io
            buff = io.BytesIO()
            with zipfile.ZipFile(buff, 'w') as archive:
                for name, data in files.items():
                    archive.writestr(name, json.dumps(data) if isinstance(data, dict) else data)
            return buff.getvalue()
        receipt = {'schema': 3, 'transport': 'ghcr-oci', 'candidateSha': sha,
                   'runId': 39009939478, 'runAttempt': 1, 'status': 'READY',
                   'prismaHead': '20261015130000_codex_telegram_challenge', 'agataHead': 'agata0011',
                   'gsalesMigrationHead': '001_gsales', 'gsalesMigrationSha256': 'c' * 64,
                   'images': {role: {'repository': repository, 'sourceSha': sha, 'platform': 'linux/amd64',
                                     'digest': 'sha256:' + str(index) * 64,
                                     'configDigest': 'sha256:' + str(index + 3) * 64}
                              for index, (role, repository) in enumerate(ASSETS.RECOVERY_IMAGE_REPOSITORIES.items(), 1)}}
        payloads = {1: zip_bytes({result_name: result}), 2: zip_bytes({
                        f'manifests/{release_id}.json': manifest,
                        'manifests/gpoint-release-20261016T120000000Z.json': {'older': True},
                        'state/production.json': {'productionSha': 'b' * 40}}),
                    3: zip_bytes({'receipt.json': receipt})}
        asset_digests = {i: hashlib.sha256(data).hexdigest() for i, data in payloads.items()}
        def download(_repo, asset_id, _token, target, **_kwargs):
            Path(target).write_bytes(payloads[asset_id])
        evidence = {'run': {'id': run_id, 'attempt': attempt, 'headSha': sha,
                            'event': 'workflow_dispatch', 'path': '.github/workflows/gpoint-deploy.yml',
                            'conclusion': 'failure'},
                    'validateJob': {'status': 'completed', 'conclusion': 'success', 'headSha': sha},
                    'deployJob': {'status': 'completed', 'conclusion': 'failure', 'headSha': sha},
                    'tag': ASSETS.release_tag('evidence', sha, run_id, attempt),
                    'resultAsset': {'id': 1, 'size': len(payloads[1]), 'digest': 'sha256:' + asset_digests[1]},
                    'manifestAsset': {'id': 2, 'size': len(payloads[2]), 'digest': 'sha256:' + asset_digests[2]}}
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(ASSETS, 'find_post_switch_failure_evidence', return_value=evidence), \
             patch.object(ASSETS, 'find_prepare_evidence', return_value=('1', {'id': 3, 'size': len(payloads[3]),
                    'digest': 'sha256:' + asset_digests[3]})), \
             patch('prepared_artifact_cache.download', side_effect=download):
            target = Path(temporary) / 'verified-recovery.json'
            verified = ASSETS.read_post_switch_failure(sha, run_id, attempt, target, token='readonly', api=lambda _route: None)
            self.assertEqual(json.loads(target.read_text())['prepare']['receipt'], receipt)
            self.assertEqual(verified['failureManifestSha256'], hashlib.sha256(
                json.dumps(manifest).encode()).hexdigest())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            original_bytes = target.read_bytes()
            with self.assertRaises(FileExistsError):
                ASSETS.read_post_switch_failure(sha, run_id, attempt, target, token='readonly', api=lambda _route: None)
            self.assertEqual(target.read_bytes(), original_bytes)
            payloads[2] = b'corrupted'
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                ASSETS.read_post_switch_failure(sha, run_id, attempt, target, token='readonly', api=lambda _route: None)

    def test_failure_manifest_archive_rejects_unsafe_members(self):
        import io
        release_id = 'gpoint-release-20261017T064101334Z'
        expected = f'manifests/{release_id}.json'
        cases = {
            'traversal': [('../outside.json', b'{}'), (expected, b'{}')],
            'unknown path': [('other/extra.json', b'{}'), (expected, b'{}')],
            'compressed member': [(expected, b'{}')],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                buffer = io.BytesIO()
                compression = zipfile.ZIP_DEFLATED if label == 'compressed member' else zipfile.ZIP_STORED
                with zipfile.ZipFile(buffer, 'w', compression=compression) as archive:
                    for name, payload in entries:
                        archive.writestr(name, payload)
                with tempfile.NamedTemporaryFile() as target:
                    target.write(buffer.getvalue())
                    target.flush()
                    with self.assertRaisesRegex(RuntimeError, 'unsafe or incomplete'):
                        ASSETS._read_failure_manifest_archive(target.name, release_id)

        with self.subTest(label='symlink'):
            buffer = io.BytesIO()
            link = zipfile.ZipInfo(expected)
            link.create_system = 3
            link.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(link, b'outside')
            with tempfile.NamedTemporaryFile() as target:
                target.write(buffer.getvalue())
                target.flush()
                with self.assertRaisesRegex(RuntimeError, 'unsafe or incomplete'):
                    ASSETS._read_failure_manifest_archive(target.name, release_id)

        with self.subTest(label='duplicate'):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(expected, b'{}')
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(expected, b'{"candidateSha":"different"}')
            with tempfile.NamedTemporaryFile() as target:
                target.write(buffer.getvalue())
                target.flush()
                with self.assertRaisesRegex(RuntimeError, 'unsafe or incomplete'):
                    ASSETS._read_failure_manifest_archive(target.name, release_id)

        with self.subTest(label='member count limit'):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(expected, b'{}')
                for index in range(ASSETS.MAX_FAILURE_MANIFEST_MEMBERS):
                    archive.writestr(f'manifests/extra-{index}.json', b'')
            with tempfile.NamedTemporaryFile() as target:
                target.write(buffer.getvalue())
                target.flush()
                with self.assertRaisesRegex(RuntimeError, 'unsafe or incomplete'):
                    ASSETS._read_failure_manifest_archive(target.name, release_id)

        with self.subTest(label='member size limit'):
            buffer = io.BytesIO()
            oversized = b'x' * (ASSETS.MAX_FAILURE_MANIFEST_MEMBER_BYTES + 1)
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(expected, oversized)
            with tempfile.NamedTemporaryFile() as target:
                target.write(buffer.getvalue())
                target.flush()
                with self.assertRaisesRegex(RuntimeError, 'member exceeds size limit'):
                    ASSETS._read_failure_manifest_archive(target.name, release_id)

    def test_failed_deploy_cannot_silently_substitute_an_earlier_prepare_attempt(self):
        sha, run_id, attempt = 'a' * 40, '39010750184', '1'
        result_name = f'gpoint-release-result-{run_id}-{attempt}.json'
        result = {'candidateSha': sha, 'automationRunId': run_id, 'automationRunAttempt': attempt,
                  'status': 'RECOVERY_REQUIRED', 'releaseState': 'RECOVERY_REQUIRED', 'failureStage': 'POST_SWITCH',
                  'releaseId': 'gpoint-release-20261017T064101334Z', 'previousProductionSha': 'b' * 40,
                  'productionBaselineSha': 'b' * 40, 'productionRef': None, 'prepareRunId': '39009939478',
                  'prepareRunAttempt': '1', 'failureReason': 'GitHub production tag push failed on a non-transient error; refusing retry'}
        import io
        buff = io.BytesIO()
        with zipfile.ZipFile(buff, 'w') as archive:
            archive.writestr(result_name, json.dumps(result))
        result_bytes = buff.getvalue()
        digest = hashlib.sha256(result_bytes).hexdigest()
        manifest_buff = io.BytesIO()
        with zipfile.ZipFile(manifest_buff, 'w') as archive:
            archive.writestr('manifests/gpoint-release-20261017T064101334Z.json', json.dumps({
                'candidateSha': sha, 'releaseId': result['releaseId'], 'status': 'RECOVERY_REQUIRED',
                'failureStage': 'POST_SWITCH', 'previousProductionSha': 'b' * 40,
                'productionShaAfter': None, 'productionRef': None,
                'failureReason': result['failureReason']}))
        manifest_bytes = manifest_buff.getvalue()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        evidence = {'run': {'id': run_id, 'attempt': attempt, 'headSha': sha,
                            'event': 'workflow_dispatch', 'path': '.github/workflows/gpoint-deploy.yml',
                            'conclusion': 'failure'},
                    'validateJob': {'status': 'completed', 'conclusion': 'success', 'headSha': sha},
                    'deployJob': {'status': 'completed', 'conclusion': 'failure', 'headSha': sha},
                    'tag': ASSETS.release_tag('evidence', sha, run_id, attempt),
                    'resultAsset': {'id': 1, 'size': len(result_bytes), 'digest': 'sha256:' + digest},
                    'manifestAsset': {'id': 2, 'size': len(manifest_bytes), 'digest': 'sha256:' + manifest_digest}}
        def download(_repo, asset_id, _token, target, **_kwargs):
            Path(target).write_bytes(result_bytes if asset_id == 1 else manifest_bytes)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(ASSETS, 'find_post_switch_failure_evidence', return_value=evidence), \
             patch.object(ASSETS, 'find_prepare_evidence', return_value=('2', {'id': 3})), \
             patch('prepared_artifact_cache.download', side_effect=download):
            with self.assertRaisesRegex(RuntimeError, 'attempt differs'):
                ASSETS.read_post_switch_failure(sha, run_id, attempt, Path(temporary) / 'recovery.json', token='readonly')

if __name__ == '__main__':
    unittest.main()
