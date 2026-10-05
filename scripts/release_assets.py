#!/usr/bin/env python3
"""GPoint-only immutable Release assets, independent of Actions storage quota."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

REPO = 'akhripunov/gpoint'


def release_tag(kind, sha, run_id, attempt):
    if (kind not in ('prepare', 'evidence', 'rehearsal')
            or not re.fullmatch(r'[a-f0-9]{40}', sha)
            or not all(re.fullmatch(r'[1-9][0-9]*', str(x)) for x in (run_id, attempt))):
        raise RuntimeError('Invalid GPoint immutable release identity')
    return f'gpoint-{kind}-{sha}-{run_id}-{attempt}'


def resolve_asset(kind, sha, run_id, attempt, name, github):
    tag = release_tag(kind, sha, run_id, attempt)
    release = github(f'releases/tags/{tag}')
    if (release.get('tag_name') != tag or release.get('draft') is not False
            or release.get('immutable') is not True):
        raise RuntimeError('GPoint release is missing, draft or mutable')
    ref = github(f'git/ref/tags/{tag}')
    if ref.get('object', {}).get('type') != 'commit' or ref.get('object', {}).get('sha') != sha:
        raise RuntimeError('Immutable release tag does not match exact candidate')
    matches = [a for a in release.get('assets', []) if a.get('name') == name]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one immutable release asset')
    asset = matches[0]
    if (asset.get('state') != 'uploaded' or not isinstance(asset.get('size'), int)
            or asset['size'] <= 0 or asset['size'] >= 2 * 1024**3
            or not re.fullmatch(r'[1-9][0-9]*', str(asset.get('id', '')))
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', asset.get('digest', ''))):
        raise RuntimeError('Immutable release asset identity/digest is unavailable')
    return asset


def validate_result(data, sha, run_id, attempt):
    if (data.get('candidateSha') != sha or str(data.get('automationRunId')) != str(run_id)
            or str(data.get('automationRunAttempt')) != str(attempt)):
        raise RuntimeError('Immutable Deploy result provenance mismatch')


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True, timeout=600).strip()


def github(route):
    return json.loads(gh('api', f'repos/{REPO}/{route}'))


def write_zip(target, files):
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, source in files:
            if source.is_symlink() or not source.is_file():
                raise RuntimeError('Release input must be a regular file')
            archive.write(source, name)


def publish(kind, sha, run_id, attempt):
    if os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_SHA') != sha:
        raise RuntimeError('Only exact GPoint workflow source may publish assets')
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() != sha:
        raise RuntimeError('Release publisher checkout differs from candidate')
    tag = release_tag(kind, sha, run_id, attempt)
    with tempfile.TemporaryDirectory(prefix='gpoint-release-assets-') as temporary:
        root = Path(temporary)
        archives = []
        if kind in ('prepare', 'rehearsal'):
            bundle = Path(os.environ['RUNNER_TEMP']) / 'gpoint-bundle'
            names = ['images.tar.gz', 'receipt.json'] if kind == 'prepare' else ['receipt.json']
            receipt = json.loads((bundle / 'receipt.json').read_text())
            # The consumer still verifies the complete receipt and image parity.
            if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != run_id
                    or str(receipt.get('runAttempt')) != attempt or receipt.get('status') != 'READY'):
                raise RuntimeError('Prepare receipt candidate mismatch')
            target = root / ('bundle.zip' if kind == 'prepare' else 'receipt.zip')
            write_zip(target, [(name, bundle / name) for name in names])
            archives.append(target)
        else:
            result = Path(os.environ['RELEASE_RESULT'])
            if not result.is_file():
                print('RELEASE_EVIDENCE=NOT_CREATED (execution did not produce a result)')
                return
            data = json.loads(result.read_text())
            validate_result(data, sha, run_id, attempt)
            target = root / 'result.zip'
            write_zip(target, [(result.name, result)])
            archives.append(target)
            evidence = Path(os.environ['GPOINT_RELEASE_EVIDENCE'])
            files = [(str(p.relative_to(evidence)), p) for p in sorted(evidence.rglob('*.json'))]
            if files:
                target = root / 'manifests.zip'
                write_zip(target, files)
                archives.append(target)
        # New tag and new draft only: never overwrite or silently reuse a release.
        gh('api', '--method', 'POST', f'repos/{REPO}/git/refs',
           '-f', f'ref=refs/tags/{tag}', '-f', f'sha={sha}')
        gh('release', 'create', tag, '--repo', REPO, '--verify-tag', '--draft',
           '--title', tag, '--notes', f'Exact SHA {sha}; workflow run {run_id}, attempt {attempt}.')
        gh('release', 'upload', tag, '--repo', REPO, *map(str, archives))
        gh('release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest=false')
        for archive in archives:
            asset = resolve_asset(kind, sha, run_id, attempt, archive.name, github)
            from prepared_artifact_cache import checksum
            if asset['digest'] != 'sha256:' + checksum(archive):
                raise RuntimeError('Published immutable asset checksum differs from local archive')
        print(f'IMMUTABLE_RELEASE=PASS TAG={tag}')


def read_result(sha, run_id, attempt, result_path=None, token=None):
    from prepared_artifact_cache import download, extract, checksum
    asset = resolve_asset('evidence', sha, run_id, attempt, 'result.zip', github)
    result = Path(result_path or os.environ['RELEASE_RESULT'])
    token = token or os.environ['GITHUB_TOKEN']
    with tempfile.TemporaryDirectory(prefix='gpoint-result-') as temporary:
        archive = Path(temporary) / 'result.zip'
        download(REPO, asset['id'], token, archive, release_asset=True,
                 expected_size=asset.get('size'))
        if checksum(archive) != asset['digest'][7:]:
            raise RuntimeError('Immutable result ZIP checksum mismatch')
        name = f'gpoint-release-result-{run_id}-{attempt}.json'
        extracted = Path(temporary) / 'result'
        extract(archive, extracted, [name])
        validate_result(json.loads((extracted / name).read_text()), sha, run_id, attempt)
        result.parent.mkdir(parents=True, exist_ok=True)
        if result.is_symlink(): raise RuntimeError('Result destination must not be a symlink')
        result.write_bytes((extracted / name).read_bytes())


def main():
    sha = os.environ.get('CANDIDATE_SHA', os.environ.get('GITHUB_SHA', ''))
    run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    if sys.argv[1] == 'publish': publish(sys.argv[2], sha, run_id, attempt)
    elif sys.argv[1] == 'read-result': read_result(sha, run_id, attempt)
    else: raise RuntimeError('Unknown release asset command')


if __name__ == '__main__': main()
