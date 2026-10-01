#!/usr/bin/env python3
"""Reuse an immutable Actions ZIP only after fresh GitHub provenance/digest checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


MAX_BYTES = 4 * 1024 ** 3


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def github(repo, route, token):
    request = urllib.request.Request(f'https://api.github.com/repos/{repo}/{route}', headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def download(repo, artifact_id, token, target):
    # The token must never follow GitHub's redirect to signed blob storage.
    request = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'})
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise RuntimeError(f'Artifact download HTTP {error.code}') from None
        location = error.headers.get('Location', '')
        if not location.startswith('https://'):
            raise RuntimeError('Artifact redirect must use HTTPS')
        response = urllib.request.urlopen(location, timeout=30)
    count = 0
    with response, target.open('wb') as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_BYTES:
                raise RuntimeError('Artifact exceeds the bounded cache size')
            output.write(chunk)


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


def materialize(cache, destination, artifact_id, digest, expected, fetch):
    if not re.fullmatch(r'[0-9]+', str(artifact_id)) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise RuntimeError('Invalid immutable artifact identity')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = cache / f'{artifact_id}-{digest}.zip'
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
    older = sorted((p for p in cache.glob('*.zip') if p != archive and not p.is_symlink()
                    and re.fullmatch(r'[0-9]+-[a-f0-9]{64}\.zip', p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    retained_bytes = archive.stat().st_size
    for index, path in enumerate(older):
        retained_bytes += path.stat().st_size
        if index >= 2 or retained_bytes > 1024 ** 3:
            path.unlink()
    return 'HIT' if hit else 'MISS'


def main():
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
    run = github(repo, f'actions/runs/{run_id}', token)
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('path') != f'.github/workflows/{workflow}'
            or str(run.get('run_attempt')) != attempt):
        raise RuntimeError('Successful Prepare provenance changed')
    if github(repo, 'git/ref/heads/main', token)['object']['sha'] != sha:
        raise RuntimeError('Candidate is no longer current main')
    metadata = github(repo, f'actions/artifacts/{artifact_id}', token)
    name = f'gpoint-release-{sha}-{run_id}-{attempt}' if project == 'gpoint' else f'maxibot-image-{sha}-{run_id}-{attempt}'
    if (metadata.get('expired') is not False or metadata.get('name') != name
            or str(metadata.get('workflow_run', {}).get('id')) != run_id
            or metadata.get('workflow_run', {}).get('head_sha') != sha):
        raise RuntimeError('Artifact does not belong to the selected Prepare')
    digest = metadata.get('digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('GitHub artifact digest is unavailable')
    cache = Path.home() / '.cache' / f'{project}-release' / 'artifacts'
    result = materialize(cache, Path(os.environ['ARTIFACT_DESTINATION']), artifact_id,
                         digest[7:], expected, lambda path: download(repo, artifact_id, token, path))
    print(f'ARTIFACT_CACHE={result} ARTIFACT_ID={artifact_id} SHA={sha}', flush=True)
    print(f'STAGE_SECONDS_ARTIFACT_DOWNLOAD={time.monotonic() - started:.3f}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Network exception strings may contain signed URLs; never print them.
        print(f'ARTIFACT_CACHE=BLOCKED ERROR_TYPE={type(error).__name__}', flush=True)
        raise SystemExit(1)
