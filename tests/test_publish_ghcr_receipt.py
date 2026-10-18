"""Local-only tests for exact GHCR image and receipt publication metadata."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


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
        'gates': {'unit': 'PASS', 'postgres': 'PASS', 'deployRehearsal': 'PASS'},
        'images': {}, 'archiveSha256': 'f' * 64,
    }


class GhcrReceiptPublisherTests(unittest.TestCase):
    def test_traceback_timeout_argument_is_not_a_network_timeout(self):
        failure = subprocess.CalledProcessError(1, ['node', 'helper.mjs'], stderr=(
            'GHCR_REHEARSAL_SUBSTAGE=capture_loaded_images\n'
            '  return subprocess.check_output(args, timeout=30)\n'
            "subprocess.CalledProcessError: Command '['ctr', '-n', 'moby', 'content', 'get', 'private-digest']' returned non-zero exit status 1.\n"
            'private-token=secret-value\n'))
        diagnostic = PUBLISHER.safe_process_failure(
            'ghcr_rehearsal_runtime_parity', 'rehearse', None, failure)
        self.assertEqual(diagnostic['category'], 'PROCESS_EXIT')
        self.assertEqual(diagnostic['childCommand'], 'ctr')
        self.assertEqual(diagnostic['childFailure'], 'CHILD_EXIT')
        self.assertNotIn('reasonCode', diagnostic)
        self.assertNotIn('private-digest', json.dumps(diagnostic))
        self.assertNotIn('secret-value', json.dumps(diagnostic))

    def test_real_child_timeout_is_retained_despite_traceback_source(self):
        failure = subprocess.CalledProcessError(1, ['node', 'helper.mjs'], stderr=(
            'GHCR_REHEARSAL_SUBSTAGE=capture_loaded_images\n'
            '  return subprocess.check_output(args, timeout=30)\n'
            "subprocess.TimeoutExpired: Command '['docker', 'image', 'inspect', 'private-tag']' timed out after 30 seconds\n"))
        diagnostic = PUBLISHER.safe_process_failure(
            'ghcr_rehearsal_runtime_parity', 'rehearse', None, failure)
        self.assertEqual(diagnostic['category'], 'NETWORK')
        self.assertEqual(diagnostic['reasonCode'], 'TIMEOUT')
        self.assertEqual(diagnostic['childCommand'], 'docker')
        self.assertEqual(diagnostic['childFailure'], 'CHILD_TIMEOUT')

    def test_missing_runtime_library_is_tooling_without_private_output(self):
        failure = subprocess.CalledProcessError(1, ['node', 'helper.mjs'], stderr=(
            'GHCR_REHEARSAL_SUBSTAGE=capture_loaded_images\n'
            'python3: error while loading shared libraries: libpython3.12.so.1.0: '
            'cannot open shared object file: private-path secret-value\n'))
        diagnostic = PUBLISHER.safe_process_failure(
            'ghcr_rehearsal_runtime_parity', 'rehearse', None, failure)
        self.assertEqual(diagnostic['category'], 'TOOLING')
        self.assertEqual(diagnostic['childFailure'], 'PYTHON_LIBRARY_LOAD')
        self.assertNotIn('secret-value', json.dumps(diagnostic))

    def test_local_containerd_connection_is_named_without_socket_or_digest(self):
        failure = subprocess.CalledProcessError(1, ['node', 'helper.mjs'], stderr=(
            'GHCR_REHEARSAL_SUBSTAGE=capture_loaded_images\n'
            'ctr: failed to dial /private/socket: context deadline exceeded\n'
            "subprocess.CalledProcessError: Command '['ctr', 'content', 'get', 'private-digest']' returned non-zero exit status 1.\n"))
        diagnostic = PUBLISHER.safe_process_failure(
            'ghcr_rehearsal_runtime_parity', 'rehearse', None, failure)
        self.assertEqual(diagnostic['localReason'], 'CONTAINERD_SOCKET_CONNECT')
        self.assertEqual(diagnostic['childCommand'], 'ctr')
        self.assertNotIn('/private/socket', json.dumps(diagnostic))
        self.assertNotIn('private-digest', json.dumps(diagnostic))

    def test_subprocess_failure_diagnostic_names_operation_without_raw_registry_details(self):
        failure = subprocess.CalledProcessError(
            1, ['docker', 'pull', 'ghcr.io/akhripunov/private-package@sha256:' + 'a' * 64],
            stderr='unauthorized: bearer token secret-value https://ghcr.io/v2/private-package',
        )

        with patch.object(PUBLISHER.subprocess, 'run', side_effect=failure):
            with self.assertRaises(PUBLISHER.SafeProcessFailure) as caught:
                PUBLISHER.run('docker', 'pull', 'ghcr.io/private-package@sha256:' + 'a' * 64,
                              stage='ghcr_image_readback', operation='pull', role='app')

        self.assertEqual(caught.exception.diagnostic, {
            'stage': 'ghcr_image_readback', 'operation': 'pull', 'role': 'app',
            'category': 'AUTH', 'exitCode': 1,
        })
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertNotIn('private-package', str(caught.exception))

    def test_subprocess_timeout_diagnostic_preserves_stage_without_command_or_output(self):
        failure = subprocess.TimeoutExpired(
            ['docker', 'pull', 'ghcr.io/akhripunov/private-package@sha256:' + 'a' * 64],
            timeout=900, stderr='request timed out with token secret-value',
        )

        with patch.object(PUBLISHER.subprocess, 'run', side_effect=failure):
            with self.assertRaises(PUBLISHER.SafeProcessFailure) as caught:
                PUBLISHER.run('docker', 'pull', 'ghcr.io/private-package@sha256:' + 'a' * 64,
                              stage='ghcr_image_readback', operation='pull', role='app')

        self.assertEqual(caught.exception.diagnostic, {
            'stage': 'ghcr_image_readback', 'operation': 'pull', 'role': 'app',
            'category': 'TIMEOUT', 'exitCode': None,
        })
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertNotIn('private-package', str(caught.exception))

    def test_runtime_parity_diagnostic_preserves_allowlisted_substage_and_network_reason(self):
        failure = subprocess.CalledProcessError(
            1, ['node', 'rehearse-release-bundle.mjs', 'private-proof.json'],
            stderr='GHCR_REHEARSAL_SUBSTAGE=runtime_inspect_app\nconnect ECONNRESET token=secret-value',
        )

        with patch.object(PUBLISHER.subprocess, 'run', side_effect=failure):
            with self.assertRaises(PUBLISHER.SafeProcessFailure) as caught:
                PUBLISHER.run('node', 'rehearse-release-bundle.mjs', 'proof.json', SHA, '/tmp/private',
                              stage='ghcr_rehearsal_runtime_parity', operation='rehearse')

        self.assertEqual(caught.exception.diagnostic, {
            'stage': 'ghcr_rehearsal_runtime_parity', 'operation': 'rehearse',
            'role': None, 'category': 'NETWORK', 'exitCode': 1,
            'substage': 'runtime_inspect_app', 'reasonCode': 'CONNECTION_RESET',
        })

    def test_runtime_parity_timeout_preserves_safe_substage_without_raw_output(self):
        failure = subprocess.TimeoutExpired(
            ['node', 'rehearse-release-bundle.mjs', 'private-proof.json'],
            timeout=900, stderr=b'GHCR_REHEARSAL_SUBSTAGE=runtime_inspect_app\nrequest timed out token=secret-value',
        )

        with patch.object(PUBLISHER.subprocess, 'run', side_effect=failure):
            with self.assertRaises(PUBLISHER.SafeProcessFailure) as caught:
                PUBLISHER.run('node', 'rehearse-release-bundle.mjs', 'proof.json', SHA, '/tmp/private',
                              stage='ghcr_rehearsal_runtime_parity', operation='rehearse')

        self.assertEqual(caught.exception.diagnostic, {
            'stage': 'ghcr_rehearsal_runtime_parity', 'operation': 'rehearse',
            'role': None, 'category': 'TIMEOUT', 'exitCode': None,
            'substage': 'runtime_inspect_app', 'reasonCode': 'TIMEOUT',
        })
        self.assertNotIn('secret-value', str(caught.exception))
        self.assertNotIn('private-proof', str(caught.exception))

    def test_runtime_parity_retries_once_only_for_network_failures(self):
        transient = PUBLISHER.SafeProcessFailure({
            'stage': 'ghcr_rehearsal_runtime_parity', 'operation': 'rehearse',
            'role': None, 'category': 'NETWORK', 'exitCode': 1,
            'substage': 'runtime_inspect_app', 'reasonCode': 'CONNECTION_RESET',
        })
        with patch.object(PUBLISHER, 'run', side_effect=[transient, 'ok']) as command, \
                patch.object(PUBLISHER.sys, 'stderr', new_callable=io.StringIO) as diagnostic:
            result = PUBLISHER.run_rehearsal_with_retry('node', 'helper.mjs', 'proof.json',
                                                       SHA, '/tmp/rehearsal', {})

        self.assertEqual(result, 'ok')
        self.assertEqual(command.call_count, 2)
        self.assertIn('GHCR_REHEARSAL_NETWORK_RETRY=1', diagnostic.getvalue())
        self.assertNotIn('private-package', diagnostic.getvalue())

    def test_runtime_parity_does_not_retry_non_network_failures(self):
        permanent = PUBLISHER.SafeProcessFailure({
            'stage': 'ghcr_rehearsal_runtime_parity', 'operation': 'rehearse',
            'role': None, 'category': 'PROCESS_EXIT', 'exitCode': 1,
        })
        with patch.object(PUBLISHER, 'run', side_effect=permanent) as command:
            with self.assertRaises(PUBLISHER.SafeProcessFailure):
                PUBLISHER.run_rehearsal_with_retry('node', 'helper.mjs', 'proof.json',
                                                   SHA, '/tmp/rehearsal', {})

        self.assertEqual(command.call_count, 1)

    def test_canonical_prepare_exposes_only_job_scoped_token_to_digest_rehearsal(self):
        workflow = yaml.safe_load((SCRIPT.parents[1] / '.github/workflows/gpoint-prepare.yml').read_text())
        job = workflow['jobs']['prepare']
        publish = next(step for step in job['steps']
                       if step.get('name') == 'Publish and read back exact GHCR artifacts')
        self.assertEqual(job['permissions']['packages'], 'write')
        self.assertEqual(publish['env']['GHCR_REHEARSAL_USERNAME'], '${{ github.actor }}')
        self.assertEqual(publish['env']['GHCR_REHEARSAL_TOKEN'], '${{ secrets.GITHUB_TOKEN }}')
        self.assertEqual(publish['run'], 'python3 scripts/publish_ghcr_images.py')

    def test_rehearsal_child_environment_excludes_credentials_but_keeps_runtime_path(self):
        source = {
            'PATH': '/usr/bin', 'GITHUB_TOKEN': 'secret-a',
            'GHCR_REHEARSAL_TOKEN': 'secret-b', 'GH_TOKEN': 'secret-c',
            'GHCR_READ_TOKEN': 'secret-d', 'GHCR_TOKEN': 'secret-e',
            'DOCKER_CONFIG': '/inherited/config',
        }

        child = PUBLISHER.rehearsal_child_environment(source)

        self.assertEqual(child['PATH'], '/usr/bin')
        for key in ('GITHUB_TOKEN', 'GHCR_REHEARSAL_TOKEN', 'GH_TOKEN', 'GHCR_READ_TOKEN', 'GHCR_TOKEN'):
            self.assertNotIn(key, child)
        self.assertNotIn('DOCKER_CONFIG', child)

    def test_rehearsal_helper_imports_and_uses_node_path_and_spawn_sync(self):
        helper = (SCRIPT.parent / 'rehearse-release-bundle.mjs').read_text()

        self.assertIn("import path from 'node:path';", helper)
        self.assertIn("import { execFileSync, spawnSync } from 'node:child_process';", helper)
        self.assertIn("rejected.status !== 42", helper)
        self.assertIn('GPOINT_CI_PARITY_IDENTITY_MISMATCH=', helper)
        self.assertIn('GHCR_REHEARSAL_SUBSTAGE=${name}', helper)

    def test_rehearsal_cleanup_failure_prevents_success_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'scratch'
            root.mkdir()
            log = tempfile.TemporaryFile()
            with patch.object(PUBLISHER.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'sudo rm')):
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    PUBLISHER.cleanup_rehearsal(root, None, log, 'sudo', {})
            self.assertTrue(root.exists())

    def test_rehearsal_cleanup_terminates_child_group_even_if_daemon_leader_exited(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'scratch'
            root.mkdir()
            log = tempfile.TemporaryFile()
            group_alive = {'value': True}

            def signal_group(_pid, signum):
                if signum == PUBLISHER.signal.SIGTERM:
                    return
                if signum == PUBLISHER.signal.SIGKILL:
                    group_alive['value'] = False
                    return
                if signum == 0 and group_alive['value']:
                    return
                if signum == 0:
                    raise ProcessLookupError
                raise AssertionError(f'unexpected signal {signum}')

            def remove_root(args, **_kwargs):
                PUBLISHER.shutil.rmtree(args[-1])
                return type('Result', (), {'returncode': 0})()

            daemon = type('Daemon', (), {'pid': 87654321, 'poll': lambda _self: 0})()
            with patch.object(PUBLISHER.os, 'killpg', side_effect=signal_group) as killpg, \
                 patch.object(PUBLISHER.subprocess, 'run', side_effect=remove_root):
                PUBLISHER.cleanup_rehearsal(root, daemon, log, 'sudo', {})

            self.assertTrue(any(call.args[1] == PUBLISHER.signal.SIGTERM for call in killpg.call_args_list))
            self.assertTrue(any(call.args[1] == PUBLISHER.signal.SIGKILL for call in killpg.call_args_list))
            self.assertFalse(group_alive['value'])
            self.assertFalse(root.exists())

    def test_rehearsal_passes_ghcr_token_only_as_login_stdin(self):
        records = {role: {'repository': repository, 'digest': DIGEST,
                          'configDigest': 'sha256:' + 'c' * 64,
                          'sourceSha': SHA, 'platform': 'linux/amd64'}
                   for role, (_, repository) in PUBLISHER.IMAGES.items()}
        manifest = DIGEST
        metadata = [{'Id': manifest, 'Os': 'linux', 'Architecture': 'amd64',
                     'Descriptor': {'digest': manifest,
                                    'mediaType': 'application/vnd.oci.image.manifest.v1+json'},
                     'Config': {'Labels': {
                         'org.opencontainers.image.revision': SHA,
                         'org.opencontainers.image.source': PUBLISHER.SOURCE_URL}}}]
        docker_info = {'ServerVersion': '29.1.3', 'Driver': 'overlayfs',
                       'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]}
        run_calls = []
        docker_wrapper = []

        def fake_run(*args, **kwargs):
            run_calls.append((args, kwargs))
            if args[:3] == ('docker', 'info', '--format'):
                return json.dumps(docker_info)
            if args[:3] == ('docker', 'image', 'inspect'):
                return json.dumps(metadata)
            if Path(args[0]).name == 'node':
                docker_wrapper.append((Path(args[-1]) / 'bin/docker').read_text())
            return ''

        def fake_subprocess_run(args, **kwargs):
            if len(args) > 2 and args[2] == 'rm':
                PUBLISHER.shutil.rmtree(args[-1])
            return type('Result', (), {'returncode': 0})()

        with patch.dict(os.environ, {
            'PATH': '/safe/bin', 'GITHUB_TOKEN': 'secret-github',
            'GHCR_REHEARSAL_TOKEN': 'secret-ghcr', 'GH_TOKEN': 'secret-cli',
            'DOCKER_CONFIG': '/untrusted/docker-config',
        }, clear=True), \
             patch.object(PUBLISHER.shutil, 'which', side_effect=lambda name: f'/safe/bin/{name}'), \
             patch.object(PUBLISHER.subprocess, 'Popen') as popen, \
             patch.object(PUBLISHER.subprocess, 'run', side_effect=fake_subprocess_run) as subprocess_run, \
             patch.object(PUBLISHER, 'run', side_effect=fake_run):
            popen.return_value.pid = 987654321
            popen.return_value.poll.side_effect = [None, 0, 0]
            popen.return_value.wait.return_value = 0
            result = PUBLISHER.rehearse_ghcr_images(records, SHA, 'actor', 'secret-ghcr')

        self.assertEqual(result['status'], 'PASS')
        child_envs = [popen.call_args.kwargs['env']]
        child_envs.extend(kwargs['env'] for _, kwargs in run_calls if 'env' in kwargs)
        child_envs.extend(call.kwargs['env'] for call in subprocess_run.call_args_list
                          if 'env' in call.kwargs)
        for env in child_envs:
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn('GHCR_REHEARSAL_TOKEN', env)
            self.assertNotIn('GH_TOKEN', env)
            self.assertNotEqual(env.get('DOCKER_CONFIG'), '/untrusted/docker-config')
            if 'DOCKER_CONFIG' in env:
                self.assertIn('gpoint-ghcr-containerd-rehearsal-', env['DOCKER_CONFIG'])
        login = next(kwargs for args, kwargs in run_calls if args[:2] == ('docker', 'login'))
        self.assertEqual(login['input'], 'secret-ghcr\n')
        self.assertEqual(len(docker_wrapper), 1)
        self.assertIn('DOCKER_CONFIG=', docker_wrapper[0])

    def test_deploy_rehearsal_attestation_binds_all_ghcr_digests_and_native_ids(self):
        records = {
            role: {'repository': repository, 'digest': DIGEST,
                   'configDigest': 'sha256:' + str(index) * 64,
                   'sourceSha': SHA, 'platform': 'linux/amd64'}
            for index, (role, (_, repository)) in enumerate(PUBLISHER.IMAGES.items(), start=1)
        }
        rehearsal = {
            'status': 'PASS', 'dockerVersion': '29.1.3',
            'store': 'containerd', 'snapshotter': 'overlayfs',
            'images': {
                role: {**{key: record[key] for key in ('repository', 'digest', 'configDigest', 'sourceSha')},
                       'nativeId': 'sha256:' + 'f' * 64}
                for role, record in records.items()
            },
        }

        result = PUBLISHER.attest_deploy_rehearsal(ready_receipt(), rehearsal, records, SHA)

        self.assertEqual(result['gates']['deployRehearsal'], 'PASS')
        self.assertEqual(result['deployRehearsal'], rehearsal)

    def test_deploy_rehearsal_attestation_rejects_missing_or_mismatched_readback(self):
        records = {
            role: {'repository': repository, 'digest': DIGEST,
                   'configDigest': 'sha256:' + 'c' * 64,
                   'sourceSha': SHA, 'platform': 'linux/amd64'}
            for role, (_, repository) in PUBLISHER.IMAGES.items()
        }
        base = {'status': 'PASS', 'dockerVersion': '29.1.3', 'store': 'containerd',
                'snapshotter': 'overlayfs', 'images': {
                    role: {**{key: record[key] for key in ('repository', 'digest', 'configDigest', 'sourceSha')},
                           'nativeId': 'sha256:' + 'f' * 64}
                    for role, record in records.items()}}

        for bad in (
            {**base, 'status': 'FAIL'},
            {**base, 'store': 'classic'},
            {**base, 'images': {**base['images'], 'app': {**base['images']['app'], 'digest': 'sha256:' + 'e' * 64}}},
            {**base, 'images': {**base['images'], 'gsales': {**base['images']['gsales'], 'sourceSha': 'b' * 40}}},
            {**base, 'images': {key: value for key, value in base['images'].items() if key != 'agata'}},
        ):
            with self.subTest(rehearsal=bad), self.assertRaises((ValueError, KeyError)):
                PUBLISHER.attest_deploy_rehearsal(ready_receipt(), bad, records, SHA)

    def test_oci_receipt_publish_fails_closed_without_real_deploy_rehearsal_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            receipt = ready_receipt()
            receipt['gates'].pop('deployRehearsal')
            path.write_text(json.dumps(receipt) + '\n')
            with patch.object(PUBLISHER, 'run') as runner:
                with self.assertRaisesRegex(ValueError, 'deploy rehearsal gate'):
                    PUBLISHER.publish_receipt(path, SHA, RUN, ATTEMPT)
                runner.assert_not_called()

    def test_failed_private_containerd_rehearsal_prevents_receipt_publication(self):
        records = {role: {'repository': repository, 'digest': DIGEST,
                          'configDigest': 'sha256:' + 'c' * 64,
                          'sourceSha': SHA, 'platform': 'linux/amd64'}
                   for role, (_, repository) in PUBLISHER.IMAGES.items()}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            initial = ready_receipt()
            initial['gates'].pop('deployRehearsal')
            original = (json.dumps(initial) + '\n').encode()
            path.write_bytes(original)

            def failed_rehearsal(*_args):
                raise RuntimeError('synthetic private containerd failure')

            with patch.object(PUBLISHER, 'publish_receipt') as publisher:
                with self.assertRaisesRegex(RuntimeError, 'private containerd failure'):
                    PUBLISHER.publish_prepare_receipt(path, initial, records, SHA, RUN, ATTEMPT,
                        rehearsal_fn=failed_rehearsal, username='synthetic-reader', token='synthetic-token')
                publisher.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_prepare_receipt_publishes_only_after_exact_digest_rehearsal_is_attested(self):
        records = {role: {'repository': repository, 'digest': DIGEST,
                          'configDigest': 'sha256:' + 'c' * 64,
                          'sourceSha': SHA, 'platform': 'linux/amd64'}
                   for role, (_, repository) in PUBLISHER.IMAGES.items()}
        rehearsal = {'status': 'PASS', 'dockerVersion': '29.1.3', 'store': 'containerd',
                     'snapshotter': 'overlayfs', 'seconds': 12.3,
                     'images': {role: {'repository': record['repository'],
                         'digest': record['digest'], 'configDigest': record['configDigest'],
                         'sourceSha': SHA, 'nativeId': 'sha256:' + 'f' * 64}
                         for role, record in records.items()}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'receipt.json'
            initial = ready_receipt()
            initial['gates'].pop('deployRehearsal')
            path.write_text(json.dumps(initial) + '\n')
            calls = []

            def publisher(receipt_path, *_args):
                calls.append(json.loads(receipt_path.read_text()))
                return {'digest': DIGEST}

            updated, transport = PUBLISHER.publish_prepare_receipt(path, initial, records,
                SHA, RUN, ATTEMPT, rehearsal_fn=lambda *_args: rehearsal,
                receipt_publisher=publisher, username='synthetic-reader', token='synthetic-token')

        self.assertEqual(transport['digest'], DIGEST)
        self.assertEqual(updated['gates']['deployRehearsal'], 'PASS')
        self.assertEqual(updated['deployRehearsal'], rehearsal)
        self.assertEqual(calls, [updated])

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
