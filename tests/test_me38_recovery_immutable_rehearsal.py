"""Opt-in rehearsal against the original immutable ME-38 release assets.

Set ME38_EXISTING_EVIDENCE_DIR to the directory holding manifests.zip,
result.zip, and prepare/bundle.zip. No GitHub or production access is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from scripts import release_assets


FIXTURE = Path(__file__).parent / 'fixtures' / 'me38-recovery-incident-evidence.json'
EVIDENCE_DIR = os.environ.get('ME38_EXISTING_EVIDENCE_DIR')


@unittest.skipUnless(EVIDENCE_DIR, 'set ME38_EXISTING_EVIDENCE_DIR for immutable evidence rehearsal')
class Me38ImmutableEvidenceRehearsal(unittest.TestCase):
    def test_original_immutable_failure_and_prepare_assets_pass_full_importer(self):
        root = Path(EVIDENCE_DIR)
        fixture = json.loads(FIXTURE.read_text())
        source = fixture['source']
        candidate = fixture['incidentResult']['candidateSha']
        failed_run = fixture['incidentResult']['automationRunId']
        failed_attempt = fixture['incidentResult']['automationRunAttempt']
        prepare_run = fixture['incidentResult']['prepareRunId']
        prepare_attempt = fixture['incidentResult']['prepareRunAttempt']

        files = {
            source['assets']['manifests.zip']['id']: root / 'manifests.zip',
            source['assets']['result.zip']['id']: root / 'result.zip',
            source['prepare']['bundleAssetId']: root / 'prepare' / 'bundle.zip',
        }
        for asset_id, file in files.items():
            self.assertTrue(file.is_file())
            expected = (source['assets']['manifests.zip']['sha256'] if asset_id == source['assets']['manifests.zip']['id']
                        else source['assets']['result.zip']['sha256'] if asset_id == source['assets']['result.zip']['id']
                        else source['prepare']['bundleSha256'])
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), expected)

        failure_tag = source['tag']
        prepare_tag = source['prepare']['tag']
        failure_release = {
            'tag_name': failure_tag, 'draft': False, 'immutable': True,
            'assets': [
                {'id': source['assets'][name]['id'], 'name': name, 'state': 'uploaded',
                 'size': source['assets'][name]['size'],
                 'digest': 'sha256:' + source['assets'][name]['sha256']}
                for name in ('result.zip', 'manifests.zip')
            ],
        }
        prepare_release = {
            'tag_name': prepare_tag, 'draft': False, 'immutable': True,
            'assets': [{'id': source['prepare']['bundleAssetId'], 'name': 'bundle.zip',
                        'state': 'uploaded', 'size': source['prepare']['bundleSize'],
                        'digest': 'sha256:' + source['prepare']['bundleSha256']}],
        }
        failed_run_obj = {'id': int(failed_run), 'run_attempt': 1, 'head_sha': candidate,
                          'event': 'workflow_dispatch', 'path': '.github/workflows/gpoint-deploy.yml',
                          'status': 'completed', 'conclusion': 'failure'}
        failed_jobs = {'total_count': 2, 'jobs': [
            {'name': 'validate', 'status': 'completed', 'conclusion': 'success', 'head_sha': candidate},
            {'name': 'deploy', 'status': 'completed', 'conclusion': 'failure', 'head_sha': candidate},
        ]}
        prepare_run_obj = {'id': int(prepare_run), 'run_attempt': 1, 'head_sha': candidate,
                           'run_started_at': '2026-10-17T06:30:00Z'}
        prepare_jobs = {'total_count': 1, 'jobs': [
            {'name': 'prepare', 'status': 'completed', 'conclusion': 'success', 'head_sha': candidate,
             'started_at': '2026-10-17T06:30:01Z', 'completed_at': '2026-10-17T06:40:00Z'},
        ]}
        routes = {
            f'actions/runs/{failed_run}/attempts/{failed_attempt}': failed_run_obj,
            f'actions/runs/{failed_run}/attempts/{failed_attempt}/jobs?per_page=100': failed_jobs,
            f'releases/tags/{failure_tag}': failure_release,
            f'git/ref/tags/{failure_tag}': {'object': {'type': 'commit', 'sha': candidate}},
            f'actions/runs/{prepare_run}/attempts/{prepare_attempt}': prepare_run_obj,
            f'actions/runs/{prepare_run}/attempts/{prepare_attempt}/jobs?per_page=100': prepare_jobs,
            f'releases/tags/{prepare_tag}': prepare_release,
            f'git/ref/tags/{prepare_tag}': {'object': {'type': 'commit', 'sha': candidate}},
        }

        def api(route):
            if route not in routes:
                raise AssertionError('unexpected evidence API route: ' + route)
            return routes[route]

        def copy_asset(repo, asset_id, token, target, release_asset=False, expected_size=None):
            self.assertEqual(repo, release_assets.REPO)
            self.assertEqual(token, 'local-rehearsal-token')
            self.assertTrue(release_asset)
            source_file = files[int(asset_id)]
            self.assertEqual(source_file.stat().st_size, expected_size)
            shutil.copyfile(source_file, target)

        import prepared_artifact_cache
        original_download = prepared_artifact_cache.download
        prepared_artifact_cache.download = copy_asset
        try:
            with tempfile.TemporaryDirectory(prefix='me38-immutable-rehearsal-') as temporary:
                output = Path(os.environ.get('ME38_VERIFIED_FAILURE_OUTPUT', Path(temporary) / 'verified-failure.json'))
                output.parent.mkdir(parents=True, exist_ok=True)
                verified = release_assets.read_post_switch_failure(
                    candidate, failed_run, failed_attempt, output,
                    token='local-rehearsal-token', api=api)
                self.assertEqual(verified['run']['id'], failed_run)
                self.assertEqual(verified['result']['status'], 'RECOVERY_REQUIRED')
                self.assertEqual(verified['failureManifestSha256'], source['incidentManifestSha256'])
                self.assertEqual(verified['prepare']['bundleDigest'], 'sha256:' + source['prepare']['bundleSha256'])
                self.assertTrue(output.is_file())
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        finally:
            prepared_artifact_cache.download = original_download


if __name__ == '__main__':
    unittest.main()
