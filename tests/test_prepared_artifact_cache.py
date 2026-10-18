import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import socket
import ssl
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepared_artifact_cache.py'
spec = importlib.util.spec_from_file_location('prepared_artifact_cache', SCRIPT)
CACHE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CACHE)
import network_retry


class GhcrReceiptTests(unittest.TestCase):
    def setUp(self):
        CACHE.ACTIVE_DIAGNOSTIC_STAGE = 'initialization'
        CACHE.DIAGNOSTIC_IDENTITY = {'candidateSha': '', 'runId': '', 'runAttempt': ''}
        CACHE.GHCR_READBACK_COMPLETED = False
        self.sha, self.run_id, self.attempt = 'a' * 40, '123', '2'
        receipt = {'schema': 3, 'transport': 'ghcr-oci', 'status': 'READY',
                   'candidateSha': self.sha, 'runId': 123, 'runAttempt': 2,
                   'gates': {'unit': 'PASS'}}
        self.blob = json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        layer_digest = 'sha256:' + hashlib.sha256(self.blob).hexdigest()
        self.manifest = json.dumps({'schemaVersion': 2, 'mediaType': CACHE.OCI_MANIFEST,
            'artifactType': CACHE.RECEIPT_MEDIA_TYPE, 'layers': [{'mediaType': CACHE.RECEIPT_LAYER_MEDIA_TYPE,
            'digest': layer_digest, 'size': len(self.blob)}]}, sort_keys=True, separators=(',', ':')).encode()
        self.digest = 'sha256:' + hashlib.sha256(self.manifest).hexdigest()
        self.tag_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/prepare-{self.sha}-{self.run_id}-{self.attempt}'
        self.digest_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/{self.digest}'
        self.blob_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/blobs/{layer_digest}'
        self.manifest_header = {'Docker-Content-Digest': self.digest}

    def _ghcr_token_request(self, token_error=None, status=None, observed_token_calls=None):
        """Exercise ghcr_request through the real bounded retry implementation."""
        token_calls = observed_token_calls if observed_token_calls is not None else []
        challenge = (f'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                     f'scope="repository:{CACHE.RECEIPT_REPOSITORY}:pull"')

        class Response:
            def __init__(self, body, headers=None):
                self.body, self.headers = body, headers or {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit=-1): return self.body

        def urlopen(req, timeout=30):
            path = urllib.parse.urlparse(req.full_url).path
            if path.startswith('/v2/') and 'Authorization' not in req.headers:
                raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized',
                    {'WWW-Authenticate': challenge}, __import__('io').BytesIO())
            if path == '/token':
                token_calls.append(req)
                if status:
                    raise urllib.error.HTTPError(req.full_url, status, 'private response detail', {},
                                                 __import__('io').BytesIO())
                if token_error and len(token_calls) <= len(token_error):
                    raise token_error[len(token_calls) - 1]
                return Response(b'{"token":"fixture-bearer"}')
            if path.startswith('/v2/') and req.headers.get('Authorization') == 'Bearer fixture-bearer':
                return Response(b'fixture-artifact', {'Docker-Content-Digest': self.digest})
            raise AssertionError(f'unexpected GHCR request path: {path}')

        clock_value = [0.0]
        def clock(): return clock_value[0]
        def sleep(seconds): clock_value[0] += max(seconds, 0.01)
        def run_retry(operation, stage, is_retryable, *, budget=None):
            bounded = network_retry.RetryBudget(seconds=0.1, clock=clock)
            return network_retry.retry_network_read(operation, stage, is_retryable,
                budget=bounded, clock=clock, sleep=sleep, jitter=lambda *_: 0.01, emit=lambda *_: None)

        with mock.patch.object(urllib.request, 'urlopen', side_effect=urlopen), \
                mock.patch.object(CACHE, 'retry_network_read', side_effect=run_retry):
            result = CACHE.ghcr_request(self.tag_url, 'reader', 'fixture-secret', CACHE.OCI_MANIFEST,
                                        'ghcr_receipt_tag_manifest')
        return result, token_calls

    def test_transient_tls_eof_during_token_request_retries_then_reads(self):
        result, token_calls = self._ghcr_token_request(
            token_error=[ssl.SSLEOFError(8, 'unexpected EOF')])
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(result[0], b'fixture-artifact')

    def test_connection_reset_during_token_request_retries_then_reads(self):
        result, token_calls = self._ghcr_token_request(
            token_error=[ConnectionResetError('connection reset')])
        self.assertEqual(len(token_calls), 2)
        self.assertEqual(result[0], b'fixture-artifact')

    def test_persistent_tls_eof_stops_within_shared_retry_budget(self):
        token_calls = []
        try:
            self._ghcr_token_request(token_error=[ssl.SSLEOFError(8, 'unexpected EOF')] * 50,
                                     observed_token_calls=token_calls)
        except CACHE.NetworkStageError as error:
            self.assertEqual(error.stage, 'ghcr_receipt_tag_manifest_auth_token')
            self.assertEqual(error.error_type, 'NETWORK_UNAVAILABLE')
            self.assertGreater(len(token_calls), 1)
            self.assertNotRegex(str(error), r'fixture-secret|fixture-bearer|https?://|Authorization')
        else:
            self.fail('persistent TLS EOF should exhaust the shared retry budget')

    def test_token_http_401_and_403_are_not_retried(self):
        for status, category in ((401, 'AUTH'), (403, 'PERMISSION')):
            with self.subTest(status=status):
                token_calls = []
                with self.assertRaises(CACHE.NetworkStageError) as raised:
                    self._ghcr_token_request(status=status, observed_token_calls=token_calls)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(len(token_calls), 1)

    def test_certificate_verification_failure_is_not_retried(self):
        token_calls = []
        with self.assertRaises(CACHE.NetworkStageError) as raised:
            self._ghcr_token_request(token_error=[ssl.SSLCertVerificationError(1, 'certificate verify failed')],
                                     observed_token_calls=token_calls)
        self.assertEqual(raised.exception.error_type, 'SSLCertVerificationError')
        self.assertEqual(raised.exception.category, 'NETWORK')
        self.assertEqual(len(token_calls), 1)

    def test_ghcr_retry_budget_stays_below_controller_subprocess_timeout(self):
        controller = (SCRIPT.parents[1] / 'scripts/release-orchestrator.mjs').read_text()
        timeout = re.search(r"spawnSync\('python3', \['scripts/prepared_artifact_cache\.py'\], \{.*?timeout:\s*(\d+)_000",
                            controller, re.DOTALL)
        self.assertIsNotNone(timeout)
        self.assertEqual(int(timeout.group(1)), 120)
        self.assertEqual(CACHE.GHCR_READ_RETRY_BUDGET_SECONDS, 60)
        self.assertLess(CACHE.GHCR_READ_RETRY_BUDGET_SECONDS, int(timeout.group(1)))

    def test_urllib_timeout_is_clamped_to_absolute_shared_deadline(self):
        class Budget:
            deadline = 108.0
        with mock.patch.object(CACHE, 'NETWORK_BUDGET', Budget()), \
                mock.patch.object(CACHE.time, 'monotonic', return_value=100.0), \
                mock.patch.object(urllib.request, 'urlopen', return_value='response') as urlopen:
            self.assertEqual(CACHE.network_urlopen('request', timeout=30), 'response')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 8.0)
        with mock.patch.object(CACHE, 'NETWORK_BUDGET', Budget()), \
                mock.patch.object(CACHE.time, 'monotonic', return_value=108.0), \
                mock.patch.object(urllib.request, 'urlopen') as urlopen:
            with self.assertRaises(network_retry.NetworkUnavailable):
                CACHE.network_urlopen('request', timeout=30)
        urlopen.assert_not_called()

    def test_response_finishing_after_shared_deadline_is_rejected(self):
        class Budget:
            deadline = 108.0
        class SlowResponse:
            def read(self, _limit):
                return b'late response'
        with mock.patch.object(CACHE, 'NETWORK_BUDGET', Budget()), \
                mock.patch.object(CACHE.time, 'monotonic', return_value=108.01):
            with self.assertRaises(network_retry.NetworkUnavailable):
                CACHE.read_network_response(SlowResponse(), 1024)

    def test_blocked_response_read_is_interrupted_at_shared_deadline(self):
        started = threading.Event()
        released = threading.Event()
        class Budget:
            deadline = time.monotonic() + 0.05
        class SlowResponse:
            closed = False
            def read(self, _limit):
                started.set()
                released.wait(1)
                return b'late response'
            def close(self):
                self.closed = True
                released.set()
        response = SlowResponse()
        began = time.monotonic()
        with mock.patch.object(CACHE, 'NETWORK_BUDGET', Budget()):
            with self.assertRaises(network_retry.NetworkUnavailable):
                CACHE.read_network_response(response, 1024)
        self.assertTrue(started.is_set())
        self.assertTrue(response.closed)
        self.assertLess(time.monotonic() - began, 0.5)

    def test_ghcr_budget_reuses_prior_shared_budget_spend(self):
        with tempfile.TemporaryDirectory(prefix='prepared-cache-budget-') as directory:
            state = Path(directory) / 'spent-seconds'
            state.write_text('50\n')
            with mock.patch.dict(os.environ, {'NETWORK_RETRY_BUDGET_STATE': str(state)}):
                budget = CACHE.new_network_budget()
        self.assertEqual(budget.seconds, 60)
        self.assertEqual(budget.spent, 50.0)
        self.assertLessEqual(budget.deadline - budget.started, 10.0)

    def fetch(self, tamper=None):
        def fake(url, username, token, accept, stage, **kwargs):
            if url == self.tag_url:
                return (self.manifest, self.manifest_header)
            if url == self.digest_url:
                return (self.manifest if tamper != 'digest-readback' else b'{}', self.manifest_header)
            if url == self.blob_url:
                return (self.blob if tamper != 'layer' else self.blob + b'bad', {})
            raise AssertionError(url)
        return fake

    def test_resolves_mutable_tag_then_refetches_by_digest_and_hashes_bytes(self):
        receipt, digest, blob = CACHE.resolve_receipt(self.sha, self.run_id, self.attempt,
            'reader', 'token', self.fetch())
        self.assertEqual(digest, self.digest)
        self.assertEqual(receipt['candidateSha'], self.sha)
        self.assertEqual(hashlib.sha256(blob).hexdigest(), hashlib.sha256(self.blob).hexdigest())

    def test_rejects_receipt_layer_with_wrong_media_type(self):
        manifest = json.loads(self.manifest)
        manifest['layers'][0]['mediaType'] = CACHE.RECEIPT_MEDIA_TYPE
        raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        def fetch(url, *_args, **_kwargs):
            self.assertEqual(url, f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/{digest}')
            return raw, {'Docker-Content-Digest': digest}
        with self.assertRaisesRegex(RuntimeError, 'layer descriptor is invalid'):
            CACHE.fetch_oci_layer(CACHE.RECEIPT_REPOSITORY, digest, CACHE.RECEIPT_MEDIA_TYPE,
                CACHE.RECEIPT_LAYER_MEDIA_TYPE, 'reader', 'token', fetch=fetch)

    def test_tampered_digest_readback_blocks(self):
        with self.assertRaisesRegex(RuntimeError, 'changed between tag resolution'):
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, 'reader', 'token', self.fetch('digest-readback'))

    def test_tampered_layer_blocks(self):
        with self.assertRaisesRegex(RuntimeError, 'digest or size mismatch'):
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, 'reader', 'token', self.fetch('layer'))

    def test_receipt_must_be_canonical_and_exact_run_identity(self):
        receipt, digest, _ = CACHE.resolve_receipt(self.sha, self.run_id, self.attempt,
            'reader', 'token', self.fetch())
        self.assertEqual(receipt['runAttempt'], 2)
        self.assertEqual(digest, self.digest)
        wrong = json.loads(self.blob)
        wrong['runAttempt'] = 1
        self.blob = json.dumps(wrong, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        # Manifest descriptor still points at the old bytes, so layer integrity fails closed.
        with self.assertRaises(RuntimeError):
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, 'reader', 'token', self.fetch())

    def test_run_log_must_bind_the_resolved_registry_digest_exactly(self):
        CACHE.verify_logged_digest(f'GHCR_RECEIPT_DIGEST={self.digest}\n', 'GHCR_RECEIPT_DIGEST', self.digest)
        with self.assertRaisesRegex(RuntimeError, 'not present'):
            CACHE.verify_logged_digest(f'GHCR_RECEIPT_DIGEST=sha256:{"f" * 64}\n',
                                       'GHCR_RECEIPT_DIGEST', self.digest)

    def test_missing_credentials_or_wrong_candidate_block(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid exact Prepare identity'):
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, '', 'token', self.fetch())
        with self.assertRaisesRegex(RuntimeError, 'Invalid exact Prepare identity'):
            CACHE.resolve_receipt('A' * 40, self.run_id, self.attempt, 'reader', 'token', self.fetch())

    def test_http_401_and_403_keep_safe_status_category_and_precise_auth_stage(self):
        challenge = {'WWW-Authenticate': f'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:{CACHE.RECEIPT_REPOSITORY}:pull"'}
        for status, category in ((401, 'AUTH'), (403, 'PERMISSION')):
            def denied(req, timeout=30, status=status):
                if req.full_url.startswith('https://ghcr.io/v2/'):
                    raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', challenge, __import__('io').BytesIO())
                raise urllib.error.HTTPError(req.full_url, status, 'Denied private detail', {}, __import__('io').BytesIO())
            with mock.patch.object(urllib.request, 'urlopen', denied), \
                    mock.patch.object(CACHE, 'retry_network_read', side_effect=lambda operation, *a, **k: operation()):
                with self.assertRaises(CACHE.NetworkStageError) as raised:
                    CACHE.ghcr_request(self.tag_url, 'reader', 'secret-token', CACHE.OCI_MANIFEST,
                                       'ghcr_receipt_tag_manifest')
            self.assertEqual((raised.exception.stage, raised.exception.category, raised.exception.http_status),
                             ('ghcr_receipt_tag_manifest_auth_token', category, status))
            self.assertEqual(raised.exception.error_type, 'HTTPError')

    def test_network_failure_is_classified_without_exception_text(self):
        def offline(*args, **kwargs):
            raise urllib.error.URLError(socket.gaierror('private-hostname.example'))
        with mock.patch.object(urllib.request, 'urlopen', offline), \
                mock.patch.object(CACHE, 'retry_network_read', side_effect=lambda operation, *a, **k: operation()):
            with self.assertRaises(CACHE.NetworkStageError) as raised:
                CACHE.ghcr_request(self.tag_url, 'reader', 'secret-token', CACHE.OCI_MANIFEST,
                                   'ghcr_receipt_tag_manifest')
        self.assertEqual(raised.exception.stage, 'ghcr_receipt_tag_manifest')
        self.assertEqual(raised.exception.category, 'NETWORK')
        self.assertIsNone(raised.exception.http_status)
        self.assertNotIn('private-hostname', str(raised.exception))

    def test_missing_receipt_preserves_http_status_and_stage(self):
        def missing(url, _username, _token, _accept, stage, **_kwargs):
            raise CACHE.NetworkStageError(stage, 'HTTPError', 'HTTP', 404)
        with self.assertRaises(CACHE.NetworkStageError) as raised:
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, 'reader', 'token', missing)
        self.assertEqual((raised.exception.stage, raised.exception.http_status), ('ghcr_receipt_tag_manifest', 404))

    def test_malformed_publisher_logs_and_wrong_run_attempt_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'not present'):
            CACHE.verify_logged_digest('not a publisher digest', 'GHCR_RECEIPT_DIGEST', self.digest)
        wrong_attempt = json.loads(self.blob)
        wrong_attempt['runAttempt'] = 1
        self.blob = json.dumps(wrong_attempt, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        with self.assertRaises(RuntimeError):
            CACHE.resolve_receipt(self.sha, self.run_id, self.attempt, 'reader', 'token', self.fetch())

    def test_missing_or_wrong_run_and_attempt_identity_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid exact Prepare identity'):
            CACHE.resolve_receipt(self.sha, self.run_id, '', 'reader', 'token', self.fetch())
        for key, replacement in (('runId', 124), ('runAttempt', 1)):
            receipt = {'schema':3,'transport':'ghcr-oci','status':'READY','candidateSha':self.sha,
                       'runId':123,'runAttempt':2,'gates':{'unit':'PASS'}}
            receipt[key] = replacement
            blob = json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode() + b'\n'
            layer = 'sha256:' + hashlib.sha256(blob).hexdigest()
            manifest = json.dumps({'schemaVersion':2,'mediaType':CACHE.OCI_MANIFEST,
                'artifactType':CACHE.RECEIPT_MEDIA_TYPE,'layers':[{'mediaType':CACHE.RECEIPT_LAYER_MEDIA_TYPE,
                'digest':layer,'size':len(blob)}]},sort_keys=True,separators=(',',':')).encode()
            digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
            urls = {self.tag_url:(manifest,{'Docker-Content-Digest':digest}),
                    f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/{digest}':(manifest,{'Docker-Content-Digest':digest}),
                    f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/blobs/{layer}':(blob,{})}
            def fetch(url,*_args,**_kwargs): return urls[url]
            with self.assertRaisesRegex(RuntimeError, 'source or Prepare identity mismatch'):
                CACHE.resolve_receipt(self.sha,self.run_id,self.attempt,'reader','token',fetch)

    def test_structured_diagnostic_redacts_secrets_and_keeps_identity_and_completion_flag(self):
        CACHE.DIAGNOSTIC_IDENTITY = {'candidateSha': self.sha, 'runId': self.run_id, 'runAttempt': self.attempt}
        CACHE.GHCR_READBACK_COMPLETED = True
        error = CACHE.NetworkStageError('ghcr_receipt_tag_manifest_auth_token', 'HTTPError', 'PERMISSION', 403)
        with mock.patch('sys.stdout') as stdout:
            CACHE.emit_blocked_diagnostic(error)
            output = ''.join(call.args[0] for call in stdout.write.call_args_list)
        payload = json.loads(next(line.split('=', 1)[1] for line in output.splitlines()
                                  if line.startswith('ARTIFACT_CACHE_DIAGNOSTIC=')))
        self.assertEqual(payload, {'schema': 1, 'stage': 'ghcr_receipt_tag_manifest_auth_token',
            'errorType': 'HTTPError', 'category': 'PERMISSION', 'httpStatus': 403,
            'candidateSha': self.sha, 'runId': self.run_id, 'runAttempt': self.attempt,
            'ghcrReadbackCompleted': True})
        self.assertIn('ARTIFACT_CACHE=BLOCKED STAGE=ghcr_receipt ERROR_TYPE=HTTPError', output)
        self.assertNotRegex(output, r'secret-token|Authorization:|Bearer |https?://|Denied private detail')
        with tempfile.TemporaryDirectory(prefix='prepared-cache-summary-') as directory:
            summary = Path(directory) / 'summary.md'
            with mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}):
                with mock.patch('sys.stdout'):
                    CACHE.emit_blocked_diagnostic(error)
            summary_output = summary.read_text()
            self.assertIn('"stage": "ghcr_receipt_tag_manifest_auth_token"', summary_output)
            self.assertIn('"httpStatus": 403', summary_output)
            self.assertNotRegex(summary_output, r'secret-token|Authorization:|Bearer |https?://')
        with mock.patch('sys.stdout') as stdout:
            CACHE.emit_blocked_diagnostic(RuntimeError('Bearer user-token https://user:pass@example.invalid/path'))
            generic_output = ''.join(call.args[0] for call in stdout.write.call_args_list)
        self.assertNotRegex(generic_output, r'user-token|user:pass|example\.invalid')

    def test_diagnostic_maps_only_known_oci_validation_errors_to_safe_reason_codes(self):
        CACHE.set_diagnostic_stage('ghcr_artifact_digest_authenticated_read')
        known = CACHE.diagnostic_payload(RuntimeError('GHCR artifact manifest digest mismatch'))
        self.assertEqual(known['reasonCode'], 'OCI_MANIFEST_DIGEST_MISMATCH')
        self.assertNotIn('GHCR artifact manifest digest mismatch', json.dumps(known))
        unknown = CACHE.diagnostic_payload(RuntimeError('Bearer private-token https://secret.example'))
        self.assertNotIn('reasonCode', unknown)
        self.assertNotIn('private-token', json.dumps(unknown))

    def test_real_cli_entrypoint_successfully_reads_fixture_receipt_and_writes_verified_evidence(self):
        # This exercises the actual CLI with a deterministic local HTTP fixture. It is
        # logic coverage only; it does not attest live GHCR access or production parity.
        sitecustomize = r'''import hashlib, io, json, os, urllib.parse, urllib.request, zipfile
from email.message import Message
class Response(io.BytesIO):
 def __init__(self, body, headers=None):
  super().__init__(body); self.headers=Message()
  for key, value in (headers or {}).items(): self.headers[key]=value
 def getcode(self): return 200
sha=os.environ['CANDIDATE_SHA']; run=os.environ['PREPARE_RUN_ID']; attempt=os.environ['PREPARE_RUN_ATTEMPT']
receipt={'schema':3,'transport':'ghcr-oci','status':'READY','candidateSha':sha,'runId':int(run),'runAttempt':int(attempt),'gates':{'unit':'PASS'}}
blob=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()+b'\n'; layer='sha256:'+hashlib.sha256(blob).hexdigest()
manifest=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','artifactType':'application/vnd.gpoint.prepare.receipt.v1','layers':[{'mediaType':'application/vnd.gpoint.prepare.receipt.v1+json','digest':layer,'size':len(blob)}]},sort_keys=True,separators=(',',':')).encode(); digest='sha256:'+hashlib.sha256(manifest).hexdigest()
def ziplogs():
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z: z.writestr('publish.log','GHCR_RECEIPT_DIGEST='+digest+'\n')
 return out.getvalue()
logs=ziplogs()
def urlopen(req,timeout=30):
 url=req.full_url; path=urllib.parse.urlparse(url).path
 if path.endswith('/actions/runs/'+run): return Response(json.dumps({'status':'completed','conclusion':'success','head_sha':sha,'head_branch':'main','event':'push','path':'.github/workflows/gpoint-prepare.yml','run_attempt':int(attempt)}).encode())
 if path.endswith('/git/ref/heads/main'): return Response(json.dumps({'object':{'sha':sha}}).encode())
 if path.endswith('/attempts/'+attempt+'/jobs'):
  return Response(json.dumps({'jobs':[{'id':999,'name':'prepare','run_attempt':int(attempt),'status':'completed','conclusion':'success','steps':[{'name':'Publish and read back exact GHCR artifacts','conclusion':'success'}]}]}).encode())
 if path.endswith('/actions/jobs/999/logs'): return Response(logs)
 if '/manifests/' in path:
  return Response(manifest,{'Docker-Content-Digest':digest})
 if path.endswith('/blobs/'+layer): return Response(blob)
 raise AssertionError('unexpected fixture route '+path)
urllib.request.urlopen=urlopen
'''
        with tempfile.TemporaryDirectory(prefix='prepared-cache-cli-') as directory:
            root = Path(directory)
            (root / 'sitecustomize.py').write_text(sitecustomize)
            destination = root / 'artifacts'
            env = {**os.environ, 'PYTHONPATH': str(root), 'CANDIDATE_SHA': self.sha,
                'PREPARE_RUN_ID': self.run_id, 'PREPARE_RUN_ATTEMPT': self.attempt,
                'GITHUB_TOKEN': 'fixture-github-secret', 'GHCR_READ_USERNAME': 'fixture-reader',
                'GHCR_READ_TOKEN': 'fixture-ghcr-secret', 'RELEASE_MODE': 'preflight',
                'ARTIFACT_DESTINATION': str(destination)}
            result = subprocess.run([sys.executable, str(SCRIPT)], cwd=SCRIPT.parents[1], env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('ARTIFACT_CACHE=GHCR_READBACK_OK', result.stdout)
            self.assertEqual((destination / 'receipt.json').read_bytes(), self.blob)
            evidence = json.loads((destination / 'ghcr-evidence.json').read_text())
            self.assertEqual(evidence['candidateSha'], self.sha)
            self.assertEqual(evidence['runId'], int(self.run_id))
            self.assertEqual(evidence['runAttempt'], int(self.attempt))
            self.assertEqual(evidence['manifestDigest'], self.digest)
            self.assertNotRegex(result.stdout, r'fixture-(?:github|ghcr)-secret|Authorization:|Bearer ')

    def test_real_cli_entrypoint_reports_ghcr_403_and_controller_keeps_fail_closed_mapping(self):
        sitecustomize = r'''import io, json, os, urllib.error, urllib.parse, urllib.request
from email.message import Message
class Response(io.BytesIO):
 def __init__(self, body): super().__init__(body); self.headers=Message()
def urlopen(req,timeout=30):
 path=urllib.parse.urlparse(req.full_url).path; sha=os.environ['CANDIDATE_SHA']; run=os.environ['PREPARE_RUN_ID']; attempt=os.environ['PREPARE_RUN_ATTEMPT']
 if path.endswith('/actions/runs/'+run): return Response(json.dumps({'status':'completed','conclusion':'success','head_sha':sha,'head_branch':'main','event':'push','path':'.github/workflows/gpoint-prepare.yml','run_attempt':int(attempt)}).encode())
 if path.endswith('/git/ref/heads/main'): return Response(json.dumps({'object':{'sha':sha}}).encode())
 if '/manifests/' in path: raise urllib.error.HTTPError(req.full_url,403,'Private detail must not escape',{},io.BytesIO())
 raise AssertionError('unexpected fixture route '+path)
urllib.request.urlopen=urlopen
'''
        with tempfile.TemporaryDirectory(prefix='prepared-cache-cli-denied-') as directory:
            root = Path(directory)
            (root / 'sitecustomize.py').write_text(sitecustomize)
            env = {**os.environ, 'PYTHONPATH': str(root), 'CANDIDATE_SHA': self.sha,
                'PREPARE_RUN_ID': self.run_id, 'PREPARE_RUN_ATTEMPT': self.attempt,
                'GITHUB_TOKEN': 'fixture-github-secret', 'GHCR_READ_USERNAME': 'fixture-reader',
                'GHCR_READ_TOKEN': 'fixture-ghcr-secret', 'RELEASE_MODE': 'preflight',
                'ARTIFACT_DESTINATION': str(root / 'artifacts')}
            result = subprocess.run([sys.executable, str(SCRIPT)], cwd=SCRIPT.parents[1], env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            line = next(line for line in result.stdout.splitlines() if line.startswith('ARTIFACT_CACHE_DIAGNOSTIC='))
            diagnostic = json.loads(line.split('=', 1)[1])
            self.assertEqual((diagnostic['stage'], diagnostic['category'], diagnostic['httpStatus']),
                             ('ghcr_receipt_tag_manifest', 'PERMISSION', 403))
            self.assertEqual((diagnostic['runId'], diagnostic['runAttempt']), (self.run_id, self.attempt))
            self.assertFalse(diagnostic['ghcrReadbackCompleted'])
            self.assertIn('ARTIFACT_CACHE=BLOCKED STAGE=ghcr_receipt ERROR_TYPE=HTTPError', result.stdout)
            self.assertNotRegex(result.stdout, r'fixture-(?:github|ghcr)-secret|Private detail|Authorization:|Bearer ')
            mapped = subprocess.run(['node', '--input-type=module', '-e',
                "import {preparedReceiptFailureDiagnostic as f} from './scripts/release-orchestrator.mjs'; console.log(JSON.stringify(f(process.argv[1])))",
                result.stdout], cwd=SCRIPT.parents[1], text=True, capture_output=True, timeout=15)
            self.assertEqual(mapped.returncode, 0, mapped.stderr)
            self.assertEqual(json.loads(mapped.stdout), {'stage':'prepare_receipt_readback',
                'substage':'ghcr_receipt_tag_manifest','resource':'ghcr_prepare_receipt',
                'code':'GHCR_READ_FAILED','candidateSha':self.sha,'runId':self.run_id,
                'runAttempt':self.attempt,'category':'PERMISSION','errorType':'HTTPError',
                'httpStatus':403,'ghcrReadbackCompleted':False})


if __name__ == '__main__':
    unittest.main()
