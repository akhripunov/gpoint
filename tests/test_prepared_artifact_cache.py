import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('artifact_cache', ROOT / 'scripts/prepared_artifact_cache.py')
CACHE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE)


class ArtifactCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'source.zip'
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('receipt.json', 'verified receipt')
        self.digest = CACHE.checksum(self.archive)
        self.downloads = 0

    def fetch(self, target):
        self.downloads += 1
        shutil.copyfile(self.archive, target)

    def restore(self, artifact_id='12', digest=None):
        return CACHE.materialize(self.root / 'cache', self.root / 'destination', artifact_id,
                                 digest or self.digest, ['receipt.json'], self.fetch)

    def test_second_job_reuses_zip_but_checks_bytes_again(self):
        self.assertEqual(self.restore(), 'MISS')
        self.assertEqual(self.restore(), 'HIT')
        self.assertEqual(self.downloads, 1)
        (self.root / 'cache' / f'12-{self.digest}.zip').write_bytes(b'corrupt cache')
        self.assertEqual(self.restore(), 'MISS')
        self.assertEqual(self.downloads, 2)

    def test_changed_artifact_id_cannot_reuse_another_attempt(self):
        self.restore('12')
        self.assertEqual(self.restore('13'), 'MISS')
        self.assertEqual(self.downloads, 2)

    def test_checksum_failure_is_not_admitted_or_extracted(self):
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.restore(digest='0' * 64)
        self.assertFalse((self.root / 'destination').exists())
        self.assertEqual(list((self.root / 'cache').glob('*.zip')), [])

    def test_unexpected_and_traversal_members_are_rejected(self):
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('../escaped', 'unsafe')
        self.digest = CACHE.checksum(self.archive)
        with self.assertRaisesRegex(RuntimeError, 'members'):
            self.restore()
        self.assertFalse((self.root / 'escaped').exists())

    def test_expired_artifact_blocks_even_with_cached_bytes(self):
        self.restore()
        env = dict(GITHUB_REPOSITORY='akhripunov/gpoint', CANDIDATE_SHA='a' * 40,
                   PREPARE_RUN_ID='1', PREPARE_RUN_ATTEMPT='1', PREPARE_ARTIFACT_ID='12',
                   GITHUB_TOKEN='unused', ARTIFACT_DESTINATION=str(self.root / 'destination'))
        run = dict(status='completed', conclusion='success', head_sha='a' * 40,
                   head_branch='main', event='push', path='.github/workflows/gpoint-prepare.yml', run_attempt=1)
        def api(repo, route, token):
            if route == 'actions/runs/1': return run
            if route == 'git/ref/heads/main': return {'object': {'sha': 'a' * 40}}
            return {'expired': True}
        with patch.dict(CACHE.os.environ, env), patch.object(CACHE, 'github', api):
            with self.assertRaisesRegex(RuntimeError, 'selected Prepare'):
                CACHE.main()

    def test_cache_is_bounded_without_touching_other_files(self):
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / 'operator-notes.txt').write_text('keep')
        for i in range(10, 15): self.restore(str(i))
        self.assertEqual(len(list(cache.glob('*.zip'))), 3)
        self.assertTrue((cache / 'operator-notes.txt').exists())


if __name__ == '__main__':
    unittest.main()
