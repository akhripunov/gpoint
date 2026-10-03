import importlib.util
import ssl
import urllib.error
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import network_retry
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
        env = dict(GITHUB_REPOSITORY='akhripunov/maxibot', CANDIDATE_SHA='a' * 40,
                   PREPARE_RUN_ID='1', PREPARE_RUN_ATTEMPT='1', PREPARE_ARTIFACT_ID='12',
                   GITHUB_TOKEN='unused', ARTIFACT_DESTINATION=str(self.root / 'destination'))
        run = dict(status='completed', conclusion='success', head_sha='a' * 40,
                   head_branch='main', event='push', path='.github/workflows/maxibot-prepare.yml', run_attempt=1)
        def api(repo, route, token, stage):
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

    def test_network_get_retries_transient_failure_with_bounded_backoff(self):
        attempts = []
        def request():
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.URLError(ConnectionResetError('temporary'))
            return 'ok'
        with patch.object(CACHE.time, 'sleep') as sleep:
            self.assertEqual(CACHE.network_request(request, 'artifact_metadata'), 'ok')
        self.assertEqual(len(attempts), 3)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertTrue(0 <= delays[0] <= 1)
        self.assertTrue(0 <= delays[1] <= 2)

    def test_long_tls_outage_recovers_within_shared_30_minute_budget(self):
        now = [0.0]
        attempts = []
        events = []
        budget = network_retry.RetryBudget(1800, clock=lambda: now[0])
        def operation():
            attempts.append(now[0])
            if len(attempts) <= 27:
                raise urllib.error.URLError(TimeoutError('TLS read timeout'))
            return 'verified metadata'
        def sleep(seconds): now[0] += max(seconds, 0.001)
        result = network_retry.retry_network_read(
            operation, 'artifact_metadata', CACHE.retryable, budget=budget,
            clock=lambda: now[0], sleep=sleep,
            jitter=lambda low, high: high, emit=events.append)
        self.assertEqual(result, 'verified metadata')
        self.assertGreater(now[0], 1200)
        self.assertLess(now[0], 1800)
        self.assertIn('NETWORK_RETRYING', '\n'.join(events))
        self.assertIn('NETWORK_RESTORED', '\n'.join(events))
        self.assertEqual(events[-1], 'RELEASE_CONTINUING stage=artifact_metadata')

    def test_long_tls_outage_expires_at_30_minutes_as_network_unavailable(self):
        now = [0.0]
        events = []
        attempts = []
        budget = network_retry.RetryBudget(1800, clock=lambda: now[0])
        def operation():
            attempts.append(1)
            raise urllib.error.URLError(TimeoutError('TLS read timeout'))
        def sleep(seconds): now[0] += seconds
        with self.assertRaises(network_retry.NetworkUnavailable):
            network_retry.retry_network_read(
                operation, 'current_main_ref', CACHE.retryable, budget=budget,
                clock=lambda: now[0], sleep=sleep,
                jitter=lambda low, high: high, emit=events.append)
        self.assertEqual(now[0], 1800)
        self.assertGreater(len(attempts), 25)
        self.assertIn('NETWORK_UNAVAILABLE', events[-1])

    def test_network_error_reports_stage_without_url_or_reason(self):
        secret_url = 'https://blob.example/archive?signature=do-not-log'
        def request():
            raise urllib.error.URLError(secret_url)
        with patch.object(CACHE.time, 'sleep') as sleep:
            with self.assertRaises(CACHE.NetworkStageError) as raised:
                CACHE.network_request(request, 'artifact_zip_storage')
        self.assertEqual(raised.exception.stage, 'artifact_zip_storage')
        self.assertEqual(raised.exception.error_type, 'str')
        self.assertNotIn(secret_url, str(raised.exception))
        sleep.assert_not_called()

    def test_certificate_validation_failure_is_not_retried(self):
        attempts = []
        def request():
            attempts.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))
        with patch.object(CACHE.time, 'sleep') as sleep:
            with self.assertRaises(CACHE.NetworkStageError) as raised:
                CACHE.network_request(request, 'artifact_metadata')
        self.assertEqual(raised.exception.error_type, 'SSLCertVerificationError')
        self.assertEqual(len(attempts), 1)
        sleep.assert_not_called()

    def test_expected_github_redirect_is_not_retried(self):
        redirect = urllib.error.HTTPError('https://api.github.com/artifact', 302, 'Found', {}, None)
        with patch.object(CACHE.time, 'sleep') as sleep:
            result = CACHE.network_request(lambda: (_ for _ in ()).throw(redirect),
                                           'artifact_zip_redirect', allow_redirect=True)
        self.assertIs(result, redirect)
        sleep.assert_not_called()

    def test_download_refreshes_signed_url_after_transient_storage_failure(self):
        import io
        redirects=[]
        storage=[]
        token='secret-test-token'
        def api_open(request, timeout):
            self.assertEqual(request.get_header('Authorization'), 'Bearer '+token)
            redirects.append(1)
            raise urllib.error.HTTPError(request.full_url,302,'Found',
                {'Location':f'https://storage.example/archive?attempt={len(redirects)}'},None)
        class BrokenBlob(io.BytesIO):
            calls=0
            def read(self,size):
                self.calls+=1
                if self.calls>1:
                    raise TimeoutError('simulated mid-transfer interruption')
                return super().read(size)
        def storage_open(location, timeout):
            self.assertIsInstance(location,str)  # No GitHub Authorization forwarded.
            storage.append(location)
            if len(storage)==1:
                return BrokenBlob(b'partial artifact to discard')
            if location.endswith('attempt=1'):
                raise urllib.error.HTTPError(location,403,'Expired signed URL',{},None)
            return io.BytesIO(b'verified complete artifact')
        def bounded_retry(operation, stage, allow_redirect=False):
            for attempt in range(2):
                try:return operation()
                except urllib.error.HTTPError as exc:
                    if allow_redirect and exc.code==302:return exc
                    raise
                except TimeoutError:
                    if attempt:raise
        with patch.object(CACHE.urllib.request,'build_opener') as opener, \
             patch.object(CACHE.urllib.request,'urlopen',side_effect=storage_open), \
             patch.object(CACHE,'network_request',side_effect=bounded_retry):
            opener.return_value.open.side_effect=api_open
            target=self.root/'refreshed.zip'
            CACHE.download('owner/repo',123,token,target)
        self.assertEqual(target.read_bytes(),b'verified complete artifact')
        self.assertEqual(len(redirects),2)
        self.assertNotEqual(storage[0],storage[1])


    def test_artifact_download_auth_failure_is_not_retried(self):
        with patch.object(CACHE.urllib.request,'build_opener') as opener, \
             patch.object(CACHE.time,'sleep') as sleep:
            opener.return_value.open.side_effect=urllib.error.HTTPError(
                'https://api.github.com/artifact',401,'Unauthorized',{},None)
            with self.assertRaises(CACHE.NetworkStageError):
                CACHE.download('owner/repo',123,'test-token',self.root/'denied.zip')
        self.assertEqual(opener.return_value.open.call_count,1)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
