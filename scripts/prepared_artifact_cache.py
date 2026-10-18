#!/usr/bin/env python3
"""Resolve and verify the exact Prepare OCI receipt from GHCR."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from network_retry import (NETWORK_RETRY_BUDGET_SECONDS, NetworkUnavailable,
                           RetryBudget, retry_network_read)

REPO = 'akhripunov/gpoint'
RECEIPT_REPOSITORY = 'akhripunov/gpoint-prepare-receipts'
RECEIPT_MEDIA_TYPE = 'application/vnd.gpoint.prepare.receipt.v1'
RECEIPT_LAYER_MEDIA_TYPE = 'application/vnd.gpoint.prepare.receipt.v1+json'
STATIC_ARTIFACT_TYPE = 'application/vnd.gpoint.miniapp.static.v1'
STATIC_LAYER_TYPE = 'application/vnd.gpoint.miniapp.static.v1+zip'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_STATIC_BYTES = 512 * 1024 * 1024
GHCR_READ_RETRY_BUDGET_SECONDS = 60
NETWORK_BUDGET = None
SAFE_DIAGNOSTIC_REASON_CODES = {
    'GHCR receipt response exceeds size limit': 'GHCR_RESPONSE_TOO_LARGE',
    'GHCR artifact manifest digest mismatch': 'OCI_MANIFEST_DIGEST_MISMATCH',
    'GHCR artifact type is invalid': 'OCI_MANIFEST_TYPE_MISMATCH',
    'GHCR artifact must contain exactly one layer': 'OCI_LAYER_COUNT_INVALID',
    'GHCR artifact layer descriptor is invalid': 'OCI_LAYER_DESCRIPTOR_INVALID',
    'GHCR artifact layer digest or size mismatch': 'OCI_LAYER_CONTENT_MISMATCH',
}


class NetworkStageError(RuntimeError):
    def __init__(self, stage, error_type, category='NETWORK', http_status=None, retryable=False):
        super().__init__(stage, error_type, category, http_status)
        self.stage, self.error_type = stage, error_type
        self.category, self.http_status = category, http_status
        self.is_retryable = retryable


ACTIVE_DIAGNOSTIC_STAGE = 'initialization'
DIAGNOSTIC_IDENTITY = {'candidateSha': '', 'runId': '', 'runAttempt': ''}
GHCR_READBACK_COMPLETED = False


def set_diagnostic_stage(stage):
    global ACTIVE_DIAGNOSTIC_STAGE
    ACTIVE_DIAGNOSTIC_STAGE = stage


def safe_exception_type(error):
    if isinstance(error, urllib.error.HTTPError):
        return 'HTTPError'
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,47}', name) else 'Exception'


def exception_category(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 401:
            return 'AUTH'
        if error.code == 403:
            return 'PERMISSION'
        return 'NETWORK' if error.code in (408, 425, 429) or error.code >= 500 else 'HTTP'
    if isinstance(error, urllib.error.URLError):
        return 'NETWORK'
    if isinstance(error, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError, ssl.SSLError)):
        return 'NETWORK'
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return 'INVALID_RESPONSE'
    if isinstance(error, OSError):
        return 'LOCAL_IO'
    if isinstance(error, (RuntimeError, ValueError, KeyError, TypeError)):
        return 'VALIDATION'
    return 'INTERNAL'


def diagnostic_payload(error):
    status = error.http_status if isinstance(error, NetworkStageError) else (
        error.code if isinstance(error, urllib.error.HTTPError) else None)
    diagnostic = {
        'schema': 1,
        'stage': error.stage if isinstance(error, NetworkStageError) else ACTIVE_DIAGNOSTIC_STAGE,
        'errorType': error.error_type if isinstance(error, NetworkStageError) else safe_exception_type(error),
        'category': error.category if isinstance(error, NetworkStageError) else exception_category(error),
        'httpStatus': status if isinstance(status, int) and 400 <= status <= 599 else None,
        **DIAGNOSTIC_IDENTITY,
        'ghcrReadbackCompleted': GHCR_READBACK_COMPLETED,
    }
    if isinstance(error, json.JSONDecodeError) and ACTIVE_DIAGNOSTIC_STAGE.startswith('ghcr_artifact_digest'):
        diagnostic['reasonCode'] = 'OCI_MANIFEST_INVALID_JSON'
    else:
        reason_code = SAFE_DIAGNOSTIC_REASON_CODES.get(str(error))
        if reason_code:
            diagnostic['reasonCode'] = reason_code
    return diagnostic


def emit_blocked_diagnostic(error):
    diagnostic = diagnostic_payload(error)
    safe_stage = diagnostic['stage']
    # Preserve the controller's existing allowlisted summary mapping while the
    # structured companion carries the precise internal stage and safe details.
    controller_stage = safe_stage if safe_stage in {
        'prepare_run_metadata', 'current_main_ref', 'ghcr_receipt',
        'prepare_publisher_logs', 'static_artifact_readback', 'receipt_validation'} else (
        'ghcr_receipt' if safe_stage.startswith('ghcr_') else 'receipt_validation')
    print('ARTIFACT_CACHE_DIAGNOSTIC=' + json.dumps(diagnostic, sort_keys=True, separators=(',', ':')), flush=True)
    print(f"ARTIFACT_CACHE=BLOCKED STAGE={controller_stage} ERROR_TYPE={diagnostic['errorType']}", flush=True)
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        try:
            with open(summary_path, 'a', encoding='utf-8') as summary:
                summary.write('### GHCR receipt readback diagnostic\n\n```json\n')
                summary.write(json.dumps(diagnostic, sort_keys=True, indent=2))
                summary.write('\n```\n')
        except OSError:
            # Diagnostics must never turn a fail-closed controller result into a pass.
            pass


def retryable(error):
    if isinstance(error, NetworkStageError):
        return error.is_retryable
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 425, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, ssl.SSLError):
        return not any(marker in str(reason).lower() for marker in (
            'certificate_verify_failed', 'wrong_version_number', 'protocol_version'))
    return isinstance(reason, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError))


def new_network_budget():
    """Leave diagnostic headroom inside the controller's 120-second subprocess limit."""
    return RetryBudget(seconds=min(NETWORK_RETRY_BUDGET_SECONDS,
                                   GHCR_READ_RETRY_BUDGET_SECONDS))


def network_urlopen(request, timeout=30):
    """Bound each urllib call by the same absolute budget used for safe retries."""
    if NETWORK_BUDGET is None:
        return urllib.request.urlopen(request, timeout=timeout)
    remaining = NETWORK_BUDGET.deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkUnavailable('network_retry_budget')
    return urllib.request.urlopen(request, timeout=min(timeout, remaining))


def open_without_redirect(request, timeout=30):
    """Open one HTTP response without following a redirect automatically."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    if NETWORK_BUDGET is None:
        bounded_timeout = timeout
    else:
        remaining = NETWORK_BUDGET.deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkUnavailable('network_retry_budget')
        bounded_timeout = min(timeout, remaining)
    return urllib.request.build_opener(NoRedirect).open(request, timeout=bounded_timeout)


def trusted_log_storage_url(value):
    parsed = urllib.parse.urlparse(value)
    hostname = parsed.hostname or ''
    try:
        port = parsed.port
    except ValueError:
        return False
    trusted_host = (re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net', hostname)
                    or hostname.endswith(('.githubusercontent.com', '.actions.githubusercontent.com')))
    return (parsed.scheme == 'https' and bool(hostname) and trusted_host
            and port in (None, 443) and not parsed.username
            and not parsed.password and not parsed.fragment)


def read_network_response(response, limit):
    """Read with a hard wait bound, interrupting a slow or trickling response."""
    if NETWORK_BUDGET is None:
        return response.read(limit)
    remaining = NETWORK_BUDGET.deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkUnavailable('network_retry_budget')

    completed = threading.Event()
    result = []
    failure = []

    def read():
        try:
            result.append(response.read(limit))
        except BaseException as error:
            failure.append(error)
        finally:
            completed.set()

    threading.Thread(target=read, name='gpoint-ghcr-response-read', daemon=True).start()
    if not completed.wait(remaining):
        # Shutdown interrupts a socket read; close is a fallback for wrappers
        # whose response object does not expose urllib's underlying socket.
        try:
            sock = response.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            response.close()
        except (AttributeError, OSError):
            pass
        raise NetworkUnavailable('network_retry_budget')
    if time.monotonic() >= NETWORK_BUDGET.deadline:
        raise NetworkUnavailable('network_retry_budget')
    if failure:
        raise failure[0]
    return result[0]


def request(operation, stage):
    set_diagnostic_stage(stage)
    def failure_stage():
        return ACTIVE_DIAGNOSTIC_STAGE if ACTIVE_DIAGNOSTIC_STAGE.startswith(stage + '_') else stage
    try:
        return retry_network_read(operation, stage, retryable, budget=NETWORK_BUDGET)
    except NetworkUnavailable:
        raise NetworkStageError(failure_stage(), 'NETWORK_UNAVAILABLE', 'NETWORK') from None
    except urllib.error.HTTPError as error:
        category = exception_category(error)
        raise NetworkStageError(failure_stage(), 'HTTPError', category, error.code,
                                retryable=retryable(error)) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout,
            ConnectionError, ssl.SSLError) as error:
        raise NetworkStageError(failure_stage(), safe_exception_type(error), 'NETWORK',
                                retryable=retryable(error)) from None


def github(route, token, stage):
    url = f'https://api.github.com/repos/{REPO}/{route}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    def read():
        with network_urlopen(req, timeout=30) as response:
            payload = read_network_response(response, MAX_RECEIPT_BYTES + 1)
            if len(payload) > MAX_RECEIPT_BYTES:
                raise RuntimeError('GitHub response exceeds size limit')
            return json.loads(payload)
    return request(read, stage)


def validate_current_main_candidate(candidate_sha, release_mode, executor_sha, current_main_sha, is_ancestor):
    if not re.fullmatch(r'[a-f0-9]{40}', candidate_sha) or current_main_sha != candidate_sha:
        raise RuntimeError('Candidate is no longer current main')


def ghcr_request(url, username, password, accept, stage, *, limit=MAX_RECEIPT_BYTES):
    set_diagnostic_stage(stage)
    headers = {'Accept': accept}
    def get(extra_headers=None):
        req = urllib.request.Request(url, headers={**headers, **(extra_headers or {})})
        try:
            response = network_urlopen(req, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            set_diagnostic_stage(stage + '_auth_challenge')
            challenge = error.headers.get('WWW-Authenticate', '')
            error.close()
            match = re.fullmatch(r'Bearer realm="(https://ghcr\.io/token)",service="ghcr\.io",scope="repository:([A-Za-z0-9._/-]+):pull"', challenge)
            if not match:
                raise RuntimeError('GHCR returned an unexpected authentication challenge') from None
            realm, scope_repo = match.groups()
            path = urllib.parse.urlparse(url).path
            expected_repo = path.split('/v2/', 1)[1].split('/manifests/', 1)[0].split('/blobs/', 1)[0]
            if scope_repo != expected_repo:
                raise RuntimeError('GHCR scope does not match the receipt repository')
            token_url = realm + '?' + urllib.parse.urlencode({'service': 'ghcr.io', 'scope': f'repository:{scope_repo}:pull'})
            basic = base64.b64encode(f'{username}:{password}'.encode()).decode()
            token_req = urllib.request.Request(token_url, headers={
                'Authorization': f'Basic {basic}', 'Accept': 'application/json'})
            set_diagnostic_stage(stage + '_auth_token')
            try:
                with network_urlopen(token_req, timeout=30) as token_response:
                    token_bytes = read_network_response(token_response, 65537)
                    if len(token_bytes) > 65536:
                        raise RuntimeError('GHCR token response exceeds size limit')
                    bearer = json.loads(token_bytes).get('token')
            except urllib.error.HTTPError as token_error:
                raise NetworkStageError(stage + '_auth_token', 'HTTPError',
                                        exception_category(token_error), token_error.code,
                                        retryable=retryable(token_error)) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, ssl.SSLError) as token_error:
                raise NetworkStageError(stage + '_auth_token', safe_exception_type(token_error), 'NETWORK',
                                        retryable=retryable(token_error)) from None
            if not isinstance(bearer, str) or not re.fullmatch(r'[^\s\r\n]{1,16384}', bearer):
                raise RuntimeError('GHCR returned an invalid pull token')
            set_diagnostic_stage(stage + '_authenticated_read')
            response = network_urlopen(urllib.request.Request(url, headers={
                **headers, 'Authorization': f'Bearer {bearer}'}), timeout=30)
        with response:
            data = read_network_response(response, limit + 1)
            if len(data) > limit:
                raise RuntimeError('GHCR receipt response exceeds size limit')
            return data, response.headers
    return request(get, stage)


def fetch_oci_layer(repository, digest, expected_type, layer_media_type, username, token,
                    fetch=ghcr_request, limit=MAX_RECEIPT_BYTES):
    if not re.fullmatch(r'[A-Za-z0-9._/-]+', repository) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('Invalid OCI artifact descriptor')
    base = f'https://ghcr.io/v2/{repository}'
    manifest_bytes, headers = fetch(f'{base}/manifests/{digest}', username, token,
                                    f'{OCI_MANIFEST}, {OCI_INDEX}', 'ghcr_artifact_digest')
    if headers.get('Docker-Content-Digest') != digest or hashlib.sha256(manifest_bytes).hexdigest() != digest[7:]:
        raise RuntimeError('GHCR artifact manifest digest mismatch')
    manifest = json.loads(manifest_bytes)
    if manifest.get('mediaType') != OCI_MANIFEST or manifest.get('artifactType') != expected_type:
        raise RuntimeError('GHCR artifact type is invalid')
    layers = manifest.get('layers')
    if not isinstance(layers, list) or len(layers) != 1:
        raise RuntimeError('GHCR artifact must contain exactly one layer')
    layer = layers[0]
    blob_digest, size = layer.get('digest', ''), layer.get('size')
    if (layer.get('mediaType') != layer_media_type or not re.fullmatch(r'sha256:[a-f0-9]{64}', blob_digest)
            or not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > limit):
        raise RuntimeError('GHCR artifact layer descriptor is invalid')
    blob, _ = fetch(f'{base}/blobs/{blob_digest}', username, token, 'application/octet-stream',
                    'ghcr_artifact_blob', limit=limit)
    if len(blob) != size or hashlib.sha256(blob).hexdigest() != blob_digest[7:]:
        raise RuntimeError('GHCR artifact layer digest or size mismatch')
    return blob


def resolve_receipt(sha, run_id, attempt, username, token, fetch=ghcr_request):
    if (not re.fullmatch(r'[a-f0-9]{40}', sha) or not run_id.isdigit() or not attempt.isdigit()
            or not username or not token):
        raise RuntimeError('Invalid exact Prepare identity or GHCR credentials')
    tag = f'prepare-{sha}-{run_id}-{attempt}'
    base = f'https://ghcr.io/v2/{RECEIPT_REPOSITORY}'
    accepted = f'{OCI_MANIFEST}, {OCI_INDEX}'
    set_diagnostic_stage('ghcr_receipt_tag_manifest')
    tag_bytes, tag_headers = fetch(f'{base}/manifests/{tag}', username, token, accepted, 'ghcr_receipt_tag_manifest')
    digest = tag_headers.get('Docker-Content-Digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('GHCR did not return an immutable receipt digest')
    if hashlib.sha256(tag_bytes).hexdigest() != digest[7:]:
        raise RuntimeError('GHCR receipt manifest digest mismatch')
    # Re-fetch by digest to avoid using mutable tag content after resolution.
    set_diagnostic_stage('ghcr_receipt_digest_readback')
    manifest_bytes, manifest_headers = fetch(f'{base}/manifests/{digest}', username, token, accepted, 'ghcr_receipt_digest_readback')
    if manifest_headers.get('Docker-Content-Digest') != digest or manifest_bytes != tag_bytes:
        raise RuntimeError('GHCR receipt changed between tag resolution and digest read')
    set_diagnostic_stage('ghcr_receipt_layer_readback')
    blob = fetch_oci_layer(RECEIPT_REPOSITORY, digest, RECEIPT_MEDIA_TYPE, RECEIPT_LAYER_MEDIA_TYPE,
                           username, token, fetch=fetch)
    set_diagnostic_stage('ghcr_receipt_content_validation')
    try:
        receipt = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError('GHCR receipt layer is invalid JSON') from error
    canonical = json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    if canonical != blob:
        raise RuntimeError('GHCR receipt content encoding is not canonical')
    if (receipt.get('schema') not in (2, 3) or receipt.get('transport') != 'ghcr-oci'
            or receipt.get('status') != 'READY' or receipt.get('candidateSha') != sha
            or str(receipt.get('runId')) != run_id or str(receipt.get('runAttempt')) != attempt):
        raise RuntimeError('GHCR receipt source or Prepare identity mismatch')
    return receipt, digest, blob


def trusted_job_logs(run_id, attempt, token, job_name, step_name):
    payload = github(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100', token, 'prepare_job_identity')
    jobs = payload.get('jobs', [])
    matches = [job for job in jobs if job.get('name') == job_name and str(job.get('run_attempt')) == attempt
               and job.get('status') == 'completed' and job.get('conclusion') == 'success'
               and any(step.get('name') == step_name and step.get('conclusion') == 'success'
                       for step in job.get('steps', []))]
    if len(matches) != 1:
        raise RuntimeError('Exact trusted Prepare publisher job is missing or ambiguous')
    job_id = matches[0].get('id')
    if not isinstance(job_id, int) or job_id < 1:
        raise RuntimeError('Prepare publisher job identity is invalid')
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs', headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    def read():
        try:
            response = open_without_redirect(req, timeout=30)
        except urllib.error.HTTPError as redirect:
            if redirect.code != 302:
                raise
            location = redirect.headers.get('Location', '')
            redirect.close()
            location = urllib.parse.urljoin(req.full_url, location)
            if not trusted_log_storage_url(location):
                raise RuntimeError('Prepare publisher log redirect is invalid') from None
            # GitHub returns a short-lived signed storage URL. Follow only a
            # bounded chain on trusted HTTPS storage hosts; never forward the
            # GITHUB_TOKEN from api.github.com to any redirect target.
            for redirect_count in range(3):
                log_request = urllib.request.Request(location, headers={'Accept': 'text/plain'})
                try:
                    response = open_without_redirect(log_request, timeout=30)
                except urllib.error.HTTPError as storage_redirect:
                    next_location = storage_redirect.headers.get('Location', '')
                    code = storage_redirect.code
                    storage_redirect.close()
                    if code != 302 or redirect_count == 2:
                        raise
                    location = urllib.parse.urljoin(location, next_location)
                    if not trusted_log_storage_url(location):
                        raise RuntimeError('Prepare publisher log redirect is invalid') from None
                    continue
                with response:
                    raw = read_network_response(response, 25 * 1024 * 1024 + 1)
                break
            else:
                raise RuntimeError('Prepare publisher log redirect limit exceeded')
        else:
            with response:
                raw = read_network_response(response, 25 * 1024 * 1024 + 1)
        if len(raw) > 25 * 1024 * 1024:
            raise RuntimeError('Prepare publisher logs exceed size limit')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raise RuntimeError('Prepare publisher logs are not valid UTF-8') from None
    return request(read, 'prepare_publisher_logs')


def verify_logged_digest(logs, label, digest):
    matches = re.findall(rf'(?:^|\s){re.escape(label)}=(sha256:[a-f0-9]{{64}})(?:\s|$)', logs)
    if matches != [digest]:
        raise RuntimeError(f'{label} is not present in the exact trusted publisher job logs')


def main():
    global NETWORK_BUDGET, DIAGNOSTIC_IDENTITY, GHCR_READBACK_COMPLETED
    NETWORK_BUDGET = new_network_budget()
    started = time.monotonic()
    sha, run_id, attempt = (os.environ.get('CANDIDATE_SHA', ''), os.environ.get('PREPARE_RUN_ID', ''),
                            os.environ.get('PREPARE_RUN_ATTEMPT', ''))
    DIAGNOSTIC_IDENTITY = {
        'candidateSha': sha if re.fullmatch(r'[a-f0-9]{40}', sha) else '',
        'runId': run_id if run_id.isdigit() and len(run_id) <= 20 else '',
        'runAttempt': attempt if attempt.isdigit() and len(attempt) <= 6 else '',
    }
    token = os.environ.get('GITHUB_TOKEN', '')
    username, ghcr_token = os.environ.get('GHCR_READ_USERNAME', ''), os.environ.get('GHCR_READ_TOKEN', '')
    stage = 'prepare_run_metadata'
    set_diagnostic_stage(stage)
    run = github(f'actions/runs/{run_id}', token, stage)
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('path') != '.github/workflows/gpoint-prepare.yml'
            or str(run.get('run_attempt')) != attempt):
        raise RuntimeError('Successful Prepare provenance changed')
    stage = 'current_main_ref'
    set_diagnostic_stage(stage)
    current = github('git/ref/heads/main', token, stage)['object']['sha']
    validate_current_main_candidate(sha, os.environ.get('RELEASE_MODE', ''), '', current, lambda *_: False)
    stage = 'ghcr_receipt'
    set_diagnostic_stage(stage)
    receipt, digest, blob = resolve_receipt(sha, run_id, attempt, username, ghcr_token)
    GHCR_READBACK_COMPLETED = True
    set_diagnostic_stage('receipt_validation')
    expected_digest = os.environ.get('EXPECTED_PREPARE_RECEIPT_DIGEST', '').strip()
    expected_sha = os.environ.get('EXPECTED_PREPARE_RECEIPT_SHA256', '').strip()
    if bool(expected_digest) != bool(expected_sha):
        raise RuntimeError('Controller Prepare receipt digest and checksum must be paired')
    if expected_digest and (expected_digest != digest or expected_sha != hashlib.sha256(blob).hexdigest()):
        raise RuntimeError('GHCR Prepare receipt differs from controller-selected digest evidence')
    stage = 'prepare_publisher_logs'
    set_diagnostic_stage(stage)
    logs = trusted_job_logs(run_id, attempt, token, 'prepare', 'Publish and read back exact GHCR artifacts')
    verify_logged_digest(logs, 'GHCR_RECEIPT_DIGEST', digest)
    static_descriptor = receipt.get('staticArtifact')
    static_digest = ''
    if receipt.get('staticArtifactRoute') == 'V4_FAST_STATIC':
        if (receipt.get('route') != 'STATIC_UI_ONLY' or not isinstance(static_descriptor, dict)
                or static_descriptor.get('repository') != 'ghcr.io/akhripunov/gpoint-miniapp-artifacts'
                or static_descriptor.get('sourceSha') != sha
                or str(static_descriptor.get('runId')) != run_id
                or str(static_descriptor.get('runAttempt')) != attempt
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', static_descriptor.get('digest', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', static_descriptor.get('contentSha256', ''))):
            raise RuntimeError('V4 static OCI artifact identity is incomplete')
        static_digest = static_descriptor['digest']
        stage = 'static_artifact_readback'
        set_diagnostic_stage(stage)
        static_bytes = fetch_oci_layer('akhripunov/gpoint-miniapp-artifacts', static_digest,
                                       STATIC_ARTIFACT_TYPE, STATIC_LAYER_TYPE,
                                       username, ghcr_token, limit=MAX_STATIC_BYTES)
        if hashlib.sha256(static_bytes).hexdigest() != static_descriptor['contentSha256']:
            raise RuntimeError('V4 Mini App OCI content SHA-256 mismatch')
        verify_logged_digest(logs, 'GHCR_STATIC_ARTIFACT_DIGEST', static_digest)
    elif receipt.get('staticArtifactRoute') == 'V4_ADVISORY_ONLY':
        set_diagnostic_stage('receipt_validation')
        if receipt.get('route') != 'STATIC_UI_ONLY':
            raise RuntimeError('V4 advisory static route identity is invalid')
        static_bytes = None
    elif static_descriptor is not None:
        set_diagnostic_stage('receipt_validation')
        raise RuntimeError('Unsupported or ambiguous Mini App OCI artifact route')
    else:
        static_bytes = None
    destination = Path(os.environ['ARTIFACT_DESTINATION'])
    set_diagnostic_stage('artifact_write_destination')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = destination / 'receipt.json'
    if receipt_path.is_symlink():
        raise RuntimeError('Receipt destination must not be a symlink')
    set_diagnostic_stage('artifact_write_receipt')
    receipt_path.write_bytes(blob)
    if static_bytes is not None:
        static_path = destination / 'miniapp.zip'
        if static_path.is_symlink():
            raise RuntimeError('Mini App destination must not be a symlink')
        set_diagnostic_stage('artifact_write_static_artifact')
        static_path.write_bytes(static_bytes)
    evidence_path = destination / 'ghcr-evidence.json'
    if evidence_path.is_symlink():
        raise RuntimeError('GHCR evidence destination must not be a symlink')
    set_diagnostic_stage('artifact_write_evidence')
    evidence_path.write_text(json.dumps({
        'schema': 1, 'candidateSha': sha, 'runId': int(run_id), 'runAttempt': int(attempt),
        'manifestDigest': digest, 'receiptSha256': hashlib.sha256(blob).hexdigest(),
        'runLogProvenance': 'PASS', 'staticArtifactDigest': static_digest,
        'staticArtifactRunLogProvenance': 'PASS' if static_digest else None,
    }, sort_keys=True, separators=(',', ':')) + '\n')
    print(f'GHCR_RECEIPT_DIGEST={digest} SHA={sha} RUN={run_id} ATTEMPT={attempt}', flush=True)
    print('ARTIFACT_CACHE=GHCR_READBACK_OK CACHE_PREVENTED_DOWNLOAD=NO', flush=True)
    print('BUNDLE_DOWNLOAD_SECONDS=0.000', flush=True)
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        set_diagnostic_stage('artifact_write_github_output')
        with open(out, 'a', encoding='utf-8') as stream:
            stream.write('cache_prevented_download=NO\n')
            stream.write('bundle_download_seconds=0.000\n')
            stream.write(f'receipt_digest={digest}\n')
            stream.write(f'receipt_sha256={hashlib.sha256(blob).hexdigest()}\n')
    print(f'STAGE_SECONDS_GHCR_RECEIPT_TOTAL={time.monotonic() - started:.3f}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except NetworkStageError as error:
        emit_blocked_diagnostic(error)
        raise SystemExit(1)
    except Exception as error:
        emit_blocked_diagnostic(error)
        raise SystemExit(1)
