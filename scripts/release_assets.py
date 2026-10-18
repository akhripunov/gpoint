#!/usr/bin/env python3
"""Read-only legacy Release evidence helpers; current releases use GHCR OCI artifacts."""
import json
import base64
import hashlib
import io
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import re
import random
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.error

REPO = 'akhripunov/gpoint'
# Actions accounts workflow outputs approximately as UTF-16. Keep this below
# half the 1 MiB per-job cap to leave room for output metadata.
ME38_INCIDENT_CANDIDATE = 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c'
ME38_INCIDENT_BASELINE = 'a700198a843c12a84f2e537178de752e3eddd54d'
EVIDENCE_API_TOTAL_BUDGET_SECONDS = 60
EVIDENCE_API_REQUEST_TIMEOUT_SECONDS = 20
EVIDENCE_API_MAX_RETRIES = 5
EVIDENCE_API_BACKOFF_CAP_SECONDS = 5


def _transient_github_read_category(error):
    """Only classify transport failures that are safe to retry for a GET."""
    if isinstance(error, urllib.error.HTTPError):
        return None
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(reason, socket.gaierror) and reason.errno == getattr(socket, 'EAI_AGAIN', None):
        return 'temporary_dns'
    if isinstance(reason, OSError) and reason.errno in {
            errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
            errno.EHOSTUNREACH, errno.ENETUNREACH}:
        return 'connection'
    return None


class _EvidenceApiClient:
    """Read immutable GitHub evidence with one bounded operation-wide deadline."""

    def __init__(self, token, *, budget_seconds=EVIDENCE_API_TOTAL_BUDGET_SECONDS,
                 request_timeout=EVIDENCE_API_REQUEST_TIMEOUT_SECONDS,
                 max_retries=EVIDENCE_API_MAX_RETRIES,
                 backoff_cap=EVIDENCE_API_BACKOFF_CAP_SECONDS,
                 opener=None, clock=None, sleep=None, jitter=None, emit=None):
        self.token = token
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.jitter = jitter or random.uniform
        self.opener = opener or urllib.request.urlopen
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.backoff_cap = backoff_cap
        self.deadline = self.clock() + budget_seconds
        self.emit = emit or print

    def __call__(self, route):
        retries = 0
        while True:
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise RuntimeError('GitHub immutable evidence read retry budget exhausted') from None
            request = urllib.request.Request(
                f'https://api.github.com/repos/{REPO}/{route}',
                headers={'Authorization': f'Bearer {self.token}',
                         'Accept': 'application/vnd.github+json'})
            try:
                with self.opener(request, timeout=min(self.request_timeout, remaining)) as response:
                    body = response.read()
                if self.clock() > self.deadline:
                    raise RuntimeError('GitHub immutable evidence read retry budget exhausted') from None
                return json.loads(body)
            except urllib.error.HTTPError as exc:
                if (exc.code == 404 and (route.startswith('releases/tags/')
                        or route.startswith('git/ref/tags/gpoint-recovery-diagnostics-')
                        or route.startswith('git/ref/tags/gpoint-evidence-'))):
                    return None
                raise
            except (urllib.error.URLError, OSError) as exc:
                category = _transient_github_read_category(exc)
                if category is None:
                    raise
                if retries >= self.max_retries:
                    raise RuntimeError('GitHub immutable evidence read retry limit exhausted') from None
                remaining = self.deadline - self.clock()
                if remaining <= 0:
                    raise RuntimeError('GitHub immutable evidence read retry budget exhausted') from None
                cap = min(self.backoff_cap, 2 ** min(retries, 6))
                delay = min(max(0.0, self.jitter(0.0, cap)), remaining)
                self.emit(f'GITHUB_API_READ_RETRY stage=immutable_evidence attempt={retries + 1} '
                          f'category={category} delay_s={delay:.1f}')
                retries += 1
                self.sleep(delay)


def _evidence_api_client():
    return _EvidenceApiClient(os.environ['GITHUB_TOKEN'])


def _api_for_operation(api):
    # Each top-level evidence import shares one deadline across all its GETs.
    # Defer token lookup until the first actual read so callers that replace
    # the reader do not need GitHub credentials for a fully mocked path.
    if api is not evidence_api:
        return api
    client = None

    def read(route):
        nonlocal client
        if client is None:
            client = _evidence_api_client()
        return client(route)

    return read


def release_tag(kind, sha, run_id, attempt):
    if (kind not in ('prepare', 'evidence', 'rehearsal')
            or not re.fullmatch(r'[a-f0-9]{40}', sha)
            or not all(re.fullmatch(r'[1-9][0-9]*', str(x)) for x in (run_id, attempt))):
        raise RuntimeError('Invalid GPoint immutable release identity')
    return f'gpoint-{kind}-{sha}-{run_id}-{attempt}'




def _stable_json_digest(value):
    def stable(item):
        if isinstance(item, list):
            return [stable(child) for child in item]
        if isinstance(item, dict):
            return {key: stable(item[key]) for key in sorted(item)}
        return item
    encoded = json.dumps(stable(value), ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()










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
    return _evidence_api_client()(route)


def find_deploy_evidence(sha, run_id, current_attempt, api=evidence_api):
    """Use the successful Deploy attempt, not a rerun of the publisher."""
    api = _api_for_operation(api)
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
    api = _api_for_operation(api)
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
        def exact_api(route):
            return release if route == f'releases/tags/{tag}' else api(route)

        asset = resolve_asset('prepare', sha, run_id, attempt, 'bundle.zip', exact_api)
        return attempt, asset
    raise RuntimeError('No immutable successful executed Prepare attempt')




def _download_immutable_asset(asset, target, token):
    from prepared_artifact_cache import download, checksum
    download(REPO, asset['id'], token, target, release_asset=True, expected_size=asset['size'])
    if checksum(target) != asset['digest'][7:]:
        raise RuntimeError('Immutable release asset checksum mismatch')






def validate_result(data, sha, run_id, attempt):
    if (data.get('candidateSha') != sha or str(data.get('automationRunId')) != str(run_id)
            or str(data.get('automationRunAttempt')) != str(attempt)):
        raise RuntimeError('Immutable Deploy result provenance mismatch')




class GitHubCliError(RuntimeError):
    def __init__(self, category, *, transient=False, http_status=None):
        self.category = category
        self.transient = transient
        self.http_status = http_status
        suffix = f' HTTP {http_status}' if http_status is not None else ''
        super().__init__(f'GitHub CLI request failed{suffix} ({category})')


def _gh_failure(error):
    detail = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else str(error.stderr or '')
    status = re.search(r'\bHTTP\s+(401|403|404|409|422)\b', detail, re.IGNORECASE)
    if status:
        return GitHubCliError('non-retryable', http_status=int(status.group(1)))
    lowered = detail.lower()
    transient = any(marker in lowered for marker in (
        'tls handshake timeout', 'i/o timeout', 'connection timed out',
        'connection reset by peer', 'connection aborted', 'unexpected eof',
        'eof occurred in violation of protocol'))
    if transient:
        category = 'tls_timeout' if 'tls handshake timeout' in lowered else 'temporary_network'
        return GitHubCliError(category, transient=True)
    return GitHubCliError('non-retryable')


def gh(*args):
    try:
        result = subprocess.run(['gh', *args], text=True, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise GitHubCliError('timeout', transient=True) from None
    if result.returncode != 0:
        raise _gh_failure(subprocess.CalledProcessError(result.returncode, ['gh', *args],
            output=result.stdout, stderr=result.stderr)) from None
    return result.stdout.strip()


def github(route):
    return json.loads(gh('api', f'repos/{REPO}/{route}'))


GITHUB_EVIDENCE_WRITE_ATTEMPTS = 3


def _optional_publish_read(api, route):
    try:
        return api(route)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('GitHub evidence read-back failed') from None
    except GitHubCliError as error:
        if error.http_status == 404:
            return None
        raise RuntimeError(f'GitHub evidence read-back failed ({error.category})') from None


def _is_transient_publish_error(error):
    if isinstance(error, GitHubCliError):
        return error.transient
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return False
    message = str(error).lower()
    if re.search(r'\bhttp\s+(401|403|404|409|422)\b', message):
        return False
    return any(marker in message for marker in (
        'tls handshake timeout', 'i/o timeout', 'connection timed out',
        'connection reset by peer', 'connection aborted', 'unexpected eof',
        'eof occurred in violation of protocol'))


def _write_evidence_with_readback(label, write, read, is_complete, is_conflict, *, sleep=None):
    sleep = sleep or time.sleep
    for attempt in range(GITHUB_EVIDENCE_WRITE_ATTEMPTS):
        before = read()
        if is_complete(before):
            return before
        if is_conflict(before):
            raise RuntimeError(f'{label} remote state conflicts')
        error = None
        try:
            write()
        except Exception as caught:
            error = caught
        after = read()
        if is_complete(after):
            return after
        if is_conflict(after):
            raise RuntimeError(f'{label} remote state conflicts') from None
        if error is None:
            raise RuntimeError(f'{label} write was not confirmed by remote read-back')
        if not _is_transient_publish_error(error):
            category = error.category if isinstance(error, GitHubCliError) else 'non-retryable'
            raise RuntimeError(f'{label} failed ({category})') from None
        if attempt + 1 == GITHUB_EVIDENCE_WRITE_ATTEMPTS:
            raise RuntimeError(f'{label} retry limit exhausted') from None
        sleep(min(2 ** attempt, 4))
    raise RuntimeError(f'{label} retry limit exhausted')




def verified_optional_v4_static_asset(receipt, bundle, sha):
    """Only accept standalone static asset explicitly attested by V3 receipt."""
    if 'staticArtifactSha256' not in receipt:
        if any(key in receipt for key in ('staticArtifactName', 'staticArtifactRoute')):
            raise RuntimeError('Partial V4 static artifact identity is prohibited')
        return None
    from release_ui_artifact_v4 import verify_bundle
    static = bundle / 'miniapp.zip'
    expected = receipt.get('staticArtifactSha256')
    expected_route = ('V4_FAST_STATIC' if receipt.get('schema') == 2 else 'V4_ADVISORY_ONLY')
    if (receipt.get('candidateSha') != sha or
            receipt.get('impactV3', {}).get('classification') != 'UI_ONLY' or
            receipt.get('staticArtifactName') != 'miniapp.zip' or
            receipt.get('staticArtifactRoute') != expected_route or
            not re.fullmatch(r'[a-f0-9]{64}', expected or '')):
        raise RuntimeError('V4 static candidate receipt scope or identity invalid')
    if (not static.is_file() or static.is_symlink() or
            hashlib.sha256(static.read_bytes()).hexdigest() != expected):
        raise RuntimeError('V4 static candidate archive mismatch')
    verify_bundle(static, sha)
    return static


def main():
    raise RuntimeError('GitHub Release asset operations are retired; use the GHCR-only GPoint workflow')


if __name__ == '__main__':
    main()
