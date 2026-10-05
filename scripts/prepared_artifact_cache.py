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
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
from network_retry import NetworkUnavailable, RetryBudget, retry_network_read


MAX_BYTES = 4 * 1024 ** 3
NETWORK_BUDGET = None


class NetworkStageError(RuntimeError):
    def __init__(self, stage, error_type):
        super().__init__(stage, error_type)
        self.stage = stage
        self.error_type = error_type


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


def network_request(operation, stage, allow_redirect=False):
    def request_once():
        try:
            return operation()
        except urllib.error.HTTPError as error:
            # GitHub's signed ZIP URL is returned through this intentional redirect.
            if allow_redirect and error.code == 302:
                return error
            raise
    try:
        return retry_network_read(request_once, stage, retryable, budget=NETWORK_BUDGET)
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


def download(repo, artifact_id, token, target, release_asset=False):
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
        if {entry.filename for entry in entries} != set(expected) or len(entries) != len(expected):
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
    # Only our immutable ZIPs are evicted. Job files and other projects are untouched.
    older = sorted((p for p in cache.glob('v2-*.zip') if p != archive and not p.is_symlink()
                    and re.fullmatch(r'v2-[a-f0-9]{64}\.zip', p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    retained_bytes = archive.stat().st_size
    for index, path in enumerate(older):
        retained_bytes += path.stat().st_size
        if index >= 2 or retained_bytes > 1024 ** 3:
            path.unlink()
    return 'HIT' if hit else 'MISS'


def main():
    global NETWORK_BUDGET
    NETWORK_BUDGET = RetryBudget()
    started = time.monotonic()
    repo = os.environ['GITHUB_REPOSITORY']
    projects = {
        'akhripunov/gpoint': ('gpoint', 'gpoint-prepare.yml', ['images.tar.gz', 'receipt.json']),
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
    if github(repo, 'git/ref/heads/main', token, stage)['object']['sha'] != sha:
        raise RuntimeError('Candidate is no longer current main')
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
        download(repo, artifact_id, token, path, release_asset=project == 'gpoint')
        download_seconds[0] += time.monotonic() - before
    result = materialize(cache, Path(os.environ['ARTIFACT_DESTINATION']), artifact_id,
                         digest[7:], expected, timed_fetch, identity=identity)
    if project == 'gpoint':
        receipt = json.loads((Path(os.environ['ARTIFACT_DESTINATION']) / 'receipt.json').read_text())
        if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
                or str(receipt.get('runAttempt')) != attempt or receipt.get('status') != 'READY'):
            raise RuntimeError('Immutable Prepare receipt provenance mismatch')
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
