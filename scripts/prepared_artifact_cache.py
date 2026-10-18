#!/usr/bin/env python3
"""Resolve and verify the exact Prepare OCI receipt from GHCR."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from network_retry import NetworkUnavailable, RetryBudget, retry_network_read

REPO = 'akhripunov/gpoint'
RECEIPT_REPOSITORY = 'akhripunov/gpoint-prepare-receipts'
RECEIPT_MEDIA_TYPE = 'application/vnd.gpoint.prepare.receipt.v1'
STATIC_ARTIFACT_TYPE = 'application/vnd.gpoint.miniapp.static.v1'
STATIC_LAYER_TYPE = 'application/vnd.gpoint.miniapp.static.v1+zip'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_STATIC_BYTES = 512 * 1024 * 1024
NETWORK_BUDGET = None


class NetworkStageError(RuntimeError):
    def __init__(self, stage, error_type):
        super().__init__(stage, error_type)
        self.stage, self.error_type = stage, error_type


def retryable(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 425, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, ssl.SSLError):
        return not any(marker in str(reason).lower() for marker in (
            'certificate_verify_failed', 'wrong_version_number', 'protocol_version'))
    return isinstance(reason, (TimeoutError, socket.timeout, socket.gaierror, ConnectionError))


def request(operation, stage):
    try:
        return retry_network_read(operation, stage, retryable, budget=NETWORK_BUDGET)
    except NetworkUnavailable:
        raise NetworkStageError(stage, 'NETWORK_UNAVAILABLE') from None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout,
            ConnectionError, ssl.SSLError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        raise NetworkStageError(stage, type(reason).__name__) from None


def github(route, token, stage):
    url = f'https://api.github.com/repos/{REPO}/{route}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    def read():
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read(MAX_RECEIPT_BYTES + 1)
            if len(payload) > MAX_RECEIPT_BYTES:
                raise RuntimeError('GitHub response exceeds size limit')
            return json.loads(payload)
    return request(read, stage)


def validate_current_main_candidate(candidate_sha, release_mode, executor_sha, current_main_sha, is_ancestor):
    if not re.fullmatch(r'[a-f0-9]{40}', candidate_sha) or current_main_sha != candidate_sha:
        raise RuntimeError('Candidate is no longer current main')


def ghcr_request(url, username, password, accept, stage, *, limit=MAX_RECEIPT_BYTES):
    headers = {'Accept': accept}
    def get(extra_headers=None):
        req = urllib.request.Request(url, headers={**headers, **(extra_headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
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
            with urllib.request.urlopen(token_req, timeout=30) as token_response:
                token_bytes = token_response.read(65537)
                if len(token_bytes) > 65536:
                    raise RuntimeError('GHCR token response exceeds size limit')
                bearer = json.loads(token_bytes).get('token')
            if not isinstance(bearer, str) or not re.fullmatch(r'[^\s\r\n]{1,16384}', bearer):
                raise RuntimeError('GHCR returned an invalid pull token')
            response = urllib.request.urlopen(urllib.request.Request(url, headers={
                **headers, 'Authorization': f'Bearer {bearer}'}), timeout=30)
        with response:
            data = response.read(limit + 1)
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
    tag_bytes, tag_headers = fetch(f'{base}/manifests/{tag}', username, token, accepted, 'ghcr_receipt_manifest')
    digest = tag_headers.get('Docker-Content-Digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('GHCR did not return an immutable receipt digest')
    if hashlib.sha256(tag_bytes).hexdigest() != digest[7:]:
        raise RuntimeError('GHCR receipt manifest digest mismatch')
    # Re-fetch by digest to avoid using mutable tag content after resolution.
    manifest_bytes, manifest_headers = fetch(f'{base}/manifests/{digest}', username, token, accepted, 'ghcr_receipt_digest')
    if manifest_headers.get('Docker-Content-Digest') != digest or manifest_bytes != tag_bytes:
        raise RuntimeError('GHCR receipt changed between tag resolution and digest read')
    blob = fetch_oci_layer(RECEIPT_REPOSITORY, digest, RECEIPT_MEDIA_TYPE, RECEIPT_MEDIA_TYPE,
                           username, token, fetch=fetch)
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
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(25 * 1024 * 1024 + 1)
            if len(raw) > 25 * 1024 * 1024:
                raise RuntimeError('Prepare publisher logs exceed size limit')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.namelist()) > 100:
                    raise RuntimeError('Prepare publisher log inventory exceeds limit')
                return '\n'.join(archive.read(name).decode('utf-8', errors='replace') for name in archive.namelist()
                                 if not name.endswith('/'))
    return request(read, 'prepare_publisher_logs')


def verify_logged_digest(logs, label, digest):
    matches = re.findall(rf'(?:^|\s){re.escape(label)}=(sha256:[a-f0-9]{{64}})(?:\s|$)', logs)
    if matches != [digest]:
        raise RuntimeError(f'{label} is not present in the exact trusted publisher job logs')


def main():
    global NETWORK_BUDGET
    NETWORK_BUDGET = RetryBudget()
    started = time.monotonic()
    sha, run_id, attempt = (os.environ.get('CANDIDATE_SHA', ''), os.environ.get('PREPARE_RUN_ID', ''),
                            os.environ.get('PREPARE_RUN_ATTEMPT', ''))
    token = os.environ.get('GITHUB_TOKEN', '')
    username, ghcr_token = os.environ.get('GHCR_READ_USERNAME', ''), os.environ.get('GHCR_READ_TOKEN', '')
    stage = 'prepare_run_metadata'
    run = github(f'actions/runs/{run_id}', token, stage)
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('path') != '.github/workflows/gpoint-prepare.yml'
            or str(run.get('run_attempt')) != attempt):
        raise RuntimeError('Successful Prepare provenance changed')
    stage = 'current_main_ref'
    current = github('git/ref/heads/main', token, stage)['object']['sha']
    validate_current_main_candidate(sha, os.environ.get('RELEASE_MODE', ''), '', current, lambda *_: False)
    stage = 'ghcr_receipt'
    receipt, digest, blob = resolve_receipt(sha, run_id, attempt, username, ghcr_token)
    expected_digest = os.environ.get('EXPECTED_PREPARE_RECEIPT_DIGEST', '').strip()
    expected_sha = os.environ.get('EXPECTED_PREPARE_RECEIPT_SHA256', '').strip()
    if bool(expected_digest) != bool(expected_sha):
        raise RuntimeError('Controller Prepare receipt digest and checksum must be paired')
    if expected_digest and (expected_digest != digest or expected_sha != hashlib.sha256(blob).hexdigest()):
        raise RuntimeError('GHCR Prepare receipt differs from controller-selected digest evidence')
    stage = 'prepare_publisher_logs'
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
        static_bytes = fetch_oci_layer('akhripunov/gpoint-miniapp-artifacts', static_digest,
                                       STATIC_ARTIFACT_TYPE, STATIC_LAYER_TYPE,
                                       username, ghcr_token, limit=MAX_STATIC_BYTES)
        if hashlib.sha256(static_bytes).hexdigest() != static_descriptor['contentSha256']:
            raise RuntimeError('V4 Mini App OCI content SHA-256 mismatch')
        verify_logged_digest(logs, 'GHCR_STATIC_ARTIFACT_DIGEST', static_digest)
    elif receipt.get('staticArtifactRoute') == 'V4_ADVISORY_ONLY':
        if receipt.get('route') != 'STATIC_UI_ONLY':
            raise RuntimeError('V4 advisory static route identity is invalid')
        static_bytes = None
    elif static_descriptor is not None:
        raise RuntimeError('Unsupported or ambiguous Mini App OCI artifact route')
    else:
        static_bytes = None
    destination = Path(os.environ['ARTIFACT_DESTINATION'])
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = destination / 'receipt.json'
    if receipt_path.is_symlink():
        raise RuntimeError('Receipt destination must not be a symlink')
    receipt_path.write_bytes(blob)
    if static_bytes is not None:
        static_path = destination / 'miniapp.zip'
        if static_path.is_symlink():
            raise RuntimeError('Mini App destination must not be a symlink')
        static_path.write_bytes(static_bytes)
    evidence_path = destination / 'ghcr-evidence.json'
    if evidence_path.is_symlink():
        raise RuntimeError('GHCR evidence destination must not be a symlink')
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
        print(f'ARTIFACT_CACHE=BLOCKED STAGE={error.stage} ERROR_TYPE={error.error_type}', flush=True)
        raise SystemExit(1)
    except Exception as error:
        print(f'ARTIFACT_CACHE=BLOCKED STAGE={locals().get("stage", "receipt_validation")} ERROR_TYPE={type(error).__name__}', flush=True)
        raise SystemExit(1)
