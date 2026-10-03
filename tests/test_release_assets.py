import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_assets as ASSETS


class ImmutableReleaseTests(unittest.TestCase):
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
            if change == 'missing': release['assets'] = []
            if change == 'duplicate': release['assets'] *= 2
            if change == 'pending': release['assets'][0]['state'] = 'new'
            if change == 'digest': release['assets'][0]['digest'] = ''
            if change == 'identity': release['assets'][0]['id'] = 'bad'
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                ASSETS.resolve_asset('prepare', sha, '55', '2', 'bundle.zip', github)

    def test_attempt_kind_and_sha_have_distinct_validated_names(self):
        self.assertNotEqual(ASSETS.release_tag('prepare', 'a'*40, '55', '1'),
                            ASSETS.release_tag('prepare', 'a'*40, '55', '2'))
        for args in [('prepare', 'main', '55', '1'), ('prepare', 'a'*40, '../55', '1'),
                     ('other', 'a'*40, '55', '1')]:
            with self.assertRaises(RuntimeError): ASSETS.release_tag(*args)

    def test_stale_deploy_result_from_previous_attempt_cannot_be_republished(self):
        data = {'candidateSha': 'a'*40, 'automationRunId': '55', 'automationRunAttempt': '1'}
        ASSETS.validate_result(data, 'a'*40, '55', '1')
        for sha, run_id, attempt in [('a'*40, '55', '2'), ('b'*40, '55', '1'), ('a'*40, '56', '1')]:
            with self.assertRaises(RuntimeError): ASSETS.validate_result(data, sha, run_id, attempt)

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

if __name__ == '__main__': unittest.main()
