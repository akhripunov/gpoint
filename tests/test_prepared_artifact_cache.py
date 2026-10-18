import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'prepared_artifact_cache.py'
spec = importlib.util.spec_from_file_location('prepared_artifact_cache', SCRIPT)
CACHE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CACHE)


class GhcrReceiptTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == '__main__':
    unittest.main()
