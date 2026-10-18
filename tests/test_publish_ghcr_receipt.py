"""Local-only tests for exact GHCR image and receipt publication metadata."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'publish_ghcr_images.py'
SPEC = importlib.util.spec_from_file_location('publish_ghcr_images', SCRIPT)
PUBLISHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISHER)

SHA = 'a' * 40
RUN = '123456'
ATTEMPT = '2'
DIGEST = 'sha256:' + 'b' * 64


def ready_receipt():
    return {
        'schema': 1, 'status': 'READY', 'candidateSha': SHA,
        'runId': int(RUN), 'runAttempt': int(ATTEMPT),
        'gates': {'unit': 'PASS', 'postgres': 'PASS'},
        'images': {}, 'archiveSha256': 'f' * 64,
    }


class GhcrReceiptPublisherTests(unittest.TestCase):
    def test_receipt_body_has_no_self_referential_transport_digest(self):
        images = {role: {'sourceSha': SHA, 'platform': 'linux/amd64'}
                  for role in PUBLISHER.IMAGES}
        result = PUBLISHER.augment_receipt(ready_receipt(), images, SHA, RUN, ATTEMPT)
        self.assertEqual(result['schema'], 3)
        self.assertEqual(result['transport'], 'ghcr-oci')
        self.assertNotIn('archiveSha256', result)
        self.assertNotIn('receiptDigest', result)
        self.assertNotIn('receiptSha256', result)

    def test_each_published_image_is_pulled_by_digest_and_config_matches_build(self):
        config = 'sha256:' + 'c' * 64
        manifest = DIGEST
        metadata = {'Id': config, 'Os': 'linux', 'Architecture': 'amd64',
                    'Config': {'Labels': {
                        'org.opencontainers.image.revision': SHA,
                        'org.opencontainers.image.source': PUBLISHER.SOURCE_URL,
                    }}}
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[:3] == ('docker', 'image', 'inspect'):
                return json.dumps([metadata])
            if args[:2] == ('docker', 'push'):
                return 'pushed digest: ' + manifest
            return ''

        with patch.object(PUBLISHER, 'run', side_effect=fake_run):
            records = PUBLISHER.publish_images(SHA, RUN, ATTEMPT)
        self.assertEqual(set(records), set(PUBLISHER.IMAGES))
        self.assertTrue(all(item['configDigest'] == config for item in records.values()))
        self.assertEqual(sum(1 for call in calls if call[:2] == ('docker', 'pull')), 3)
        self.assertTrue(all(any(f"{PUBLISHER.IMAGES[role][1]}@{manifest}" in call
                                for call in calls if call[:2] == ('docker', 'pull'))
                            for role in PUBLISHER.IMAGES))

    def test_published_image_readback_config_must_match_local_build(self):
        local = {'Id': 'sha256:' + 'c' * 64, 'Os': 'linux', 'Architecture': 'amd64',
                 'Config': {'Labels': {'org.opencontainers.image.revision': SHA,
                                       'org.opencontainers.image.source': PUBLISHER.SOURCE_URL}}}
        pulled = {**local, 'Id': 'sha256:' + 'd' * 64}
        inspect_count = 0

        def fake_run(*args, **kwargs):
            nonlocal inspect_count
            if args[:3] == ('docker', 'image', 'inspect'):
                inspect_count += 1
                return json.dumps([local if inspect_count == 1 else pulled])
            fake_run.calls.append(args)
            if args[:2] == ('docker', 'push'):
                return 'digest: ' + DIGEST
            return ''
        fake_run.calls = []

        with patch.object(PUBLISHER, 'run', side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, 'config differs from built app image'):
                PUBLISHER.publish_images(SHA, RUN, ATTEMPT)

    def test_publish_pushes_one_receipt_then_reads_back_by_manifest_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            raw = (json.dumps(ready_receipt(), sort_keys=True) + '\n').encode()
            path.write_bytes(raw)
            process_cwd = os.getcwd()

            def fake_run(*args, **kwargs):
                if args[:2] == ('oras', 'push'):
                    self.assertTrue(path.is_absolute())
                    self.assertEqual(kwargs.get('cwd'), path.parent.resolve())
                    self.assertIn(f'{PUBLISHER.RECEIPT_REPOSITORY}:prepare-{SHA}-{RUN}-{ATTEMPT}', args)
                    self.assertIn('--artifact-type', args)
                    self.assertIn('receipt.json:application/vnd.gpoint.prepare.receipt.v1+json', args)
                    self.assertNotIn(str(path), args)
                    self.assertNotIn('--disable-path-validation', args)
                    return json.dumps({'digest': DIGEST,
                                       'reference': f'{PUBLISHER.RECEIPT_REPOSITORY}@{DIGEST}'})
                self.assertEqual(args[:4], ('oras', 'pull',
                                            f'{PUBLISHER.RECEIPT_REPOSITORY}@{DIGEST}',
                                            '--output'))
                output = Path(args[-1])
                (output / path.name).write_bytes(raw)
                return ''

            with patch.object(PUBLISHER, 'run', side_effect=fake_run):
                result = PUBLISHER.publish_receipt(path, SHA, RUN, ATTEMPT)

        self.assertEqual(result['digest'], DIGEST)
        self.assertEqual(result['sourceSha'], SHA)
        self.assertEqual(result['runId'], int(RUN))
        self.assertEqual(result['runAttempt'], int(ATTEMPT))
        self.assertEqual(result['contentSha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(os.getcwd(), process_cwd)

    def test_run_passes_cwd_to_subprocess_without_changing_process_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            process_cwd = os.getcwd()
            with patch.object(PUBLISHER.subprocess, 'run') as execute:
                execute.return_value.stdout = 'ok'
                execute.return_value.stderr = ''
                self.assertEqual(PUBLISHER.run('oras', 'version', capture=True, cwd=Path(folder)), 'ok')
                self.assertEqual(execute.call_args.kwargs['cwd'], Path(folder))
            self.assertEqual(os.getcwd(), process_cwd)

    def test_receipt_rejects_relative_traversal_and_symlink_paths_before_oras(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / 'outside'
            outside.mkdir()
            receipt = outside / 'receipt.json'
            receipt.write_text(json.dumps(ready_receipt()) + '\n')
            traversal = root / 'nested' / '..' / 'outside' / 'receipt.json'
            symlink = root / 'alias' / 'receipt.json'
            symlink.parent.mkdir()
            symlink.symlink_to(receipt)
            with patch.object(PUBLISHER, 'run') as runner:
                for unsafe in (Path('receipt.json'), traversal, symlink):
                    with self.subTest(path=unsafe), self.assertRaisesRegex(ValueError, 'unsafe OCI artifact path'):
                        PUBLISHER.publish_receipt(unsafe, SHA, RUN, ATTEMPT)
                runner.assert_not_called()

    def test_receipt_oras_push_failure_stops_before_digest_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            path.write_text(json.dumps(ready_receipt()) + '\n')
            with patch.object(PUBLISHER, 'run', side_effect=subprocess.CalledProcessError(1, 'oras')) as runner:
                with self.assertRaises(subprocess.CalledProcessError):
                    PUBLISHER.publish_receipt(path, SHA, RUN, ATTEMPT)
            runner.assert_called_once()

    def test_readback_tampering_or_bad_descriptor_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            path.write_text(json.dumps(ready_receipt()) + '\n')

            def tampered(*args, **kwargs):
                if args[:2] == ('oras', 'push'):
                    return json.dumps({'digest': DIGEST})
                Path(args[-1], path.name).write_text('tampered')
                return ''

            with patch.object(PUBLISHER, 'run', side_effect=tampered):
                with self.assertRaisesRegex(ValueError, 'readback content mismatch'):
                    PUBLISHER.publish_receipt(path, SHA, RUN, ATTEMPT)

            with patch.object(PUBLISHER, 'run', return_value='{}'):
                with self.assertRaisesRegex(ValueError, 'exact receipt manifest digest'):
                    PUBLISHER.publish_receipt(path, SHA, RUN, ATTEMPT)

    def test_wrong_prepare_identity_is_rejected_before_registry_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            path.write_text(json.dumps(ready_receipt()) + '\n')
            with patch.object(PUBLISHER, 'run') as runner:
                with self.assertRaisesRegex(ValueError, 'does not bind exact Prepare identity'):
                    PUBLISHER.publish_receipt(path, 'c' * 40, RUN, ATTEMPT)
                runner.assert_not_called()

    def test_v4_static_artifact_is_separate_oci_object_and_checked_by_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'miniapp.zip'
            raw = b'bounded static package'
            path.write_bytes(raw)
            process_cwd = os.getcwd()
            receipt = {'staticArtifactName': 'miniapp.zip',
                       'staticArtifactRoute': 'V4_FAST_STATIC',
                       'staticArtifactSha256': hashlib.sha256(raw).hexdigest()}

            def fake_run(*args, **kwargs):
                if args[:2] == ('oras', 'push'):
                    self.assertTrue(path.is_absolute())
                    self.assertEqual(kwargs.get('cwd'), path.parent.resolve())
                    self.assertTrue(any(str(item).startswith(PUBLISHER.STATIC_REPOSITORY + ':')
                                        for item in args))
                    self.assertIn('miniapp.zip:application/vnd.gpoint.miniapp.static.v1+zip', args)
                    self.assertNotIn(str(path), args)
                    self.assertNotIn('--disable-path-validation', args)
                    return json.dumps({'digest': DIGEST})
                self.assertEqual(args[:3], ('oras', 'pull', f'{PUBLISHER.STATIC_REPOSITORY}@{DIGEST}'))
                Path(args[-1], path.name).write_bytes(raw)
                return ''

            with patch.object(PUBLISHER, 'run', side_effect=fake_run):
                result = PUBLISHER.publish_static_artifact(path, receipt, SHA, RUN, ATTEMPT)
            self.assertEqual(result['digest'], DIGEST)
            self.assertEqual(result['contentSha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(os.getcwd(), process_cwd)

    def test_static_artifact_rejects_unsafe_paths_and_readback_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expected = root / 'miniapp.zip'
            raw = b'miniapp bytes'
            expected.write_bytes(raw)
            process_cwd = os.getcwd()
            receipt = {'staticArtifactName': 'miniapp.zip',
                       'staticArtifactRoute': 'V4_FAST_STATIC',
                       'staticArtifactSha256': hashlib.sha256(raw).hexdigest()}
            with patch.object(PUBLISHER, 'run') as runner:
                with self.assertRaisesRegex(ValueError, 'unsafe OCI artifact path'):
                    PUBLISHER.publish_static_artifact(root / '..' / root.name / 'miniapp.zip', receipt, SHA, RUN, ATTEMPT)
                runner.assert_not_called()

            def tampered(*args, **kwargs):
                if args[:2] == ('oras', 'push'):
                    self.assertEqual(kwargs.get('cwd'), root.resolve())
                    return json.dumps({'digest': DIGEST})
                Path(args[-1], 'miniapp.zip').write_bytes(b'changed bytes')
                return ''

            with patch.object(PUBLISHER, 'run', side_effect=tampered):
                with self.assertRaisesRegex(ValueError, 'readback content mismatch'):
                    PUBLISHER.publish_static_artifact(expected, receipt, SHA, RUN, ATTEMPT)
            self.assertEqual(os.getcwd(), process_cwd)

    def test_static_artifact_oras_failure_stops_without_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'miniapp.zip'
            raw = b'miniapp bytes'
            path.write_bytes(raw)
            receipt = {'staticArtifactName': 'miniapp.zip',
                       'staticArtifactRoute': 'V4_FAST_STATIC',
                       'staticArtifactSha256': hashlib.sha256(raw).hexdigest()}
            error = subprocess.CalledProcessError(1, 'oras')
            with patch.object(PUBLISHER, 'run', side_effect=error) as runner:
                with self.assertRaises(subprocess.CalledProcessError):
                    PUBLISHER.publish_static_artifact(path, receipt, SHA, RUN, ATTEMPT)
            runner.assert_called_once()

    def test_static_receipt_requires_static_artifact_and_no_docker_images(self):
        receipt = {'schema': 2, 'route': 'STATIC_UI_ONLY', 'status': 'READY',
                   'candidateSha': SHA, 'runId': int(RUN), 'runAttempt': int(ATTEMPT),
                   'gates': {'miniappBuild': 'PASS'}}
        static = {'repository': PUBLISHER.STATIC_REPOSITORY, 'digest': DIGEST,
                  'contentSha256': 'c' * 64, 'sourceSha': SHA,
                  'runId': int(RUN), 'runAttempt': int(ATTEMPT)}
        result = PUBLISHER.augment_receipt(receipt, {}, SHA, RUN, ATTEMPT, static)
        self.assertEqual(result['staticArtifact'], static)
        with self.assertRaisesRegex(ValueError, 'must publish only'):
            PUBLISHER.augment_receipt(receipt, {'app': {}}, SHA, RUN, ATTEMPT, static)


if __name__ == '__main__':
    unittest.main()
