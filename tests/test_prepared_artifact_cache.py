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
import urllib.error
import urllib.request

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepared_artifact_cache.py'
spec = importlib.util.spec_from_file_location('prepared_artifact_cache', SCRIPT)
CACHE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CACHE)


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
            'artifactType': CACHE.RECEIPT_MEDIA_TYPE, 'layers': [{'mediaType': CACHE.RECEIPT_MEDIA_TYPE,
            'digest': layer_digest, 'size': len(self.blob)}]}, sort_keys=True, separators=(',', ':')).encode()
        self.digest = 'sha256:' + hashlib.sha256(self.manifest).hexdigest()
        self.tag_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/prepare-{self.sha}-{self.run_id}-{self.attempt}'
        self.digest_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/manifests/{self.digest}'
        self.blob_url = f'https://ghcr.io/v2/{CACHE.RECEIPT_REPOSITORY}/blobs/{layer_digest}'
        self.manifest_header = {'Docker-Content-Digest': self.digest}

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
                'artifactType':CACHE.RECEIPT_MEDIA_TYPE,'layers':[{'mediaType':CACHE.RECEIPT_MEDIA_TYPE,
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
manifest=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','artifactType':'application/vnd.gpoint.prepare.receipt.v1','layers':[{'mediaType':'application/vnd.gpoint.prepare.receipt.v1','digest':layer,'size':len(blob)}]},sort_keys=True,separators=(',',':')).encode(); digest='sha256:'+hashlib.sha256(manifest).hexdigest()
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
                'substage':'ghcr_receipt','resource':'ghcr_prepare_receipt','code':'GHCR_READ_FAILED'})


if __name__ == '__main__':
    unittest.main()
