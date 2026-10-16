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
import network_retry  # noqa: E402
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

    def restore(self, artifact_id='12', digest=None, identity=None):
        return CACHE.materialize(self.root / 'cache', self.root / 'destination', artifact_id,
                                 digest or self.digest, ['receipt.json'], self.fetch,
                                 identity=identity or {'project':'gpoint','sha':'a'*40,'tag':'tag-1',
                                                       'run_id':'1','attempt':'1','asset_id':artifact_id,
                                                       'asset_sha256':digest or self.digest})

    def test_second_job_reuses_zip_but_checks_bytes_again(self):
        self.assertEqual(self.restore(), 'MISS')
        self.assertEqual(self.restore(), 'HIT')
        self.assertEqual(self.downloads, 1)
        next((self.root / 'cache').glob('v2-*.zip')).write_bytes(b'corrupt cache')
        self.assertEqual(self.restore(), 'MISS')
        self.assertEqual(self.downloads, 2)

    def test_archive_fallback_requires_exact_receipt_members_and_checksum(self):
        import json
        receipt = {'schema': 3, 'candidateSha': 'a' * 40,
                   'archiveSha256': __import__('hashlib').sha256(b'image archive').hexdigest()}
        fallback = self.root / 'fallback.zip'
        with zipfile.ZipFile(fallback, 'w') as output:
            output.writestr('images.tar.gz', b'image archive')
            output.writestr('receipt.json', json.dumps(receipt, sort_keys=True))
        target = CACHE.verify_and_install_archive_fallback(fallback, self.root / 'selected', receipt)
        self.assertEqual(target.read_bytes(), b'image archive')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            CACHE.verify_and_install_archive_fallback(fallback, self.root / 'other', {**receipt, 'candidateSha': 'b' * 40})
        with zipfile.ZipFile(fallback, 'w') as output:
            output.writestr('images.tar.gz', b'changed')
            output.writestr('receipt.json', json.dumps(receipt))
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            CACHE.verify_and_install_archive_fallback(fallback, self.root / 'bad', receipt)

    def test_archive_fallback_streams_large_image_member_instead_of_reading_it_all(self):
        import json
        receipt = {'schema': 3, 'candidateSha': 'a' * 40,
                   'archiveSha256': __import__('hashlib').sha256(b'image archive').hexdigest()}
        fallback = self.root / 'fallback-stream.zip'
        with zipfile.ZipFile(fallback, 'w') as output:
            output.writestr('images.tar.gz', b'image archive')
            output.writestr('receipt.json', json.dumps(receipt, sort_keys=True))
        original_read = CACHE.zipfile.ZipFile.read

        def reject_unbounded_image_read(archive, name, *args, **kwargs):
            if name == 'images.tar.gz':
                raise AssertionError('large image member must be streamed')
            return original_read(archive, name, *args, **kwargs)

        with patch.object(CACHE.zipfile.ZipFile, 'read', new=reject_unbounded_image_read):
            target = CACHE.verify_and_install_archive_fallback(
                fallback, self.root / 'streamed', receipt)
        self.assertEqual(target.read_bytes(), b'image archive')

    def test_changed_artifact_id_cannot_reuse_another_attempt(self):
        self.restore('12')
        self.assertEqual(self.restore('13'), 'MISS')
        self.assertEqual(self.downloads, 2)

    def test_changed_prepare_attempt_cannot_reuse_cached_bundle(self):
        first = {'project':'gpoint','sha':'a'*40,'tag':'tag-1','run_id':'1','attempt':'1',
                 'asset_id':'12','asset_sha256':self.digest}
        second = {**first, 'attempt':'2'}
        self.assertEqual(self.restore(identity=first), 'MISS')
        self.assertEqual(self.restore(identity=first), 'HIT')
        self.assertEqual(self.restore(identity=second), 'MISS')
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
            if route == 'actions/runs/1':
                return run
            if route == 'git/ref/heads/main':
                return {'object': {'sha': 'a' * 40}}
            return {'expired': True}
        with patch.dict(CACHE.os.environ, env), patch.object(CACHE, 'github', api):
            with self.assertRaisesRegex(RuntimeError, 'selected Prepare'):
                CACHE.main()

    def test_cache_is_bounded_without_touching_other_files(self):
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / 'operator-notes.txt').write_text('keep')
        for i in range(10, 15):
            self.restore(str(i))
        self.assertEqual(len(list(cache.glob('*.zip'))), 1)
        self.assertTrue((cache / 'operator-notes.txt').exists())

    def test_cache_prunes_only_superseded_release_zip_identities(self):
        cache = self.root / 'cache'
        cache.mkdir()
        legacy = cache / ('12-' + 'a' * 64 + '.zip')
        previous_v2 = cache / ('v2-' + 'b' * 64 + '.zip')
        unrelated = cache / 'operator-backup.zip'
        for path in (legacy, previous_v2, unrelated):
            path.write_bytes(b'old')

        self.restore('13')

        self.assertFalse(legacy.exists())
        self.assertFalse(previous_v2.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(len(list(cache.glob('v2-*.zip'))), 1)

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
                try:
                    return operation()
                except urllib.error.HTTPError as exc:
                    if allow_redirect and exc.code == 302:
                        return exc
                    raise
                except TimeoutError:
                    if attempt:
                        raise
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

    def test_parallel_range_transfer_reassembles_exact_bytes(self):
        content = bytes(index % 251 for index in range(1024 * 1024 + 319))
        calls = []
        target = self.root / 'ranges.zip'

        def fetch_range(start, end, part):
            calls.append((start, end))
            part.write_bytes(content[start:end + 1])

        CACHE.download_ranges(len(content), target, fetch_range, workers=4, chunk_size=256 * 1024)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(sum(end - start + 1 for start, end in calls), len(content))
        self.assertEqual(min(start for start, _end in calls), 0)
        self.assertEqual(max(end for _start, end in calls), len(content) - 1)

    def test_parallel_range_transfer_rejects_incomplete_part_atomically(self):
        target = self.root / 'ranges.zip'
        target.write_bytes(b'previous')

        def fetch_range(_start, _end, part):
            part.write_bytes(b'bad')

        with self.assertRaises(CACHE.ReleaseAssetError):
            CACHE.download_ranges(100, target, fetch_range, workers=2, chunk_size=50)
        self.assertEqual(target.read_bytes(), b'previous')

    def test_release_asset_ranges_strip_auth_and_validate_content_range(self):
        import io
        payload = b'immutable release bytes'
        api_requests, storage_requests = [], []
        token = 'test-release-token'

        def api_open(request, timeout):
            api_requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 302, 'Found',
                {'Location': 'https://blob.example/bundle.zip?sig=short-lived'}, None)

        class RangeResponse(io.BytesIO):
            headers = {'Content-Range': f'bytes 0-{len(payload) - 1}/{len(payload)}',
                       'Content-Length': str(len(payload))}
            def getcode(self): return 206

        def storage_open(request, timeout):
            storage_requests.append(request)
            self.assertIsNone(request.get_header('Authorization'))
            self.assertEqual(request.get_header('Range'), f'bytes=0-{len(payload) - 1}')
            return RangeResponse(payload)

        with patch.object(CACHE.urllib.request, 'build_opener') as opener, \
             patch.object(CACHE.urllib.request, 'urlopen', side_effect=storage_open), \
             patch.object(CACHE, 'network_request', side_effect=lambda operation, *_args, **_kwargs: operation()):
            opener.return_value.open.side_effect = api_open
            target = self.root / 'release.zip'
            CACHE.download('owner/repo', 72, token, target, release_asset=True, expected_size=len(payload))

        self.assertEqual(api_requests[0].get_header('Authorization'), f'Bearer {token}')
        self.assertEqual(api_requests[0].get_header('Accept'), 'application/octet-stream')
        self.assertEqual(len(storage_requests), 1)
        self.assertEqual(target.read_bytes(), payload)

    def test_release_asset_rejects_wrong_content_range_without_replacing_target(self):
        import io
        payload = b'immutable release bytes'

        class WrongRange(io.BytesIO):
            headers = {'Content-Range': f'bytes 1-{len(payload)}/{len(payload) + 1}',
                       'Content-Length': str(len(payload))}
            def getcode(self): return 206

        target = self.root / 'release.zip'
        target.write_bytes(b'previous')
        with patch.object(CACHE.urllib.request, 'build_opener') as opener, \
             patch.object(CACHE.urllib.request, 'urlopen', return_value=WrongRange(payload)), \
             patch.object(CACHE, 'network_request', side_effect=lambda operation, *_args, **_kwargs: operation()):
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                'https://api.github.com/assets/72', 302, 'Found',
                {'Location': 'https://blob.example/bundle.zip?sig=short-lived'}, None)
            with self.assertRaisesRegex(CACHE.ReleaseAssetError, 'byte range'):
                CACHE.download('owner/repo', 72, 'test-token', target,
                               release_asset=True, expected_size=len(payload))
        self.assertEqual(target.read_bytes(), b'previous')


if __name__ == '__main__':
    unittest.main()
