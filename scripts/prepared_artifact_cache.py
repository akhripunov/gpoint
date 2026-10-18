#!/usr/bin/env python3
"""Reuse an immutable Actions ZIP only after fresh GitHub provenance/digest checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parent))
from network_retry import NetworkUnavailable, RetryBudget, retry_network_read


MAX_BYTES = 4 * 1024 ** 3
NETWORK_BUDGET = None


class NetworkStageError(RuntimeError):
    def __init__(self, stage, error_type):
        super().__init__(stage, error_type)
        self.stage = stage
        self.error_type = error_type


class ReleaseAssetError(RuntimeError):
    pass


def validate_current_main_candidate(candidate_sha, release_mode, executor_sha, current_main_sha, is_ancestor):
    if current_main_sha != candidate_sha:
        raise RuntimeError('Candidate is no longer current main')


def retryable(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 425, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, ssl.SSLError):
        return not any(marker in str(reason).lower() for marker in (
            'certificate_verify_failed', 'wrong_version_number', 'protocol_version'))
    return isinstance(reason, (TimeoutError, socket.timeout, socket.gaierror,
                               ConnectionError))


def error_type(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return type(reason).__name__


def network_request(operation, stage, allow_redirect=False, budget=None):
    def request_once():
        try:
            return operation()
        except urllib.error.HTTPError as error:
            # GitHub's signed ZIP URL is returned through this intentional redirect.
            if allow_redirect and error.code == 302:
                return error
            raise
    try:
        return retry_network_read(request_once, stage, retryable, budget=budget or NETWORK_BUDGET)
    except Exception as error:
        if isinstance(error, NetworkUnavailable):
            raise NetworkStageError(stage, 'NETWORK_UNAVAILABLE') from None
        if isinstance(error, (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                              socket.timeout, ConnectionError, ssl.SSLError)):
            raise NetworkStageError(stage, error_type(error)) from None
        raise


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_ranges(size, target, fetch_range, *, workers=4, chunk_size=16 * 1024 * 1024):
    """Fetch independent byte ranges, assemble atomically, and reject gaps/overlaps."""
    if (not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_BYTES
            or workers < 1 or chunk_size < 1):
        raise ReleaseAssetError('Invalid ranged download bounds')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ranges = [(start, min(start + chunk_size, size) - 1)
              for start in range(0, size, chunk_size)]
    with tempfile.TemporaryDirectory(prefix='ranges-', dir=target.parent) as directory:
        temporary = Path(directory)
        parts = [(start, end, temporary / f'{index:06d}.part')
                 for index, (start, end) in enumerate(ranges)]

        def receive(item):
            start, end, path = item
            fetch_range(start, end, path)
            expected = end - start + 1
            if path.is_symlink() or not path.is_file() or path.stat().st_size != expected:
                raise ReleaseAssetError('Ranged download returned an incomplete chunk')

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(receive, parts))
        assembled = temporary / 'complete.zip'
        with assembled.open('wb') as output:
            for _start, _end, path in parts:
                with path.open('rb') as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        if assembled.stat().st_size != size:
            raise ReleaseAssetError('Ranged download assembled an unexpected length')
        os.replace(assembled, target)


def github(repo, route, token, stage):
    request = urllib.request.Request(f'https://api.github.com/repos/{repo}/{route}', headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    def fetch():
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    return network_request(fetch, stage)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def download(repo, artifact_id, token, target, release_asset=False, expected_size=None):
    if release_asset:
        if (not isinstance(expected_size, int) or isinstance(expected_size, bool)
                or not 0 < expected_size <= MAX_BYTES):
            raise ReleaseAssetError('Release asset size is unavailable or out of bounds')
        deadline = time.monotonic() + RetryBudget().seconds
        def fetch_range(start, end, path):
            expected_range = f'bytes {start}-{end}/{expected_size}'
            range_header = f'bytes={start}-{end}'
            def operation():
                request = urllib.request.Request(
                    f'https://api.github.com/repos/{repo}/releases/assets/{artifact_id}',
                    headers={'Authorization': f'Bearer {token}', 'Accept': 'application/octet-stream',
                             'X-GitHub-Api-Version': '2022-11-28', 'Range': range_header})
                try:
                    response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
                except urllib.error.HTTPError as error:
                    if error.code != 302:
                        raise
                    location = error.headers.get('Location', '')
                    if error.fp is not None:
                        error.close()
                    if not location.startswith('https://'):
                        raise ReleaseAssetError('Release asset redirect must use HTTPS')
                    response = urllib.request.urlopen(
                        urllib.request.Request(location, headers={'Range': range_header}), timeout=30)
                with response:
                    if response.getcode() != 206 or response.headers.get('Content-Range') != expected_range:
                        raise ReleaseAssetError('Release asset did not honor the requested byte range')
                    content_length = response.headers.get('Content-Length')
                    if content_length is not None and content_length != str(end - start + 1):
                        raise ReleaseAssetError('Release asset range length is inconsistent')
                    total = 0
                    with path.open('wb') as output:
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError('Release asset transfer deadline exceeded')
                            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                            if sock is not None:
                                sock.settimeout(min(30.0, remaining))
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > end - start + 1:
                                raise ReleaseAssetError('Release asset range exceeded its bounds')
                            output.write(chunk)
                            if time.monotonic() >= deadline:
                                raise TimeoutError('Release asset transfer deadline exceeded')
                    if total != end - start + 1:
                        raise ConnectionError('Release asset range was incomplete')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkStageError('release_asset_download', 'NETWORK_UNAVAILABLE')
            network_request(operation, 'release_asset_download', budget=RetryBudget(seconds=remaining))
        before = time.monotonic()
        download_ranges(expected_size, target, fetch_range, workers=4)
        elapsed = time.monotonic() - before
        if time.monotonic() >= deadline:
            raise NetworkStageError('release_asset_download', 'NETWORK_UNAVAILABLE')
        print(f'RELEASE_ASSET_RANGE_WORKERS=4 RELEASE_ASSET_RANGE_SECONDS={elapsed:.3f}', flush=True)
        return

    # Each bounded attempt obtains a fresh signed URL. Never forward the
    # GitHub token to blob storage or reuse a URL after a connection outage.
    route = f'releases/assets/{artifact_id}' if release_asset else f'actions/artifacts/{artifact_id}/zip'
    request = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/{route}',
        headers={'Authorization': f'Bearer {token}',
                 'Accept': 'application/octet-stream' if release_asset else 'application/vnd.github+json'})

    def copy_bounded(response):
        with response, target.open('wb') as output:
            count = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_BYTES:
                    raise RuntimeError('Artifact exceeds the bounded cache size')
                output.write(chunk)

    def fetch_storage():
        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 302:
                raise
            location = error.headers.get('Location', '')
            if error.fp is not None:
                error.close()
            if not location.startswith('https://'):
                raise RuntimeError('Artifact redirect must use HTTPS')
            copy_bounded(urllib.request.urlopen(location, timeout=30))
        else:
            copy_bounded(response)

    # One retry scope/budget covers redirect plus transfer; no nested charges.
    network_request(fetch_storage, 'artifact_zip_storage')

def extract(archive, destination, expected):
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        allowed = (expected if expected and isinstance(expected[0], (list, tuple))
                   else [expected])
        members = {entry.filename for entry in entries}
        if not any(members == set(choice) and len(entries) == len(choice)
                   for choice in allowed):
            raise RuntimeError('Unexpected artifact members')
        if sum(entry.file_size for entry in entries) > MAX_BYTES:
            raise RuntimeError('Expanded artifact exceeds size limit')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
                raise RuntimeError('Artifact members must be regular files')
        destination.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = destination / entry.filename
            if target.is_symlink():
                raise RuntimeError('Artifact destination must not be a symlink')
            with source.open(entry) as incoming, target.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def materialize(cache, destination, artifact_id, digest, expected, fetch, identity=None):
    if not re.fullmatch(r'[0-9]+', str(artifact_id)) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise RuntimeError('Invalid immutable artifact identity')
    identity = identity or {'asset_id': str(artifact_id), 'asset_sha256': digest}
    identity_key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = cache / f'v2-{identity_key}.zip'
    hit = archive.is_file() and not archive.is_symlink() and checksum(archive) == digest
    if not hit:
        with tempfile.TemporaryDirectory(prefix='incoming-', dir=cache) as temporary:
            pending = Path(temporary) / 'artifact.zip'
            fetch(pending)
            if checksum(pending) != digest:
                raise RuntimeError('GitHub artifact ZIP checksum mismatch')
            # Validate members before admitting downloaded bytes to the cache.
            extract(pending, destination, expected)
            pending.replace(archive)
    else:
        extract(archive, destination, expected)
    archive.touch()
    # Retain only this immutable bundle. Older V2 and legacy release ZIPs are
    # reproducible downloads and can consume the space needed to inspect/load
    # the current image. Never touch files outside this project cache or files
    # whose names do not match a known release-asset identity.
    release_zip = re.compile(r'(?:v2-[a-f0-9]{64}|[0-9]+-[a-f0-9]{64})\.zip')
    for path in cache.iterdir():
        if path != archive and not path.is_symlink() and path.is_file() and release_zip.fullmatch(path.name):
            path.unlink()
    return 'HIT' if hit else 'MISS'


def verify_and_install_archive_fallback(zip_path, destination, expected_receipt):
    """Accept only the separate immutable, receipt-matched fallback asset."""
    zip_path, destination = Path(zip_path), Path(destination)
    if zip_path.is_symlink() or not zip_path.is_file():
        raise RuntimeError('Fallback archive must be a regular file')
    with zipfile.ZipFile(zip_path) as source:
        entries = source.infolist()
        if ({entry.filename for entry in entries} != {'images.tar.gz', 'receipt.json'}
                or len(entries) != 2 or any(stat.S_ISLNK(e.external_attr >> 16) or e.is_dir() for e in entries)
                or sum(entry.file_size for entry in entries) > MAX_BYTES):
            raise RuntimeError('Unexpected fallback archive members')
        receipt_info = next(entry for entry in entries if entry.filename == 'receipt.json')
        if receipt_info.file_size > 1024 * 1024:
            raise RuntimeError('Fallback receipt exceeds its size limit')
        with source.open('receipt.json') as receipt_file:
            receipt_bytes = receipt_file.read(1024 * 1024 + 1)
    try:
        receipt = json.loads(receipt_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError('Fallback receipt is invalid') from exc
    if receipt != expected_receipt or receipt.get('schema') != 3:
        raise RuntimeError('Fallback receipt differs from selected immutable Prepare receipt')
    target = destination / 'images.tar.gz'
    if target.is_symlink():
        raise RuntimeError('Fallback destination must not be a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    temporary = None
    digest = hashlib.sha256()
    total = 0
    try:
        with tempfile.NamedTemporaryFile(prefix='.images.tar.gz.', dir=destination,
                                         delete=False) as output:
            temporary = Path(output.name)
            with zipfile.ZipFile(zip_path) as source, source.open('images.tar.gz') as image_file:
                while True:
                    chunk = image_file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError('Fallback image archive exceeds its size limit')
                    digest.update(chunk)
                    output.write(chunk)
        if total != next(entry.file_size for entry in entries if entry.filename == 'images.tar.gz'):
            raise RuntimeError('Fallback image archive length is inconsistent')
        if digest.hexdigest() != receipt.get('archiveSha256'):
            raise RuntimeError('Fallback image archive checksum mismatch')
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def main():
    global NETWORK_BUDGET
    NETWORK_BUDGET = RetryBudget()
    started = time.monotonic()
    repo = os.environ['GITHUB_REPOSITORY']
    projects = {
        'akhripunov/gpoint': ('gpoint', 'gpoint-prepare.yml',
                             [['receipt.json'], ['images.tar.gz', 'receipt.json'], ['miniapp.zip', 'receipt.json']]),
        'akhripunov/maxibot': ('maxibot', 'maxibot-prepare.yml', [
            f"maxibot-image-{os.environ['CANDIDATE_SHA']}.tar.gz",
            'maxibot-image-manifest.json', 'maxibot-prepare-result.json']),
    }
    project, workflow, expected = projects[repo]
    sha, run_id = os.environ['CANDIDATE_SHA'], os.environ['PREPARE_RUN_ID']
    attempt, artifact_id = os.environ['PREPARE_RUN_ATTEMPT'], os.environ['PREPARE_ARTIFACT_ID']
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or not all(x.isdigit() for x in (run_id, attempt, artifact_id)):
        raise RuntimeError('Invalid release identity')
    token = os.environ['GITHUB_TOKEN']
    stage = 'prepare_run_metadata'
    run = github(repo, f'actions/runs/{run_id}', token, stage)
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('path') != f'.github/workflows/{workflow}'
            or str(run.get('run_attempt')) != attempt):
        raise RuntimeError('Successful Prepare provenance changed')
    stage = 'current_main_ref'
    current_main_sha = github(repo, 'git/ref/heads/main', token, stage)['object']['sha']
    release_mode = os.environ.get('RELEASE_MODE', '')
    executor_sha = os.environ.get('GPOINT_RECOVERY_EXECUTOR_SHA', '')
    validate_current_main_candidate(sha, release_mode, executor_sha, current_main_sha,
        lambda candidate, executor: subprocess.run(
            ['git', 'merge-base', '--is-ancestor', candidate, executor],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    stage = 'artifact_metadata'
    if project == 'gpoint':
        from release_assets import resolve_asset
        metadata = resolve_asset('prepare', sha, run_id, attempt, 'bundle.zip',
                                 lambda route: github(repo, route, token, stage))
        if str(metadata['id']) != artifact_id:
            raise RuntimeError('Immutable asset changed after selection')
    else:
        metadata = github(repo, f'actions/artifacts/{artifact_id}', token, stage)
        name = f'gpoint-release-{sha}-{run_id}-{attempt}' if project == 'gpoint' else f'maxibot-image-{sha}-{run_id}-{attempt}'
        if (metadata.get('expired') is not False or metadata.get('name') != name
                or str(metadata.get('workflow_run', {}).get('id')) != run_id
                or metadata.get('workflow_run', {}).get('head_sha') != sha):
            raise RuntimeError('Artifact does not belong to the selected Prepare')
    digest = metadata.get('digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('GitHub artifact digest is unavailable')
    cache = Path.home() / '.cache' / f'{project}-release' / 'artifacts'
    stage = 'artifact_cache_or_download'
    release_tag = f'gpoint-prepare-{sha}-{run_id}-{attempt}' if project == 'gpoint' else ''
    identity = {'project': project, 'candidate_sha': sha, 'release_tag': release_tag,
                'prepare_run_id': run_id, 'prepare_run_attempt': attempt,
                'asset_id': artifact_id, 'asset_sha256': digest[7:]}
    download_seconds = [0.0]
    def timed_fetch(path):
        before = time.monotonic()
        download(repo, artifact_id, token, path, release_asset=project == 'gpoint',
                 expected_size=metadata.get('size') if project == 'gpoint' else None)
        download_seconds[0] += time.monotonic() - before
    result = materialize(cache, Path(os.environ['ARTIFACT_DESTINATION']), artifact_id,
                         digest[7:], expected, timed_fetch, identity=identity)
    if project == 'gpoint':
        receipt = json.loads((Path(os.environ['ARTIFACT_DESTINATION']) / 'receipt.json').read_text())
        if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
                or str(receipt.get('runAttempt')) != attempt or receipt.get('status') != 'READY'):
            raise RuntimeError('Immutable Prepare receipt provenance mismatch')
        requested_transport = os.environ.get('IMAGE_TRANSPORT', 'prepared')
        if requested_transport not in {'prepared', 'ghcr-oci', 'archive'}:
            raise RuntimeError('Requested image transport is invalid')
        if requested_transport == 'ghcr-oci' and receipt.get('transport') != 'ghcr-oci':
            raise RuntimeError('GHCR transport is not proven by the Prepare receipt')
        if requested_transport == 'archive' and receipt.get('schema') == 3:
            from release_assets import resolve_asset
            fallback = resolve_asset('prepare', sha, run_id, attempt, 'fallback-images.zip',
                                     lambda route: github(repo, route, token, 'fallback_asset_metadata'))
            with tempfile.TemporaryDirectory(prefix='gpoint-fallback-') as temporary:
                fallback_zip = Path(temporary) / 'fallback-images.zip'
                download(repo, fallback['id'], token, fallback_zip, release_asset=True,
                         expected_size=fallback['size'])
                if checksum(fallback_zip) != fallback['digest'][7:]:
                    raise RuntimeError('Immutable fallback asset checksum mismatch')
                verify_and_install_archive_fallback(fallback_zip,
                    Path(os.environ['ARTIFACT_DESTINATION']), receipt)
        elif requested_transport == 'archive' and receipt.get('schema') != 3:
            if not (Path(os.environ['ARTIFACT_DESTINATION']) / 'images.tar.gz').is_file():
                raise RuntimeError('Prepared archive transport is unavailable')
    print(f'ARTIFACT_CACHE={result} ARTIFACT_ID={artifact_id} SHA={sha}', flush=True)
    print(f'BUNDLE_DOWNLOAD_SECONDS={download_seconds[0]:.3f}', flush=True)
    prevented = 'YES' if result == 'HIT' else 'NO'
    print(f'CACHE_PREVENTED_DOWNLOAD={prevented}', flush=True)
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'cache_prevented_download={prevented}\n')
            stream.write(f'bundle_download_seconds={download_seconds[0]:.3f}\n')
    print(f'STAGE_SECONDS_ARTIFACT_CACHE_TOTAL={time.monotonic() - started:.3f}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except NetworkStageError as error:
        print(f'ARTIFACT_CACHE=BLOCKED STAGE={error.stage} ERROR_TYPE={error.error_type}', flush=True)
        raise SystemExit(1)
    except Exception as error:
        # Network exception strings may contain signed URLs; never print them.
        print(f'ARTIFACT_CACHE=BLOCKED STAGE={locals().get("stage", "artifact_validation")} ERROR_TYPE={type(error).__name__}', flush=True)
        raise SystemExit(1)
