#!/usr/bin/env python3
"""GPoint-only immutable Release assets, independent of Actions storage quota."""
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
import tempfile
import time
import zipfile
import urllib.request
import urllib.error

REPO = 'akhripunov/gpoint'
RECOVERY_IMAGE_REPOSITORIES = {
    'app': 'ghcr.io/akhripunov/gpoint-runtime',
    'agata': 'ghcr.io/akhripunov/gpoint-agata-runtime',
    'gsales': 'ghcr.io/akhripunov/gpoint-g-sales-runtime',
}
MAX_FAILURE_MANIFEST_MEMBERS = 4096
MAX_FAILURE_MANIFEST_MEMBER_BYTES = 32 * 1024**2
MAX_FAILURE_MANIFEST_TOTAL_BYTES = 2 * 1024**3
# Actions accounts workflow outputs approximately as UTF-16. Keep this below
# half the 1 MiB per-job cap to leave room for output metadata.
MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES = 192 * 1024
MAX_RECOVERY_DIAGNOSTIC_OUTPUT_BYTES = 256 * 1024
MAX_RECOVERY_DIAGNOSTIC_OUTPUT_UTF16_BYTES = 512 * 1024
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
                        or route.startswith('git/ref/tags/gpoint-recovery-diagnostics-'))):
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


def recovery_diagnostics_tag(executor_sha, run_id, attempt):
    if (not re.fullmatch(r'[a-f0-9]{40}', executor_sha or '')
            or not re.fullmatch(r'[1-9][0-9]*', str(run_id))
            or not re.fullmatch(r'[1-9][0-9]*', str(attempt))):
        raise RuntimeError('Invalid recovery diagnostic Release identity')
    return f'gpoint-recovery-diagnostics-{executor_sha}-{run_id}-{attempt}'


def _stable_json_digest(value):
    def stable(item):
        if isinstance(item, list):
            return [stable(child) for child in item]
        if isinstance(item, dict):
            return {key: stable(item[key]) for key in sorted(item)}
        return item
    encoded = json.dumps(stable(value), ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def recovery_snapshot_digest(snapshot):
    body = dict(snapshot)
    supplied = body.pop('snapshotSha256', None)
    digest = _stable_json_digest(body)
    if supplied != digest:
        raise RuntimeError('Recovery snapshot SHA-256 verification failed')
    return digest


def _validate_recovery_snapshot_semantics(snapshot):
    missing, blockers, checks = (snapshot.get('missingEvidence'), snapshot.get('blockers'),
                                 snapshot.get('checks'))
    if (snapshot.get('releaseState') != 'RECOVERY_DIAGNOSTICS_ONLY'
            or not isinstance(missing, list)
            or any(not isinstance(item, str) or not item for item in missing)
            or len(set(missing)) != len(missing)
            or not isinstance(blockers, list)
            or any(not isinstance(item, str) or not item for item in blockers)
            or not isinstance(checks, dict)):
        raise RuntimeError('Recovery snapshot semantic fields are invalid')
    allowed_missing = {'snapshot.observedAt', 'ledger.observedAt', 'ledger.headSha',
        'ledger.productionSha', 'ledger.stateSha256', 'ledger.refTargetSha', 'runtime.observedAt',
        'runtime.releaseEnvSha', 'runtime.miniappSha', 'runtime.databaseReadOnlyQueryExitCode',
        *(f'runtime.services.{name}' for name in ('api', 'worker', 'bot', 'agata-api',
            'agata-memory-worker', 'gsales-api')),
        'runtime.migrationHeads.prisma', 'runtime.migrationHeads.agata',
        'runtime.migrationHeads.gsales', 'runtime.migrationHeads.gsalesMigrationSha256',
        'workers.observedAt', 'workers.lanes.GPOINT', 'workers.lanes.GSALES',
        'backup.status', 'backup.integrity', 'locks', 'productionTag', 'incidentEvidence'}
    allowed_blockers = {'RUNTIME_IMAGE_SOURCE_PARITY_FAILED', 'MINIAPP_SOURCE_PARITY_FAILED',
        'PRODUCTION_SERVICE_NOT_HEALTHY', 'WORKER_LANES_NOT_VERIFIED_HEALTHY',
        'PROTECTED_BACKUP_MISSING', 'PROTECTED_BACKUP_UNVERIFIED', 'RELEASE_LOCK_CONFLICT',
        'PRODUCTION_TAG_BASELINE_NOT_VERIFIED', 'CANDIDATE_PRODUCTION_TAG_CONFLICT',
        'READ_ONLY_SNAPSHOT_INCOMPLETE', 'FINALIZATION_REQUIRES_SEPARATE_OWNER_AUTHORIZATION'}
    if not set(missing).issubset(allowed_missing) or not set(blockers).issubset(allowed_blockers):
        raise RuntimeError('Recovery snapshot contains unknown evidence or blocker fields')
    complete = snapshot.get('factsComplete')
    if (complete is not (len(missing) == 0)
            or snapshot.get('status') != ('FACTS_COMPLETE' if complete else 'INCOMPLETE')):
        raise RuntimeError('Recovery snapshot completeness fields are inconsistent')
    expected = {'incidentEvidence', 'ledger', 'runtime', 'database', 'workers', 'backup',
                'locks', 'productionTag', 'mutationBoundary', 'finalization'}
    states = {'PASS', 'BLOCKED', 'NOT_VERIFIED'}
    if (set(checks) != expected
            or any(not isinstance(value, str) or value not in states for value in checks.values())):
        raise RuntimeError('Recovery snapshot check statuses are invalid')
    def status(prefixes=(), blocker_codes=()):
        if prefixes and any(item.startswith(prefixes) for item in missing):
            return 'NOT_VERIFIED'
        if any(item in blocker_codes for item in blockers):
            return 'BLOCKED'
        return 'PASS'
    backup_status = snapshot.get('backup', {}).get('status')
    expected_checks = {
        'incidentEvidence': status(('incidentEvidence',)),
        'ledger': status(('ledger.',)),
        'runtime': status(('runtime.',), ('RUNTIME_IMAGE_SOURCE_PARITY_FAILED',
            'MINIAPP_SOURCE_PARITY_FAILED', 'PRODUCTION_SERVICE_NOT_HEALTHY')),
        'database': status(('runtime.database', 'runtime.migrationHeads.')),
        'workers': status(('workers.',), ('WORKER_LANES_NOT_VERIFIED_HEALTHY',)),
        'backup': ('NOT_VERIFIED' if backup_status not in ('VERIFIED', 'MISSING', 'UNVERIFIED')
                   else status(('backup.',)) if backup_status == 'VERIFIED' else 'BLOCKED'),
        'locks': status(('locks.',), ('RELEASE_LOCK_CONFLICT',)),
        'productionTag': status(('productionTag.',), ('PRODUCTION_TAG_BASELINE_NOT_VERIFIED',
            'CANDIDATE_PRODUCTION_TAG_CONFLICT')),
        'mutationBoundary': 'PASS',
        'finalization': 'BLOCKED',
    }
    blocker_checks = {
        'RUNTIME_IMAGE_SOURCE_PARITY_FAILED': 'runtime',
        'MINIAPP_SOURCE_PARITY_FAILED': 'runtime',
        'PRODUCTION_SERVICE_NOT_HEALTHY': 'runtime',
        'WORKER_LANES_NOT_VERIFIED_HEALTHY': 'workers',
        'PROTECTED_BACKUP_MISSING': 'backup',
        'PROTECTED_BACKUP_UNVERIFIED': 'backup',
        'RELEASE_LOCK_CONFLICT': 'locks',
        'PRODUCTION_TAG_BASELINE_NOT_VERIFIED': 'productionTag',
        'CANDIDATE_PRODUCTION_TAG_CONFLICT': 'productionTag',
    }
    if (checks != expected_checks
            or 'FINALIZATION_REQUIRES_SEPARATE_OWNER_AUTHORIZATION' not in blockers
            or (bool(missing) != ('READ_ONLY_SNAPSHOT_INCOMPLETE' in blockers))
            or any(checks[blocker_checks[item]] not in ('BLOCKED', 'NOT_VERIFIED')
                   for item in blockers if item in blocker_checks)):
        raise RuntimeError('Recovery snapshot check statuses contradict its evidence')
    mutation = snapshot.get('mutationPolicy')
    if (not isinstance(mutation, dict)
            or mutation.get('allowed') != ['read_only_observation', 'write_diagnostic_snapshot_artifact']):
        raise RuntimeError('Recovery diagnostic mutation policy is not read-only')


def build_recovery_diagnostics_bundle(snapshot_path, result_path, executor_sha, run_id, attempt):
    """Validate and prepare the small, provenance-bound handoff to the isolated publisher."""
    tag = recovery_diagnostics_tag(executor_sha, run_id, attempt)
    snapshot_path, result_path = Path(snapshot_path), Path(result_path)
    if any(path.is_symlink() or not path.is_file() for path in (snapshot_path, result_path)):
        raise RuntimeError('Recovery diagnostic inputs must be regular files')
    snapshot_bytes, result_bytes = snapshot_path.read_bytes(), result_path.read_bytes()
    if len(snapshot_bytes) + len(result_bytes) > MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES:
        raise RuntimeError('Recovery diagnostic bundle exceeds the bounded workflow-output size')
    snapshot, result = json.loads(snapshot_bytes), json.loads(result_bytes)
    recovery_snapshot_digest(snapshot)
    _validate_recovery_snapshot_semantics(snapshot)
    incident = snapshot.get('incident', {})
    forbidden = set(snapshot.get('mutationPolicy', {}).get('forbidden', []))
    required_forbidden = {'apply', 'migration', 'rollback', 'runtime_switch', 'tag_write',
                          'ledger_write', 'config_write', 'restart'}
    if (snapshot.get('schema') != 'gpoint-recovery-read-only-snapshot-v1'
            or snapshot.get('mode') != 'recovery-diagnostics-read-only'
            or snapshot.get('status') not in ('FACTS_COMPLETE', 'INCOMPLETE')
            or snapshot.get('finalizationReady') is not False
            or not required_forbidden.issubset(forbidden)):
        raise RuntimeError('Recovery snapshot schema, status or mutation policy is invalid')
    candidate = snapshot.get('candidateSha')
    original_run, original_attempt = '39010750184', '1'
    if (candidate != ME38_INCIDENT_CANDIDATE or incident.get('baselineSha') != ME38_INCIDENT_BASELINE
            or candidate == executor_sha
            or incident.get('candidateSha') != candidate
            or incident.get('runId') != original_run or str(incident.get('attempt')) != original_attempt
            or not re.fullmatch(r'gpoint-release-\d{8}T\d{9}Z', incident.get('releaseId', ''))):
        raise RuntimeError('Recovery snapshot incident or executor identity is inconsistent')
    if (result.get('mode') != 'recovery-diagnostics' or result.get('status') != snapshot.get('status')
            or result.get('candidateSha') != candidate
            or str(result.get('automationRunId')) != str(run_id)
            or str(result.get('automationRunAttempt')) != str(attempt)
            or result.get('recoverySnapshot') != snapshot):
        raise RuntimeError('Recovery diagnostic result does not match snapshot and workflow attempt')
    manifest = {
        'schema': 'gpoint-recovery-diagnostics-release-v1',
        'releaseId': tag,
        'eventType': 'RECOVERY_READ_ONLY_DIAGNOSTICS',
        'status': snapshot['status'],
        'executorSha': executor_sha,
        'candidateSha': candidate,
        'workflowRunId': str(run_id),
        'workflowRunAttempt': str(attempt),
        'originalIncidentRunId': original_run,
        'originalIncidentRunAttempt': original_attempt,
        'originalIncidentReleaseId': incident['releaseId'],
        'snapshotSha256': snapshot['snapshotSha256'],
        'resultSha256': hashlib.sha256(result_bytes).hexdigest(),
        'productionMutationOccurred': False,
        'finalizationReady': False,
    }
    return {
        'snapshot.json': snapshot_bytes,
        'result.json': result_bytes,
        'diagnostic-manifest.json': (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode(),
    }


def _read_diagnostic_bundle(bundle_b64):
    if (not bundle_b64 or len(bundle_b64) > MAX_RECOVERY_DIAGNOSTIC_OUTPUT_BYTES
            or len(bundle_b64.encode('utf-16-le')) > MAX_RECOVERY_DIAGNOSTIC_OUTPUT_UTF16_BYTES):
        raise RuntimeError('Recovery diagnostic workflow output is missing or too large')
    try:
        payload = base64.b64decode(bundle_b64, validate=True)
        if len(payload) > MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES:
            raise ValueError('oversized')
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            expected = {'snapshot.json', 'result.json', 'diagnostic-manifest.json'}
            infos = archive.infolist()
            if ({item.filename for item in infos} != expected or len(infos) != len(expected)
                    or any(item.is_dir() or item.filename.startswith('/') or '..' in Path(item.filename).parts
                           or stat.S_ISLNK(item.external_attr >> 16) or item.file_size > MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES
                           for item in infos)
                    or sum(item.file_size for item in infos) > MAX_RECOVERY_DIAGNOSTIC_BUNDLE_BYTES):
                raise ValueError('unsafe member set')
            return {item.filename: archive.read(item) for item in infos}
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError('Recovery diagnostic bundle is invalid') from exc


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


def find_post_switch_failure_evidence(sha, run_id, attempt, api=evidence_api):
    """Resolve only the immutable evidence for the named failed Deploy attempt."""
    api = _api_for_operation(api)
    release_tag('evidence', sha, run_id, attempt)
    details = api(f'actions/runs/{run_id}/attempts/{attempt}')
    jobs = api(f'actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    if (not isinstance(details, dict) or str(details.get('run_attempt')) != str(attempt)
            or details.get('head_sha') != sha or details.get('event') != 'workflow_dispatch'
            or details.get('path') != '.github/workflows/gpoint-deploy.yml'
            or details.get('status') != 'completed' or details.get('conclusion') != 'failure'
            or not isinstance(jobs, dict) or not isinstance(jobs.get('jobs'), list)
            or jobs.get('total_count', len(jobs['jobs'])) != len(jobs['jobs'])):
        raise RuntimeError('Failed Deploy attempt provenance is incomplete or mismatched')
    validate = [job for job in jobs['jobs'] if job.get('name') == 'validate']
    deploy = [job for job in jobs['jobs'] if job.get('name') == 'deploy']
    if (len(validate) != 1 or len(deploy) != 1
            or validate[0].get('status') != 'completed' or validate[0].get('conclusion') != 'success'
            or validate[0].get('head_sha') != sha
            or deploy[0].get('status') != 'completed' or deploy[0].get('conclusion') != 'failure'
            or deploy[0].get('head_sha') != sha):
        raise RuntimeError('Deploy attempt jobs do not prove validate PASS and deploy failure')
    tag = release_tag('evidence', sha, run_id, attempt)
    release = api(f'releases/tags/{tag}')
    if release is None:
        raise RuntimeError('Immutable failure evidence release is missing')
    def exact_api(route):
        return release if route == f'releases/tags/{tag}' else api(route)
    result = resolve_asset('evidence', sha, run_id, attempt, 'result.zip', exact_api)
    manifests = resolve_asset('evidence', sha, run_id, attempt, 'manifests.zip', exact_api)
    return {'run': details, 'validateJob': validate[0], 'deployJob': deploy[0], 'tag': tag,
            'resultAsset': result, 'manifestAsset': manifests}


def _download_immutable_asset(asset, target, token):
    from prepared_artifact_cache import download, checksum
    download(REPO, asset['id'], token, target, release_asset=True, expected_size=asset['size'])
    if checksum(target) != asset['digest'][7:]:
        raise RuntimeError('Immutable release asset checksum mismatch')


def _read_failure_manifest_archive(archive_path, release_id):
    """Read one manifest from the exact directory layout emitted by publish()."""
    expected = f'manifests/{release_id}.json'
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (not infos or len(infos) > MAX_FAILURE_MANIFEST_MEMBERS
                or len(names) != len(set(names))):
            raise RuntimeError('Failure manifest archive members are unsafe or incomplete')
        selected = None
        total_size = 0
        for info in infos:
            name = info.filename
            parts = name.split('/')
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            canonical = (
                len(parts) == 2 and parts[0] == 'manifests' and parts[1].endswith('.json')
                and parts[1] not in ('', '.', '..')
            ) or name == 'state/production.json'
            if (not canonical or name.startswith('/') or '\\' in name
                    or any(part in ('', '.', '..') for part in parts)
                    or info.is_dir() or info.flag_bits & 0x41
                    or info.compress_type != zipfile.ZIP_STORED
                    or file_type not in (0, stat.S_IFREG)):
                raise RuntimeError('Failure manifest archive members are unsafe or incomplete')
            if info.file_size > MAX_FAILURE_MANIFEST_MEMBER_BYTES:
                raise RuntimeError('Failure manifest archive member exceeds size limit')
            total_size += info.file_size
            if total_size > MAX_FAILURE_MANIFEST_TOTAL_BYTES:
                raise RuntimeError('Failure manifest archive exceeds size limit')
            if name == expected:
                selected = info
        if selected is None:
            raise RuntimeError('Failure manifest archive members are unsafe or incomplete')
        with archive.open(selected) as stream:
            data = stream.read(MAX_FAILURE_MANIFEST_MEMBER_BYTES + 1)
        if len(data) > MAX_FAILURE_MANIFEST_MEMBER_BYTES or len(data) != selected.file_size:
            raise RuntimeError('Failure manifest archive member exceeds size limit')
        return data


def read_post_switch_failure(sha, run_id, attempt, output_path, token=None, api=evidence_api):
    """Download and verify failed-run evidence without authorizing production mutation."""
    from prepared_artifact_cache import extract
    api = _api_for_operation(api)
    token = token or os.environ['GITHUB_TOKEN']
    identity = find_post_switch_failure_evidence(sha, run_id, attempt, api)
    output = Path(output_path)
    if output.is_symlink():
        raise RuntimeError('Recovery evidence destination must not be a symlink')
    with tempfile.TemporaryDirectory(prefix='gpoint-recovery-evidence-') as temporary:
        root = Path(temporary)
        result_zip, manifests_zip = root / 'result.zip', root / 'manifests.zip'
        _download_immutable_asset(identity['resultAsset'], result_zip, token)
        _download_immutable_asset(identity['manifestAsset'], manifests_zip, token)
        result_name = f'gpoint-release-result-{run_id}-{attempt}.json'
        result_dir = root / 'result'
        extract(result_zip, result_dir, [result_name])
        result = json.loads((result_dir / result_name).read_text())
        validate_result(result, sha, run_id, attempt)
        if (result.get('status') != 'RECOVERY_REQUIRED' or result.get('releaseState') != 'RECOVERY_REQUIRED'
                or result.get('failureStage') != 'POST_SWITCH' or result.get('productionRef') is not None
                or result.get('productionBaselineSha') != result.get('previousProductionSha')
                or not re.search(r'GitHub production tag push failed on a non-transient error; refusing retry',
                                 result.get('failureReason', ''))):
            raise RuntimeError('Immutable result does not prove the expected post-switch tag failure')
        release_id = result.get('releaseId')
        if not isinstance(release_id, str) or not re.fullmatch(r'gpoint-release-\d{8}T\d{9}Z', release_id):
            raise RuntimeError('Failed release identity is invalid')
        member = f'{release_id}.json'
        manifest_bytes = _read_failure_manifest_archive(manifests_zip, release_id)
        manifest = json.loads(manifest_bytes)
        if (manifest.get('candidateSha') != sha or manifest.get('releaseId') != release_id
                or manifest.get('status') != 'RECOVERY_REQUIRED' or manifest.get('failureStage') != 'POST_SWITCH'
                or manifest.get('previousProductionSha') != result.get('previousProductionSha')
                or manifest.get('productionShaAfter') is not None
                or manifest.get('productionRef') is not None
                or manifest.get('failureReason') != result.get('failureReason')):
            raise RuntimeError('Immutable failure manifest does not match result')
        prepare_run, prepare_attempt = result.get('prepareRunId'), result.get('prepareRunAttempt')
        if not re.fullmatch(r'[1-9][0-9]*', str(prepare_run)) or not re.fullmatch(r'[1-9][0-9]*', str(prepare_attempt)):
            raise RuntimeError('Failed Deploy result has no exact Prepare identity')
        selected_prepare_attempt, prepare_asset = find_prepare_evidence(
            sha, str(prepare_run), str(prepare_attempt), api)
        if str(selected_prepare_attempt) != str(prepare_attempt):
            raise RuntimeError('Prepare evidence attempt differs from failed Deploy input identity')
        prepare_dir = root / 'prepare'
        prepare_dir.mkdir()
        prepare_zip = prepare_dir / 'bundle.zip'
        _download_immutable_asset(prepare_asset, prepare_zip, token)
        prepare_contents = root / 'prepare-contents'
        with zipfile.ZipFile(prepare_zip) as archive:
            prepare_names = [info.filename for info in archive.infolist()]
            if ('receipt.json' not in prepare_names or len(prepare_names) != len(set(prepare_names))
                    or any(Path(name).name != name for name in prepare_names)):
                raise RuntimeError('Prepare bundle members are unsafe or incomplete')
        extract(prepare_zip, prepare_contents, [prepare_names])
        receipt = json.loads((prepare_contents / 'receipt.json').read_text())
        if (receipt.get('candidateSha') != sha or str(receipt.get('runId')) != str(prepare_run)
                or str(receipt.get('runAttempt')) != str(prepare_attempt) or receipt.get('status') != 'READY'
                or receipt.get('schema') != 3 or receipt.get('transport') != 'ghcr-oci'
                or set(receipt.get('images', {})) != set(RECOVERY_IMAGE_REPOSITORIES)
                or not re.fullmatch(r'[A-Za-z0-9_]{1,128}', receipt.get('prismaHead', ''))
                or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', receipt.get('agataHead', ''))
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', receipt.get('gsalesMigrationHead', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', receipt.get('gsalesMigrationSha256', ''))):
            raise RuntimeError('Immutable Prepare receipt does not match failure result')
        for role, repository in RECOVERY_IMAGE_REPOSITORIES.items():
            image = receipt['images'][role]
            if (image.get('repository') != repository or image.get('sourceSha') != sha
                    or image.get('platform') != 'linux/amd64'
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', image.get('digest', ''))
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', image.get('configDigest', ''))):
                raise RuntimeError(f'Immutable Prepare image identity is invalid: {role}')
        # Record verified asset identities and manifest bytes digest; callers must
        # still supply independent, fresh production readback before reconciliation.
        verified = {
            'run': {'id': str(run_id), 'attempt': str(attempt), 'headSha': sha,
                    'event': identity['run']['event'], 'path': identity['run']['path'],
                    'conclusion': identity['run']['conclusion']},
            'validateJob': {'status': identity['validateJob']['status'],
                            'conclusion': identity['validateJob']['conclusion'], 'headSha': sha},
            'deployJob': {'status': identity['deployJob']['status'],
                          'conclusion': identity['deployJob']['conclusion'], 'headSha': sha},
            'evidence': {'tag': identity['tag'], 'immutable': True,
                         'resultDigest': identity['resultAsset']['digest'],
                         'manifestDigest': identity['manifestAsset']['digest'],
                         'resultChecksumVerified': True, 'manifestChecksumVerified': True},
            'result': result,
            'failureManifest': {**manifest, 'fileName': member},
            'failureManifestSha256': __import__('hashlib').sha256(manifest_bytes).hexdigest(),
            'failureManifestBytesBase64': base64.b64encode(manifest_bytes).decode('ascii'),
            'prepare': {'runId': str(prepare_run), 'attempt': str(prepare_attempt),
                        'candidateSha': sha, 'tag': f'gpoint-prepare-{sha}-{prepare_run}-{prepare_attempt}',
                        'status': 'success', 'immutable': True, 'bundleDigest': prepare_asset['digest'],
                        'receipt': receipt},
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(verified, sort_keys=True, indent=2) + '\n').encode()
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.unlink(output)
            except OSError:
                pass
            raise
        return verified


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


def package_recovery_diagnostics(snapshot_path=None, result_path=None, manifest_path=None,
                                 executor_sha=None, run_id=None, attempt=None, output_path=None):
    executor_sha = executor_sha or os.environ.get('GPOINT_RECOVERY_EXECUTOR_SHA', '')
    run_id = str(run_id or os.environ.get('GITHUB_RUN_ID', ''))
    attempt = str(attempt or os.environ.get('GITHUB_RUN_ATTEMPT', ''))
    snapshot_path = snapshot_path or os.environ.get('GPOINT_RECOVERY_SNAPSHOT_PATH')
    result_path = result_path or os.environ.get('RELEASE_RESULT')
    manifest_path = manifest_path or os.environ.get('GPOINT_RECOVERY_DIAGNOSTIC_MANIFEST')
    output_path = output_path or os.environ.get('GITHUB_OUTPUT')
    if not output_path:
        raise RuntimeError('Recovery diagnostic workflow output is unavailable')
    files = build_recovery_diagnostics_bundle(snapshot_path, result_path, executor_sha, run_id, attempt)
    diagnostic_manifest = json.loads(files['diagnostic-manifest.json'])
    if not manifest_path:
        raise RuntimeError('Recovery diagnostic local manifest path is unavailable')
    source_manifest = Path(manifest_path)
    if source_manifest.is_symlink() or not source_manifest.is_file():
        raise RuntimeError('Recovery diagnostic local manifest is unavailable')
    local_manifest = json.loads(source_manifest.read_text())
    if (local_manifest.get('releaseId') != diagnostic_manifest['releaseId']
            or local_manifest.get('recoverySnapshot') != json.loads(files['snapshot.json'])
            or local_manifest.get('productionMutationOccurred') is not False
            or local_manifest.get('databaseApplyOccurred') is not False
            or local_manifest.get('runtimeSwitchOccurred') is not False):
        raise RuntimeError('Recovery diagnostic local manifest conflicts with verified snapshot')
    files['diagnostic-manifest.json'] = (json.dumps(diagnostic_manifest,
        sort_keys=True, separators=(',', ':')) + '\n').encode()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in ('snapshot.json', 'result.json', 'diagnostic-manifest.json'):
            archive.writestr(name, files[name])
    encoded = base64.b64encode(payload.getvalue()).decode('ascii')
    if (len(encoded) > MAX_RECOVERY_DIAGNOSTIC_OUTPUT_BYTES
            or len(encoded.encode('utf-16-le')) > MAX_RECOVERY_DIAGNOSTIC_OUTPUT_UTF16_BYTES):
        raise RuntimeError('Recovery diagnostic bundle exceeds the bounded workflow-output size')
    with open(output_path, 'a', encoding='utf-8') as output:
        output.write(f'recovery_diagnostic_bundle={encoded}\n')
    print(f'RECOVERY_DIAGNOSTIC_BUNDLE=READY bytes={len(payload.getvalue())} sha256={hashlib.sha256(payload.getvalue()).hexdigest()}')


def _verify_diagnostic_release_metadata(release, tag, executor_sha, expected, *, draft, immutable):
    if (not isinstance(release, dict) or release.get('tag_name') != tag
            or release.get('draft') is not draft
            or (immutable and release.get('immutable') is not True)
            or (not immutable and release.get('immutable') is True)):
        raise RuntimeError('Recovery diagnostic Release metadata identity/status mismatch')
    assets = release.get('assets')
    if not isinstance(assets, list) or {asset.get('name') for asset in assets} != set(expected) or len(assets) != len(expected):
        raise RuntimeError('Recovery diagnostic Release asset set is incomplete or unexpected')
    for asset in assets:
        name = asset['name']
        if (asset.get('state') != 'uploaded' or asset.get('digest') != 'sha256:' + expected[name][0]
                or asset.get('size') != expected[name][1]
                or not re.fullmatch(r'[1-9][0-9]*', str(asset.get('id', '')))):
            raise RuntimeError('Recovery diagnostic Release asset checksum or identity mismatch')


def publish_recovery_diagnostics(executor_sha=None, run_id=None, attempt=None, bundle_b64=None,
                                 api=None, command=None, downloader=None):
    """Publish only a new, provenance-bound diagnostic Release; never reuse a tag."""
    executor_sha = executor_sha or os.environ.get('GITHUB_SHA', '')
    run_id = str(run_id or os.environ.get('GITHUB_RUN_ID', ''))
    attempt = str(attempt or os.environ.get('GITHUB_RUN_ATTEMPT', ''))
    bundle_b64 = bundle_b64 if bundle_b64 is not None else os.environ.get('RECOVERY_DIAGNOSTICS_BUNDLE_B64', '')
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not token:
        raise RuntimeError('Recovery diagnostic publication authorization is unavailable')
    if (os.environ.get('GITHUB_REPOSITORY') != REPO or os.environ.get('GITHUB_SHA') != executor_sha
            or subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() != executor_sha):
        raise RuntimeError('Recovery diagnostic publisher checkout differs from exact executor SHA')
    files = _read_diagnostic_bundle(bundle_b64)
    snapshot, result, manifest = (json.loads(files[name]) for name in
                                  ('snapshot.json', 'result.json', 'diagnostic-manifest.json'))
    verified_files = build_recovery_diagnostics_bundle_from_bytes(snapshot, result, manifest,
        executor_sha, run_id, attempt, files['snapshot.json'], files['result.json'])
    if json.loads(files['snapshot.json']) != snapshot or json.loads(files['result.json']) != result:
        raise RuntimeError('Recovery diagnostic bundle contains altered evidence')
    if files['diagnostic-manifest.json'] != verified_files['diagnostic-manifest.json']:
        raise RuntimeError('Recovery diagnostic bundle manifest is not reproducible')
    tag = recovery_diagnostics_tag(executor_sha, run_id, attempt)
    reader = api or _EvidenceApiClient(token)
    writer = command or gh
    fetch_asset = downloader or _download_immutable_asset
    if reader(f'releases/tags/{tag}') is not None or reader(f'git/ref/tags/{tag}') is not None:
        raise RuntimeError('Recovery diagnostic immutable Release identity already exists')
    expected = {name: (hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items()}
    with tempfile.TemporaryDirectory(prefix='gpoint-recovery-diagnostics-publish-') as temporary:
        root = Path(temporary)
        local = {}
        for name, data in files.items():
            path = root / name
            with path.open('xb') as stream:
                stream.write(data)
            local[name] = path
        writer('api', '--method', 'POST', f'repos/{REPO}/git/refs',
               '-f', f'ref=refs/tags/{tag}', '-f', f'sha={executor_sha}')
        writer('release', 'create', tag, '--repo', REPO, '--verify-tag', '--draft',
               '--title', tag, '--notes', f'Read-only recovery diagnostics; executor {executor_sha}, run {run_id}, attempt {attempt}.')
        writer('release', 'upload', tag, '--repo', REPO, *map(str, local.values()))
        draft = reader(f'releases/tags/{tag}')
        ref = reader(f'git/ref/tags/{tag}')
        if ref is None or ref.get('object', {}).get('type') != 'commit' or ref.get('object', {}).get('sha') != executor_sha:
            raise RuntimeError('Recovery diagnostic draft tag does not target the exact executor SHA')
        _verify_diagnostic_release_metadata(draft, tag, executor_sha, expected, draft=True, immutable=False)
        writer('release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest=false')
        release = reader(f'releases/tags/{tag}')
        ref = reader(f'git/ref/tags/{tag}')
        if ref is None or ref.get('object', {}).get('type') != 'commit' or ref.get('object', {}).get('sha') != executor_sha:
            raise RuntimeError('Published recovery diagnostic tag target changed')
        _verify_diagnostic_release_metadata(release, tag, executor_sha, expected, draft=False, immutable=True)
        assets = {asset['name']: asset for asset in release['assets']}
        for name, path in local.items():
            readback = root / f'readback-{name}'
            fetch_asset(assets[name], readback, token)
            if hashlib.sha256(readback.read_bytes()).hexdigest() != expected[name][0]:
                raise RuntimeError('Recovery diagnostic immutable asset readback checksum mismatch')
    print(f'RECOVERY_DIAGNOSTIC_RELEASE=IMMUTABLE tag={tag} snapshot_sha256={manifest["snapshotSha256"]}')


def build_recovery_diagnostics_bundle_from_bytes(snapshot, result, manifest, executor_sha, run_id, attempt,
                                                 snapshot_bytes, result_bytes):
    """Revalidate the publisher input without trusting the producer job output."""
    # Reuse the same identity, mutation-policy, snapshot digest and result checks.
    with tempfile.TemporaryDirectory(prefix='gpoint-recovery-diagnostic-verify-') as temporary:
        root = Path(temporary)
        snapshot_path, result_path = root/'snapshot.json', root/'result.json'
        snapshot_path.write_bytes(snapshot_bytes)
        result_path.write_bytes(result_bytes)
        if json.loads(snapshot_bytes) != snapshot or json.loads(result_bytes) != result:
            raise RuntimeError('Recovery diagnostic JSON bytes do not match parsed evidence')
        generated = build_recovery_diagnostics_bundle(snapshot_path, result_path, executor_sha, run_id, attempt)
    if generated['diagnostic-manifest.json'] != json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode() + b'\n':
        raise RuntimeError('Recovery diagnostic manifest identity or digest mismatch')
    return {'snapshot.json': snapshot_bytes, 'result.json': result_bytes,
            'diagnostic-manifest.json': (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()}


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
        if result.is_symlink():
            raise RuntimeError('Result destination must not be a symlink')
        result.write_bytes((extracted / name).read_bytes())
        if result_path is None and os.environ.get('GITHUB_ENV'):
            with open(os.environ['GITHUB_ENV'], 'a') as stream:
                stream.write(f'RELEASE_RESULT={result}\n')
                stream.write(f'RELEASE_EVIDENCE_ATTEMPT={source_attempt}\n')
        print(f'RELEASE_EVIDENCE_ATTEMPT={source_attempt}')



def main():
    sha = os.environ.get('CANDIDATE_SHA', os.environ.get('GITHUB_SHA', ''))
    if len(sys.argv) > 1 and sys.argv[1] == 'package-recovery-diagnostics':
        package_recovery_diagnostics()
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'publish-recovery-diagnostics':
        publish_recovery_diagnostics()
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'read-recovery-failure' and len(sys.argv) == 6:
        read_post_switch_failure(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
        return
    run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    if sys.argv[1] == 'publish':
        publish(sys.argv[2], sha, run_id, attempt)
    elif sys.argv[1] == 'read-result':
        read_result(sha, run_id, attempt)
    else:
        raise RuntimeError('Unknown release asset command')


if __name__ == '__main__':
    main()
