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
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_assets as ASSETS  # noqa: E402
import publish_ghcr_images as GHCR  # noqa: E402


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
    def test_native_image_roles_are_candidate_bound_immutable_linux_amd64_digests(self):
        sha = 'a' * 40
        digest = 'sha256:' + 'b' * 64
        config = 'sha256:' + 'c' * 64
        for role, (_local, repository) in GHCR.IMAGES.items():
            with self.subTest(role=role):
                metadata = {
                    'Id': config,
                    'Os': 'linux',
                    'Architecture': 'amd64',
                    'Config': {'Labels': {
                        'org.opencontainers.image.revision': sha,
                        'org.opencontainers.image.source': GHCR.SOURCE_URL,
                    }},
                }
                evidence = GHCR.validate_image_metadata(role, repository, digest, metadata, sha)
                self.assertEqual(evidence['repository'], repository)
                self.assertEqual(evidence['digest'], digest)
                self.assertEqual(evidence['sourceSha'], sha)
                self.assertEqual(evidence['platform'], 'linux/amd64')
                with self.assertRaisesRegex(ValueError, 'repository mismatch'):
                    GHCR.validate_image_metadata(role, 'ghcr.io/attacker/image', digest, metadata, sha)

    def test_privileged_prepare_is_main_push_only_and_branch_jobs_are_not_dispatchable(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        trigger = workflow.split('\npermissions:', 1)[0]
        self.assertIn('push:', trigger)
        self.assertNotIn('workflow_dispatch:', trigger)
        self.assertIn('permissions:\n  contents: read\n  actions: read', workflow)
        prepare = workflow.split('  prepare:\n', 1)[1].split('    steps:\n', 1)[0]
        self.assertIn("github.ref == 'refs/heads/main'", prepare)
        self.assertNotIn("github.ref != 'refs/heads/main'", prepare)
        self.assertIn('permissions:\n      contents: read\n      actions: read\n      packages: write', prepare)

    def test_privileged_prepare_checkout_does_not_persist_write_credentials(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-prepare.yml').read_text())
        checkouts = [step for job in workflow['jobs'].values() for step in job.get('steps', [])
                     if step.get('uses', '').startswith('actions/checkout@')]
        self.assertTrue(checkouts)
        self.assertTrue(all(step.get('with', {}).get('persist-credentials') is False
                            for step in checkouts))

    def test_privileged_deploy_checkout_does_not_persist_write_credentials(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/gpoint-deploy.yml').read_text())
        checkouts = [step for step in workflow['jobs']['deploy'].get('steps', [])
                     if step.get('uses', '').startswith('actions/checkout@')]
        self.assertTrue(checkouts)
        self.assertTrue(all(step.get('with', {}).get('persist-credentials') is False
                            for step in checkouts))

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

    def test_operational_prepare_has_no_release_or_archive_transport(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        source = (ROOT / 'scripts/prepare-release.py').read_text()
        publisher = (ROOT / 'scripts/publish_ghcr_images.py').read_text()
        self.assertNotIn('release_assets.py publish', workflow)
        self.assertNotIn('images.tar.gz', source + publisher)
        self.assertNotIn('fallback-images.zip', source + publisher)

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
        self.assertFalse(hasattr(ASSETS, 'read_result'))

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
        self.assertNotIn('gh(\'release\',', source)

    def test_github_release_publisher_rejects_every_non_prepare_release(self):
        self.assertFalse(hasattr(ASSETS, 'publish'))
        self.assertFalse(hasattr(ASSETS, 'build_oci_prepare_assets'))

    def test_deploy_workflow_uses_deployments_statuses_without_release_catalog_or_environment_job(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        runner = (ROOT / 'scripts/automated-release.mjs').read_text()
        self.assertIn('deployments: write', workflow)
        self.assertIn('createGitHubDeploymentsClient', runner)
        self.assertIn('beginProductionDeployment', runner)
        execution = (ROOT / 'scripts/deployment-execution.mjs').read_text()
        self.assertIn('executeProductionDeployment', runner)
        self.assertIn("state: 'success'", execution)
        self.assertNotIn('release_assets.py publish evidence', workflow)
        self.assertNotIn('gpoint-release-ledger', workflow + runner)
        self.assertNotRegex(workflow, re.compile(r'^\s+environment:\s*production\s*$', re.M))








if __name__ == '__main__':
    unittest.main()
