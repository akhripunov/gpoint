#!/usr/bin/env python3
"""GPoint-only immutable Release assets, independent of Actions storage quota."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile
import urllib.request
import urllib.error

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


def evidence_api(route):
    """Keep 404 distinct from transient GitHub or authorization failures."""
    token = os.environ['GITHUB_TOKEN']
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/{route}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and route.startswith('releases/tags/'):
            return None
        raise


def find_deploy_evidence(sha, run_id, current_attempt, api=evidence_api):
    """Use the successful Deploy attempt, not a rerun of the publisher."""
    release_tag('evidence', sha, run_id, current_attempt)
    for num in range(int(current_attempt), 0, -1):
        attempt = str(num)
        tag = release_tag('evidence', sha, run_id, attempt)
        release = api(f'releases/tags/{tag}')
        if release is None:
            # A fresh unsuccessful Deploy must never silently borrow evidence
            # from an older successful attempt of the same workflow.
            missing_jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
            if not isinstance(missing_jobs, dict) or not isinstance(missing_jobs.get('jobs'), list):
                raise RuntimeError('Missing Deploy attempt job evidence')
            if missing_jobs.get('total_count', len(missing_jobs['jobs'])) > 100:
                raise RuntimeError('Incomplete Deploy attempt jobs')
            deploy_jobs = [job for job in missing_jobs['jobs'] if job.get('name') == 'deploy']
            if deploy_jobs:
                if len(deploy_jobs) != 1:
                    raise RuntimeError('Ambiguous Deploy job without immutable evidence')
                if (deploy_jobs[0].get('status') != 'completed'
                        or deploy_jobs[0].get('conclusion') != 'success'):
                    raise RuntimeError('Newer Deploy attempt has no immutable evidence')
                # GitHub's /attempts/N/jobs endpoint repeats previously
                # successful jobs as run_attempt=N with a new job ID. Their
                # actual started/completed timestamps precede rerun start.
                # A genuinely re-executed Deploy must produce new evidence.
                attempt_run = api(f'actions/runs/{run_id}/attempts/{attempt}')
                if not isinstance(attempt_run, dict) or str(attempt_run.get('run_attempt')) != attempt:
                    raise RuntimeError('Deploy attempt identity mismatch')
                try:
                    def timestamp(value):
                        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        if parsed.tzinfo is None:
                            raise ValueError('timezone is required')
                        return parsed.astimezone(timezone.utc)
                    started = timestamp(attempt_run['run_started_at'])
                    job = deploy_jobs[0]
                    carried = (job.get('status') == 'completed'
                               and job.get('conclusion') == 'success'
                               and job.get('head_sha') == sha
                               and timestamp(job['started_at']) < started
                               and timestamp(job['completed_at']) <= started)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise RuntimeError('Deploy job timing evidence invalid') from exc
                if not carried:
                    raise RuntimeError('Newer Deploy attempt has no immutable evidence')
            continue
        def exact_api(route):
            return release if route == f'releases/tags/{tag}' else api(route)
        asset = resolve_asset('evidence', sha, run_id, attempt, 'result.zip', exact_api)
        jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
        if (not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list)
                or jobs.get('total_count', len(jobs['jobs'])) > 100):
            raise RuntimeError('Incomplete Deploy jobs evidence')
        matched = [job for job in jobs['jobs'] if job.get('name') == 'deploy']
        if (len(matched) != 1 or matched[0].get('conclusion') != 'success'
                or matched[0].get('status') != 'completed'
                or matched[0].get('head_sha') != sha):
            raise RuntimeError('Immutable evidence without matching successful Deploy job')
        return attempt, asset
    raise RuntimeError('No immutable evidence for successful Deploy job')


def find_prepare_evidence(sha, run_id, current_attempt, api=evidence_api):
    """Resolve an immutable Prepare bundle from the actual executed job attempt."""
    release_tag('prepare', sha, run_id, current_attempt)
    def timestamp(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc)
    for number in range(int(current_attempt), 0, -1):
        attempt = str(number)
        tag = release_tag('prepare', sha, run_id, attempt)
        release = api(f'releases/tags/{tag}')
        details = api(f'actions/runs/{run_id}/attempts/{attempt}')
        jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
        if (not isinstance(details, dict) or str(details.get('run_attempt')) != attempt
                or not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list)
                or jobs.get('total_count', len(jobs['jobs'])) > 100):
            raise RuntimeError('Prepare attempt identity or jobs invalid')
        matched = [j for j in jobs['jobs'] if j.get('name') == 'prepare']
        if len(matched) != 1:
            raise RuntimeError('Prepare job identity ambiguous or missing')
        job = matched[0]
        if (job.get('status') != 'completed' or job.get('conclusion') != 'success'
                or job.get('head_sha') != sha):
            raise RuntimeError('Prepare job must be successful for exact SHA')
        try:
            attempt_start = timestamp(details['run_started_at'])
            actual_start = timestamp(job['started_at'])
            actual_end = timestamp(job['completed_at'])
            if actual_end < actual_start:
                raise ValueError('invalid job chronology')
        except (KeyError, AttributeError, ValueError, TypeError) as exc:
            raise RuntimeError('Prepare job chronology invalid') from exc
        if release is None:
            if actual_end <= attempt_start and actual_start < attempt_start:
                # GitHub repeats earlier successful jobs on publish-only reruns.
                continue
            raise RuntimeError('Executed Prepare has no immutable bundle')
        if actual_start < attempt_start:
            raise RuntimeError('Immutable Prepare tag does not match executed attempt')
        exact_api = lambda route: release if route == f'releases/tags/{tag}' else api(route)
        asset = resolve_asset('prepare', sha, run_id, attempt, 'bundle.zip', exact_api)
        return attempt, asset
    raise RuntimeError('No immutable successful executed Prepare attempt')


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


def build_oci_prepare_assets(bundle, output_dir):
    """Keep the normal Deploy asset small; retain the verified archive separately for fallback."""
    bundle, output_dir = Path(bundle), Path(output_dir)
    receipt_path = bundle / 'receipt.json'
    archive_path = bundle / 'images.tar.gz'
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('schema') != 3 or receipt.get('transport') != 'ghcr-oci'
            or not archive_path.is_file() or archive_path.is_symlink()):
        raise RuntimeError('OCI Prepare receipt or fallback archive is invalid')
    primary = output_dir / 'bundle.zip'
    fallback = output_dir / 'fallback-images.zip'
    write_zip(primary, [('receipt.json', receipt_path)])
    write_zip(fallback, [('images.tar.gz', archive_path), ('receipt.json', receipt_path)])
    return primary, fallback


def verified_optional_v4_static_asset(receipt, bundle, sha):
    """Only accept standalone static asset explicitly attested by V3 receipt."""
    if 'staticArtifactSha256' not in receipt:
        if any(key in receipt for key in ('staticArtifactName', 'staticArtifactRoute')):
            raise RuntimeError('Partial V4 static artifact identity is prohibited')
        return None
    from release_ui_artifact_v4 import verify_bundle
    from prepared_artifact_cache import checksum
    static = bundle / 'miniapp.zip'
    expected = receipt.get('staticArtifactSha256')
    expected_route = ('V4_FAST_STATIC' if receipt.get('schema') == 2 else 'V4_ADVISORY_ONLY')
    if (receipt.get('candidateSha') != sha or
            receipt.get('impactV3', {}).get('classification') != 'UI_ONLY' or
            receipt.get('staticArtifactName') != 'miniapp.zip' or
            receipt.get('staticArtifactRoute') != expected_route or
            not re.fullmatch(r'[a-f0-9]{64}', expected or '')):
        raise RuntimeError('V4 static candidate receipt scope or identity invalid')
    if not static.is_file() or static.is_symlink() or checksum(static) != expected:
        raise RuntimeError('V4 static candidate archive mismatch')
    verify_bundle(static, sha)
    return static


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
            if kind == 'prepare' and receipt.get('schema') == 2:
                if receipt.get('route') != 'STATIC_UI_ONLY':
                    raise RuntimeError('unknown V4 Prepare receipt route')
                static = verified_optional_v4_static_asset(receipt, bundle, sha)
                if static is None:
                    raise RuntimeError('V4 static bundle missing')
                names = ['miniapp.zip', 'receipt.json']
                target = root / 'bundle.zip'
                write_zip(target, [(name, bundle / name) for name in names])
                archives.append(target)
            elif kind == 'prepare' and receipt.get('schema') == 3:
                if receipt.get('transport') != 'ghcr-oci':
                    raise RuntimeError('unknown OCI Prepare receipt transport')
                archives.extend(build_oci_prepare_assets(bundle, root))
            else:
                target = root / ('bundle.zip' if kind == 'prepare' else 'receipt.zip')
                write_zip(target, [(name, bundle / name) for name in names])
                archives.append(target)
                if kind == 'prepare':
                    # Existing V3 advisory: an extra standalone static ZIP
                    # that does NOT change the legacy complete Docker bundle.
                    static = verified_optional_v4_static_asset(receipt, bundle, sha)
                    if static is not None:
                        archives.append(static)
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
    source_attempt, asset = find_deploy_evidence(sha, run_id, attempt)
    result = Path(result_path or os.environ['RELEASE_RESULT'])
    if result_path is None and os.environ.get('GITHUB_ENV'):
        result = result.with_name(f'gpoint-release-result-{run_id}-{source_attempt}.json')
    token = token or os.environ['GITHUB_TOKEN']
    with tempfile.TemporaryDirectory(prefix='gpoint-result-') as temporary:
        archive = Path(temporary) / 'result.zip'
        download(REPO, asset['id'], token, archive, release_asset=True,
                 expected_size=asset.get('size'))
        if checksum(archive) != asset['digest'][7:]:
            raise RuntimeError('Immutable result ZIP checksum mismatch')
        name = f'gpoint-release-result-{run_id}-{source_attempt}.json'
        extracted = Path(temporary) / 'result'
        extract(archive, extracted, [name])
        validate_result(json.loads((extracted / name).read_text()), sha, run_id, source_attempt)
        result.parent.mkdir(parents=True, exist_ok=True)
        if result.is_symlink(): raise RuntimeError('Result destination must not be a symlink')
        result.write_bytes((extracted / name).read_bytes())
        if result_path is None and os.environ.get('GITHUB_ENV'):
            with open(os.environ['GITHUB_ENV'], 'a') as stream:
                stream.write(f'RELEASE_RESULT={result}\n')
                stream.write(f'RELEASE_EVIDENCE_ATTEMPT={source_attempt}\n')
        print(f'RELEASE_EVIDENCE_ATTEMPT={source_attempt}')



def main():
    sha = os.environ.get('CANDIDATE_SHA', os.environ.get('GITHUB_SHA', ''))
    run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    if sys.argv[1] == 'publish': publish(sys.argv[2], sha, run_id, attempt)
    elif sys.argv[1] == 'read-result': read_result(sha, run_id, attempt)
    else: raise RuntimeError('Unknown release asset command')


if __name__ == '__main__': main()
