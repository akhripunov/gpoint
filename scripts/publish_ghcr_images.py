#!/usr/bin/env python3
"""Publish already-tested GPoint images to GHCR and attest exact manifest digests."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SHA40 = re.compile(r'^[a-f0-9]{40}$')
DIGEST = re.compile(r'^sha256:[a-f0-9]{64}$')
SOURCE_URL = 'https://github.com/akhripunov/gpoint'
IMAGES = {
    'app': ('gpoint-runtime', 'ghcr.io/akhripunov/gpoint-runtime'),
    'agata': ('gpoint-agata-runtime', 'ghcr.io/akhripunov/gpoint-agata-runtime'),
    'gsales': ('gpoint-g-sales-runtime', 'ghcr.io/akhripunov/gpoint-g-sales-runtime'),
}
RECEIPT_REPOSITORY = 'ghcr.io/akhripunov/gpoint-prepare-receipts'
RECEIPT_ARTIFACT_TYPE = 'application/vnd.gpoint.prepare.receipt.v1'
STATIC_REPOSITORY = 'ghcr.io/akhripunov/gpoint-miniapp-artifacts'
STATIC_ARTIFACT_TYPE = 'application/vnd.gpoint.miniapp.static.v1'


def parse_push_digest(output: str) -> str:
    matches = re.findall(r'\bdigest:\s*(sha256:[a-f0-9]{64})\b', output)
    if len(set(matches)) != 1:
        raise ValueError('GHCR push must return one exact manifest digest')
    return matches[0]


def validate_image_metadata(role: str, repository: str, digest: str, metadata: dict, sha: str) -> dict:
    expected = IMAGES.get(role)
    if expected is None:
        raise ValueError('unexpected GPoint image role')
    if repository != expected[1]:
        raise ValueError(f'GHCR repository mismatch for {role}')
    image_id = metadata.get('Id', '')
    if (not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(image_id)
            or metadata.get('Os') != 'linux' or metadata.get('Architecture') != 'amd64'):
        raise ValueError(f'GHCR image digest/config/platform mismatch for {role}')
    labels = (metadata.get('Config') or {}).get('Labels') or {}
    if (labels.get('org.opencontainers.image.revision') != sha
            or labels.get('org.opencontainers.image.source') != SOURCE_URL):
        raise ValueError(f'GHCR image source labels mismatch for {role}')
    return {'repository': repository, 'digest': digest, 'configDigest': image_id,
            'sourceSha': sha, 'platform': 'linux/amd64'}


def diagnostic_message_text(output: str) -> str:
    # A Python/Node traceback can print timeout=30, timeout=timeout or timeout: 900_000 from
    # source code even when the child failed immediately for another reason.
    return re.sub(r'\btimeout\s*[=:]\s*(?:[0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)',
                  '', output, flags=re.IGNORECASE)


def safe_runtime_diagnostic_fields(output: str | bytes, category: str) -> dict:
    """Extract allowlisted runtime markers; never return subprocess output."""
    if isinstance(output, bytes):
        output = output.decode('utf-8', 'replace')
    raw_output = str(output)
    output = diagnostic_message_text(raw_output).lower()
    diagnostic = {}
    markers = re.findall(r'GHCR_REHEARSAL_SUBSTAGE=(capture_loaded_images|parity_loaded_images|'
                         r'native_image_inspect_(?:app|agata|gsales)|negative_identity_(?:app|agata|gsales)|'
                         r'runtime_create_(?:app|agata|gsales)|runtime_inspect_(?:app|agata|gsales)|'
                         r'runtime_remove_(?:app|agata|gsales)|parity_loaded_images_final)', raw_output)
    if markers:
        diagnostic['substage'] = markers[-1]
    child_commands = re.findall(r"Command ['\"]?[\[(]['\"](docker|ctr|python3)['\"]", raw_output)
    if child_commands:
        diagnostic['childCommand'] = child_commands[-1]
    child_failures = re.findall(r'subprocess\.(CalledProcessError|TimeoutExpired)', raw_output)
    if child_failures:
        diagnostic['childFailure'] = {
            'CalledProcessError': 'CHILD_EXIT', 'TimeoutExpired': 'CHILD_TIMEOUT',
        }[child_failures[-1]]
    elif 'error while loading shared libraries:' in output and 'libpython' in output:
        diagnostic['childFailure'] = 'PYTHON_LIBRARY_LOAD'
    if 'ctr:' in output and ('failed to dial' in output or 'dial unix' in output):
        diagnostic['localReason'] = 'CONTAINERD_SOCKET_CONNECT'
    elif diagnostic.get('childCommand') == 'ctr' and 'context deadline exceeded' in output:
        diagnostic['localReason'] = 'CONTAINERD_RPC_TIMEOUT'
    elif 'loaded image' in output and any(value in output for value in (
            'mismatch', 'checksum', 'differs', 'invalid')):
        diagnostic['localReason'] = 'IMAGE_IDENTITY_VERIFICATION'
    reason_markers = (
        (('econnreset', 'connection reset'), 'CONNECTION_RESET'),
        (('econnrefused', 'connection refused'), 'CONNECTION_REFUSED'),
        (('eai_again', 'temporary failure in name resolution'), 'DNS_TEMPORARY'),
        (('enotfound', 'no such host'), 'DNS_NOT_FOUND'),
        (('etimedout', 'deadline exceeded', 'timed out', 'timeout'), 'TIMEOUT'),
        (('tls', 'x509', 'certificate'), 'TLS'),
    )
    if category in {'NETWORK', 'TIMEOUT'}:
        for needles, reason in reason_markers:
            if any(needle in output for needle in needles):
                diagnostic['reasonCode'] = reason
                break
    return diagnostic


def safe_output_text(*values: str | bytes | None) -> str:
    return '\n'.join(value.decode('utf-8', 'replace') if isinstance(value, bytes) else value
                      for value in values if value)


def safe_process_failure(stage: str, operation: str, role: str | None,
                         error: subprocess.CalledProcessError) -> dict:
    """Return fixed diagnostic fields only; never expose refs, URLs or command output."""
    allowed_stages = {
        'prepare_image_inspect', 'prepare_image_tag', 'ghcr_image_push',
        'ghcr_image_readback', 'ghcr_image_inspect', 'ghcr_rehearsal_info',
        'ghcr_rehearsal_auth', 'ghcr_rehearsal_readback',
        'ghcr_rehearsal_identity', 'ghcr_rehearsal_tag',
        'ghcr_rehearsal_runtime_parity', 'ghcr_receipt_push',
        'ghcr_receipt_readback', 'ghcr_static_push', 'ghcr_static_readback',
    }
    if stage not in allowed_stages or operation not in {'inspect', 'tag', 'push', 'pull', 'login', 'rehearse'}:
        stage, operation, role = 'unknown', 'unknown', None
    if role not in {*IMAGES.keys(), None}:
        role = None
    output = safe_output_text(error.stderr, error.stdout)
    raw_output = str(output)
    output = diagnostic_message_text(raw_output).lower()
    if any(marker in output for marker in ('unauthorized', 'authentication required', 'invalid username or password')):
        category = 'AUTH'
    elif any(marker in output for marker in ('denied', 'forbidden', 'insufficient_scope', 'permission denied')):
        category = 'PERMISSION'
    elif any(marker in output for marker in ('manifest unknown', 'not found', 'no such image', 'unknown blob')):
        category = 'OCI_NOT_FOUND'
    elif any(marker in output for marker in ('unsupported media type', 'manifest invalid', 'media type')):
        category = 'OCI_MEDIA_TYPE'
    elif 'error while loading shared libraries:' in output or 'filenotfounderror:' in output:
        category = 'TOOLING'
    elif any(marker in output for marker in ('tls', 'x509', 'certificate', 'timed out', 'timeout',
                                              'connection reset', 'econnreset', 'connection refused',
                                              'econnrefused', 'no such host', 'enotfound', 'eai_again')):
        category = 'NETWORK'
    elif any(marker in output for marker in ('daemon is not running', 'cannot connect to the docker daemon', 'failed to connect')):
        category = 'TOOLING'
    else:
        category = 'PROCESS_EXIT'
    diagnostic = {'stage': stage, 'operation': operation, 'role': role,
                  'category': category, 'exitCode': error.returncode}
    if stage == 'ghcr_rehearsal_runtime_parity':
        diagnostic.update(safe_runtime_diagnostic_fields(raw_output, category))
    return diagnostic


class SafeProcessFailure(RuntimeError):
    def __init__(self, diagnostic: dict):
        super().__init__('release subprocess failed')
        self.diagnostic = diagnostic


def run(*args: str, capture: bool = False, cwd: Path | None = None,
        env: dict | None = None, input: str | None = None, timeout: int = 900,
        stage: str | None = None, operation: str | None = None,
        role: str | None = None) -> str:
    try:
        result = subprocess.run(args, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, cwd=cwd, env=env, input=input)
    except subprocess.CalledProcessError as error:
        if stage and operation:
            raise SafeProcessFailure(safe_process_failure(stage, operation, role, error)) from None
        raise
    except subprocess.TimeoutExpired as error:
        if stage and operation:
            diagnostic = {'stage': stage, 'operation': operation,
                          'role': role if role in IMAGES else None,
                          'category': 'TIMEOUT', 'exitCode': None}
            if stage == 'ghcr_rehearsal_runtime_parity':
                output = safe_output_text(error.stderr, error.stdout)
                diagnostic.update(safe_runtime_diagnostic_fields(output, 'TIMEOUT'))
            raise SafeProcessFailure(diagnostic) from None
        raise
    except OSError:
        if stage and operation:
            diagnostic = {'stage': stage, 'operation': operation,
                          'role': role if role in IMAGES else None,
                          'category': 'TOOLING', 'exitCode': None}
            raise SafeProcessFailure(diagnostic) from None
        raise
    return ((result.stdout or '') + (result.stderr or '')) if capture else ''


def run_rehearsal_with_retry(node: str, helper: Path | str, proof_path: Path | str,
                             sha: str, root: Path | str, env: dict[str, str]) -> str:
    """Retry one safe read-only runtime verification after a transient network failure."""
    args = (node, str(helper), str(proof_path), sha, str(root))
    try:
        return run(*args, env=env, timeout=900, stage='ghcr_rehearsal_runtime_parity',
                   operation='rehearse')
    except SafeProcessFailure as error:
        diagnostic = error.diagnostic
        retryable_substages = {
            'capture_loaded_images', 'parity_loaded_images',
            'native_image_inspect_app', 'native_image_inspect_agata', 'native_image_inspect_gsales',
            'negative_identity_app', 'negative_identity_agata', 'negative_identity_gsales',
            'runtime_inspect_app', 'runtime_inspect_agata', 'runtime_inspect_gsales',
            'parity_loaded_images_final',
        }
        if (diagnostic.get('stage') != 'ghcr_rehearsal_runtime_parity'
                or diagnostic.get('category') != 'NETWORK'
                or diagnostic.get('substage') not in retryable_substages):
            raise
        reason = diagnostic.get('reasonCode', 'NETWORK')
        print(f"GHCR_REHEARSAL_NETWORK_RETRY=1 stage=ghcr_rehearsal_runtime_parity "
              f"substage={diagnostic['substage']} reason={reason}", file=sys.stderr)
        return run(*args, env=env, timeout=900, stage='ghcr_rehearsal_runtime_parity',
                   operation='rehearse')


def rehearsal_child_environment(source: dict | None = None) -> dict[str, str]:
    """Pass only non-secret runtime settings to disposable rehearsal children."""
    source = os.environ if source is None else source
    allowed = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
    return {key: source[key] for key in allowed if source.get(key)}


def cleanup_rehearsal(root: Path, daemon, log, sudo: str, env: dict[str, str], *, containerd=None) -> None:
    """Stop the isolated daemon and verify its credential-bearing scratch root is removed."""
    cleanup_error = None
    for owned_daemon in (daemon, containerd):
        if owned_daemon is None:
            continue
        daemon = owned_daemon
        try:
            os.killpg(daemon.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_error = error
        if daemon.poll() is None:
            try:
                daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(daemon.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    cleanup_error = cleanup_error or error
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    cleanup_error = cleanup_error or error
        if daemon.poll() is None:
            cleanup_error = cleanup_error or RuntimeError('private rehearsal daemon remained active')
        group_gone = False
        for _ in range(20):
            try:
                os.killpg(daemon.pid, 0)
            except ProcessLookupError:
                group_gone = True
                break
            except OSError as error:
                cleanup_error = cleanup_error or error
                break
            try:
                os.killpg(daemon.pid, signal.SIGKILL)
            except ProcessLookupError:
                group_gone = True
                break
            except OSError as error:
                cleanup_error = cleanup_error or error
                break
            time.sleep(0.1)
        if not group_gone:
            cleanup_error = cleanup_error or RuntimeError('private rehearsal process group remained active')

    try:
        log.close()
    except Exception as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        # Never delete state/config beneath an owned process group still alive.
        raise RuntimeError('private rehearsal cleanup failed') from cleanup_error
    try:
        subprocess.run([sudo, '-n', 'rm', '-rf', '--', str(root)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       env=env)
    except Exception as error:
        cleanup_error = cleanup_error or error
    if root.exists():
        cleanup_error = cleanup_error or RuntimeError('private rehearsal scratch root remains')
    if cleanup_error is not None:
        raise RuntimeError('private rehearsal cleanup failed') from cleanup_error


def attest_deploy_rehearsal(receipt: dict, rehearsal: dict, records: dict, sha: str) -> dict:
    """Bind a completed private containerd GHCR pull to the exact Prepare images."""
    if (not SHA40.fullmatch(sha) or rehearsal.get('status') != 'PASS'
            or rehearsal.get('dockerVersion') != '29.1.3'
            or rehearsal.get('store') != 'containerd'
            or rehearsal.get('snapshotter') != 'overlayfs'):
        raise ValueError('deploy rehearsal did not prove the production image store')
    images = rehearsal.get('images')
    if not isinstance(images, dict) or set(images) != set(IMAGES) or set(records) != set(IMAGES):
        raise ValueError('deploy rehearsal image identities are incomplete')
    for role in IMAGES:
        observed = images[role]
        expected = records[role]
        for field in ('repository', 'digest', 'configDigest', 'sourceSha'):
            if observed.get(field) != expected.get(field):
                raise ValueError(f'deploy rehearsal {field} mismatch for {role}')
        if (expected.get('sourceSha') != sha or expected.get('platform') != 'linux/amd64'
                or not DIGEST.fullmatch(observed.get('nativeId', ''))
                or observed['nativeId'] == expected.get('configDigest')):
            raise ValueError(f'deploy rehearsal native image identity is invalid for {role}')
    enriched = dict(receipt)
    enriched['gates'] = {**(receipt.get('gates') or {}), 'deployRehearsal': 'PASS'}
    enriched['deployRehearsal'] = rehearsal
    return enriched


def private_containerd_endpoint_diagnostics(info: dict, root: Path) -> tuple[str, str]:
    """Compare the private daemon's reported endpoint without exposing paths/namespaces."""
    containerd = info.get('Containerd')
    containerd = containerd if isinstance(containerd, dict) else {}
    address = containerd.get('Address')
    namespaces = containerd.get('Namespaces')
    namespace = namespaces.get('Containers') if isinstance(namespaces, dict) else None
    def compare(actual, expected):
        return 'UNAVAILABLE' if not isinstance(actual, str) or not actual else 'MATCH' if actual == expected else 'MISMATCH'
    return compare(address, str(root / 'exec/containerd/containerd.sock')), compare(namespace, 'moby')


def rehearse_ghcr_images(records: dict, sha: str, username: str, token: str) -> dict:
    """Pull exact GHCR manifest digests into a private containerd Docker store and verify reuse."""
    if (not SHA40.fullmatch(sha) or set(records) != set(IMAGES)
            or not isinstance(username, str) or not username.strip()
            or not isinstance(token, str) or not token):
        raise ValueError('exact GHCR image identities and scoped read credentials are required')
    dockerd, docker, ctr, sudo, containerd_binary = (
        shutil.which(name) for name in ('dockerd', 'docker', 'ctr', 'sudo', 'containerd'))
    node = shutil.which('node')
    if not all((dockerd, docker, ctr, sudo, node, containerd_binary)):
        raise RuntimeError('private containerd rehearsal tooling is unavailable')

    root = Path(tempfile.mkdtemp(prefix='gpoint-ghcr-containerd-rehearsal-'))
    socket = root / 'docker.sock'
    pidfile = root / 'dockerd.pid'
    log_path = root / 'dockerd.log'
    host = f'unix://{socket}'
    docker_config = root / 'docker-config'
    docker_config.mkdir(mode=0o700)
    config = root / 'daemon.json'
    config.write_text(json.dumps({'features': {'containerd-snapshotter': True}}))
    containerd_socket = root / 'exec/containerd/containerd.sock'
    containerd_socket.parent.mkdir(mode=0o700, parents=True)
    containerd_config = root / 'containerd.toml'
    containerd_config.write_text(
        'version = 2\n'
        'disabled_plugins = ["io.containerd.grpc.v1.cri", '
        '"io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime"]\n')
    log = log_path.open('wb')
    daemon = None
    containerd_process = None
    started = time.monotonic()
    daemon_env = rehearsal_child_environment()
    try:
        # An explicit private server prevents dockerd from selecting a host
        # containerd endpoint that differs from the verifier's ctr wrapper.
        containerd_process = subprocess.Popen(
            [sudo, '-n', 'env', f'PATH={os.environ.get("PATH", "")}', containerd_binary,
             '--config', str(containerd_config), '--address', str(containerd_socket),
             '--root', str(root / 'containerd-data'), '--state', str(root / 'containerd-state')],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=daemon_env)
        containerd_ready = False
        for _ in range(90):
            if containerd_process.poll() is not None:
                break
            try:
                probe = subprocess.run([sudo, '-n', ctr, '--address', str(containerd_socket), 'version'],
                                       env=daemon_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, timeout=3)
            except subprocess.TimeoutExpired:
                probe = None
            if probe is not None and probe.returncode == 0:
                containerd_ready = True
                break
            time.sleep(1)
        if not containerd_ready:
            raise RuntimeError('private rehearsal containerd did not become ready')
        daemon = subprocess.Popen(
            [sudo, '-n', 'env', f'PATH={os.environ.get("PATH", "")}', dockerd,
             '--config-file', str(config), '--host', host,
             '--data-root', str(root / 'data'), '--exec-root', str(root / 'exec'),
             '--containerd', str(containerd_socket),
             '--pidfile', str(pidfile), '--storage-driver=overlayfs',
             '--containerd-namespace=moby', '--bridge=none', '--iptables=false',
             '--ip-masq=false', '--ip-forward=false'],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=daemon_env)

        wrappers = root / 'bin'
        wrappers.mkdir(mode=0o700)
        for name, command in {
            'docker': [sudo, '-n', 'env', f'DOCKER_CONFIG={docker_config}', docker, '--host', host],
            'ctr': [sudo, '-n', ctr, '--address', str(containerd_socket)],
        }.items():
            wrapper = wrappers / name
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
            wrapper.chmod(0o700)

        client_env = dict(daemon_env, PATH=f'{wrappers}:{daemon_env.get("PATH", "")}',
                          DOCKER_HOST=host, DOCKER_CONFIG=str(docker_config))
        for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_API_VERSION'):
            client_env.pop(key, None)
        ready = False
        for _ in range(90):
            if daemon.poll() is not None:
                break
            probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                   env=client_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, timeout=3)
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise RuntimeError('private containerd Docker daemon did not become ready')

        info = json.loads(run('docker', 'info', '--format', '{{json .}}', capture=True,
                              env=client_env, timeout=10, stage='ghcr_rehearsal_info', operation='inspect'))
        driver_status = info.get('DriverStatus') or []
        if (info.get('ServerVersion') != '29.1.3' or info.get('Driver') != 'overlayfs'
                or ['driver-type', 'io.containerd.snapshotter.v1'] not in driver_status):
            raise RuntimeError('private rehearsal store is not Docker 29.1.3 containerd/overlayfs')
        endpoint, namespace = private_containerd_endpoint_diagnostics(info, root)
        print(f'GHCR_REHEARSAL_CONTAINERD_ENDPOINT={endpoint} NAMESPACE={namespace}', flush=True)
        if endpoint != 'MATCH' or namespace != 'MATCH':
            raise RuntimeError('private containerd endpoint or namespace does not match isolated rehearsal')
        run('docker', 'login', 'ghcr.io', '--username', username, '--password-stdin',
            capture=True, env=client_env, input=token + '\n', timeout=30,
            stage='ghcr_rehearsal_auth', operation='login')

        observed_images = {}
        proof_images = {}
        for role, (local_name, repository) in IMAGES.items():
            record = records[role]
            if (record.get('repository') != repository or record.get('sourceSha') != sha
                    or record.get('platform') != 'linux/amd64'
                    or not DIGEST.fullmatch(record.get('digest', ''))
                    or not DIGEST.fullmatch(record.get('configDigest', ''))):
                raise ValueError(f'invalid exact GHCR Prepare image identity for {role}')
            reference = f'{repository}@{record["digest"]}'
            run('docker', 'pull', reference, capture=True, env=client_env, timeout=900,
                stage='ghcr_rehearsal_readback', operation='pull', role=role)
            metadata = json.loads(run('docker', 'image', 'inspect', reference,
                                      capture=True, env=client_env, timeout=30,
                                      stage='ghcr_rehearsal_identity', operation='inspect', role=role))[0]
            descriptor = metadata.get('Descriptor') or {}
            native_id = metadata.get('Id', '')
            if (metadata.get('Os') != 'linux' or metadata.get('Architecture') != 'amd64'
                    or not DIGEST.fullmatch(native_id)
                    or native_id != record['digest']
                    or native_id == record['configDigest']
                    or descriptor.get('digest') != native_id
                    or descriptor.get('mediaType') not in (
                        'application/vnd.oci.image.manifest.v1+json',
                        'application/vnd.docker.distribution.manifest.v2+json')
                    or (metadata.get('Config') or {}).get('Labels', {}).get(
                        'org.opencontainers.image.revision') != sha
                    or (metadata.get('Config') or {}).get('Labels', {}).get(
                        'org.opencontainers.image.source') != SOURCE_URL):
                raise ValueError(f'private containerd GHCR manifest/config identity mismatch for {role}')
            local_tag = f'{local_name}:{sha}'
            run('docker', 'tag', reference, local_tag, env=client_env, timeout=30,
                stage='ghcr_rehearsal_tag', operation='tag', role=role)
            proof_images[role] = {'id': record['configDigest']}
            observed_images[role] = {
                'repository': repository, 'digest': record['digest'],
                'configDigest': record['configDigest'], 'sourceSha': sha,
                'nativeId': native_id,
            }

        proof_path = root / 'proof.json'
        proof_path.write_text(json.dumps({'candidateSha': sha, 'images': proof_images}))
        helper = Path(__file__).with_name('rehearse-release-bundle.mjs')
        run_rehearsal_with_retry(node, helper, proof_path, sha, root, client_env)
        elapsed = round(time.monotonic() - started, 1)
        return {'status': 'PASS', 'dockerVersion': '29.1.3', 'store': 'containerd',
                'snapshotter': 'overlayfs', 'seconds': elapsed, 'images': observed_images}
    finally:
        cleanup_rehearsal(root, daemon, log, sudo, daemon_env, containerd=containerd_process)


def safe_oci_file(path: Path, expected_name: str, media_type: str) -> tuple[str, Path]:
    """Return an ORAS-safe relative file reference and its resolved working directory."""
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or path.name != expected_name
            or path.is_symlink() or not path.is_file()):
        raise ValueError('unsafe OCI artifact path')
    working_dir = path.parent.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved.parent != working_dir or resolved.name != expected_name or not resolved.is_file():
        raise ValueError('unsafe OCI artifact path')
    return f'{expected_name}:{media_type}', working_dir


def publish_images(sha: str, run_id: str, attempt: str) -> dict:
    if not SHA40.fullmatch(sha) or not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid exact Prepare identity')
    tag = f'prepare-{sha}-{run_id}-{attempt}'
    records = {}
    for role, (local_name, repository) in IMAGES.items():
        local = f'{local_name}:{sha}'
        remote = f'{repository}:{tag}'
        local_metadata = json.loads(run('docker', 'image', 'inspect', local, capture=True,
                                        stage='prepare_image_inspect', operation='inspect', role=role))[0]
        local_identity = validate_image_metadata(
            role, repository, local_metadata.get('Id', ''), local_metadata, sha)
        run('docker', 'tag', local, remote, stage='prepare_image_tag', operation='tag', role=role)
        output = run('docker', 'push', remote, capture=True,
                     stage='ghcr_image_push', operation='push', role=role)
        digest = parse_push_digest(output)
        reference = f'{repository}@{digest}'
        run('docker', 'pull', reference, capture=True,
            stage='ghcr_image_readback', operation='pull', role=role)
        metadata = json.loads(run('docker', 'image', 'inspect', reference, capture=True,
                                  stage='ghcr_image_inspect', operation='inspect', role=role))[0]
        record = validate_image_metadata(role, repository, digest, metadata, sha)
        if record['configDigest'] != local_identity['configDigest']:
            raise ValueError(f'GHCR readback image config differs from built {role} image')
        records[role] = record
    return records


def validate_prepare_identity(receipt: dict, sha: str, run_id: str, attempt: str) -> None:
    if (receipt.get('schema') not in (1, 2) or receipt.get('status') != 'READY'
            or receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
            or str(receipt.get('runAttempt')) != attempt):
        raise ValueError('Prepare receipt identity changed before GHCR publication')


def augment_receipt(receipt: dict, records: dict, sha: str, run_id: str, attempt: str,
                    static_artifact: dict | None = None) -> dict:
    validate_prepare_identity(receipt, sha, run_id, attempt)
    static_only = receipt.get('route') == 'STATIC_UI_ONLY'
    if static_only:
        if records or not static_artifact:
            raise ValueError('static-only Prepare must publish only its verified Mini App artifact')
    elif set(records) != set(IMAGES):
        raise ValueError('GHCR image records are incomplete')
    for role, record in records.items():
        if record.get('sourceSha') != sha or record.get('platform') != 'linux/amd64':
            raise ValueError(f'GHCR image proof is invalid for {role}')
    # The manifest digest and receipt blob checksum are transport metadata.
    # Embedding either in this blob would be self-referential.
    enriched = {**receipt, 'schema': 3, 'transport': 'ghcr-oci', 'images': records}
    enriched.pop('archiveSha256', None)
    enriched.pop('receiptDigest', None)
    enriched.pop('receiptSha256', None)
    if static_only:
        enriched['gates'] = {**enriched['gates'], 'ghcrStaticReadback': 'PASS'}
    else:
        enriched['gates'] = {**enriched['gates'], 'ghcrImageReadback': 'PASS'}
    if static_artifact:
        enriched['staticArtifact'] = static_artifact
    return enriched


def publish_receipt(receipt_path: Path, sha: str, run_id: str, attempt: str) -> dict:
    """Publish one canonical receipt OCI artifact, then verify it by digest."""
    if not SHA40.fullmatch(sha) or not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid exact Prepare identity')
    file_reference, working_dir = safe_oci_file(
        receipt_path, 'receipt.json', 'application/vnd.gpoint.prepare.receipt.v1+json')
    raw = receipt_path.read_bytes()
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    receipt = json.loads(raw)
    if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
            or str(receipt.get('runAttempt')) != attempt or receipt.get('status') != 'READY'):
        raise ValueError('OCI receipt does not bind exact Prepare identity')
    if receipt.get('route') != 'STATIC_UI_ONLY' and receipt.get('gates', {}).get('deployRehearsal') != 'PASS':
        raise ValueError('OCI receipt is missing the deploy rehearsal gate')
    tag = f'{RECEIPT_REPOSITORY}:prepare-{sha}-{run_id}-{attempt}'
    output = run('oras', 'push', '--artifact-type', RECEIPT_ARTIFACT_TYPE,
                 '--format', 'json', tag, file_reference, capture=True, cwd=working_dir,
                 stage='ghcr_receipt_push', operation='push')
    try:
        descriptor = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError('ORAS did not return a valid artifact descriptor') from error
    digest = descriptor.get('digest')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('ORAS did not return one exact receipt manifest digest')
    if descriptor.get('reference') not in (None, f'{RECEIPT_REPOSITORY}@{digest}'):
        raise ValueError('ORAS returned an unexpected receipt repository')
    with tempfile.TemporaryDirectory(prefix='gpoint-receipt-readback-') as directory:
        run('oras', 'pull', f'{RECEIPT_REPOSITORY}@{digest}', '--output', directory,
            stage='ghcr_receipt_readback', operation='pull')
        readback = Path(directory) / receipt_path.name
        if not readback.is_file():
            raise ValueError('OCI receipt readback did not contain receipt.json')
        content = readback.read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt_sha256 or content != raw:
            raise ValueError('OCI receipt readback content mismatch')
    return {'repository': RECEIPT_REPOSITORY, 'digest': digest,
            'contentSha256': receipt_sha256, 'sourceSha': sha,
            'runId': int(run_id), 'runAttempt': int(attempt)}


def publish_static_artifact(bundle_path: Path, receipt: dict, sha: str,
                            run_id: str, attempt: str) -> dict:
    if (receipt.get('staticArtifactName') != 'miniapp.zip'
            or receipt.get('staticArtifactRoute') not in ('V4_FAST_STATIC', 'V4_ADVISORY_ONLY')
            or not Path(bundle_path).is_file()):
        raise ValueError('static Mini App artifact is missing or not approved by V4 scope')
    file_reference, working_dir = safe_oci_file(
        bundle_path, 'miniapp.zip', 'application/vnd.gpoint.miniapp.static.v1+zip')
    content = bundle_path.read_bytes()
    content_sha256 = hashlib.sha256(content).hexdigest()
    if receipt.get('staticArtifactSha256') != content_sha256:
        raise ValueError('static Mini App artifact checksum differs from Prepare receipt')
    tag = f'{STATIC_REPOSITORY}:prepare-{sha}-{run_id}-{attempt}'
    output = run('oras', 'push', '--artifact-type', STATIC_ARTIFACT_TYPE,
                 '--format', 'json', tag,
                 file_reference, capture=True, cwd=working_dir,
                 stage='ghcr_static_push', operation='push')
    try:
        descriptor = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError('ORAS did not return a valid static artifact descriptor') from error
    digest = descriptor.get('digest')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('ORAS did not return one exact static artifact manifest digest')
    with tempfile.TemporaryDirectory(prefix='gpoint-static-readback-') as directory:
        run('oras', 'pull', f'{STATIC_REPOSITORY}@{digest}', '--output', directory,
            stage='ghcr_static_readback', operation='pull')
        readback = Path(directory) / bundle_path.name
        if not readback.is_file():
            raise ValueError('OCI static artifact readback content mismatch')
        readback_content = readback.read_bytes()
        if hashlib.sha256(readback_content).hexdigest() != content_sha256 or readback_content != content:
            raise ValueError('OCI static artifact readback content mismatch')
    return {'repository': STATIC_REPOSITORY, 'digest': digest,
            'contentSha256': content_sha256, 'sourceSha': sha,
            'runId': int(run_id), 'runAttempt': int(attempt)}


def publish_prepare_receipt(receipt_path: Path, receipt: dict, records: dict,
                            sha: str, run_id: str, attempt: str,
                            static_artifact: dict | None = None, *,
                            rehearsal_fn=rehearse_ghcr_images,
                            receipt_publisher=publish_receipt,
                            username: str | None = None, token: str | None = None) -> tuple[dict, dict]:
    """Rehearse private GHCR images before the final READY receipt is published."""
    if receipt.get('route') != 'STATIC_UI_ONLY':
        rehearsal = rehearsal_fn(records, sha,
                                 username if username is not None else os.environ.get('GHCR_REHEARSAL_USERNAME', ''),
                                 token if token is not None else os.environ.get('GHCR_REHEARSAL_TOKEN', ''))
        receipt = attest_deploy_rehearsal(receipt, rehearsal, records, sha)
        print(f"PREPARE_DEPLOY_REHEARSAL=PASS docker={rehearsal['dockerVersion']} store={rehearsal['store']} seconds={rehearsal['seconds']}")
    updated = augment_receipt(receipt, records, sha, run_id, attempt, static_artifact)
    temporary = receipt_path.with_suffix('.json.pending')
    temporary.write_text(json.dumps(updated, sort_keys=True, separators=(',', ':')) + '\n')
    temporary.replace(receipt_path)
    transport = receipt_publisher(receipt_path, sha, run_id, attempt)
    return updated, transport


def main() -> None:
    sha = os.environ.get('CANDIDATE_SHA', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if (os.environ.get('GITHUB_REPOSITORY') != 'akhripunov/gpoint'
            or os.environ.get('GITHUB_SHA') != sha
            or not SHA40.fullmatch(sha)):
        raise RuntimeError('GHCR publication requires exact trusted GPoint workflow source')
    root = Path(os.environ.get('RUNNER_TEMP', '')) / 'gpoint-bundle'
    receipt_path = root / 'receipt.json'
    receipt = json.loads(receipt_path.read_text())
    validate_prepare_identity(receipt, sha, run_id, attempt)
    if receipt.get('route') == 'STATIC_UI_ONLY':
        records = {}
    else:
        records = publish_images(sha, run_id, attempt)
    static_path = root / 'miniapp.zip'
    if receipt.get('staticArtifactSha256') and not static_path.is_file():
        raise ValueError('Prepare receipt requires a missing Mini App OCI artifact')
    static_artifact = (publish_static_artifact(static_path, receipt, sha, run_id, attempt)
                       if static_path.exists() else None)
    updated, receipt_transport = publish_prepare_receipt(
        receipt_path, receipt, records, sha, run_id, attempt, static_artifact)
    output_path = os.environ.get('GITHUB_OUTPUT')
    if output_path:
        with Path(output_path).open('a', encoding='utf-8') as output:
            output.write(f"receipt_digest={receipt_transport['digest']}\n")
            output.write(f"receipt_content_sha256={receipt_transport['contentSha256']}\n")
    print('GHCR_IMAGE_PROVENANCE=PASS roles=' + str(len(records)) + ' platform=linux/amd64')
    print('GHCR_RECEIPT_DIGEST=' + receipt_transport['digest'])
    if static_artifact:
        print('GHCR_STATIC_ARTIFACT_DIGEST=' + static_artifact['digest'])


if __name__ == '__main__':
    try:
        main()
    except SafeProcessFailure as error:
        diagnostic = error.diagnostic
        role = f" role={diagnostic['role']}" if diagnostic['role'] else ''
        substage = f" substage={diagnostic['substage']}" if diagnostic.get('substage') else ''
        reason = f" reason={diagnostic['reasonCode']}" if diagnostic.get('reasonCode') else ''
        child = f" child={diagnostic['childCommand']}" if diagnostic.get('childCommand') else ''
        failure = f" childFailure={diagnostic['childFailure']}" if diagnostic.get('childFailure') else ''
        local_reason = f" localReason={diagnostic['localReason']}" if diagnostic.get('localReason') else ''
        print(f"GHCR_IMAGE_PROVENANCE=BLOCKED stage={diagnostic['stage']} "
              f"operation={diagnostic['operation']}{role}{substage} category={diagnostic['category']}{reason}{child}{failure}{local_reason} "
              f"exit={diagnostic['exitCode']}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # Do not print Docker transport errors, which can include registry metadata.
        print(f'GHCR_IMAGE_PROVENANCE=BLOCKED error={type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
