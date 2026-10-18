#!/usr/bin/env python3
"""Publish bounded release facts; preserve architect-owned context/history."""
import json
import os
from pathlib import Path
import re
import sys
import base64
import socket
import ssl
import io
import urllib.error
import urllib.request
import urllib.parse
import zipfile
import hashlib
from datetime import datetime, timezone

from sync_agents_to_linear import graphql_request, sync_document, SyncError

DOCUMENTS = {
    'handoff': ('1d84a3c6-f297-40c2-b9e7-ab81bc749c48', 'Архитектор — Handoff'),
    'journal': ('56f7f9f5-c6e1-42b7-8c29-5b24e6c41a06', 'Журнал релизов'),
}
REPOSITORY = 'akhripunov/gpoint'
CURRENT_MARKER = 'GPOINT_CURRENT_STATE'


def github_json(path, token=None):
    token = token or os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise SyncError('GITHUB_TOKEN is missing: cannot verify current release state')
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    request = urllib.request.Request(f'{base}/{path.lstrip("/")}', headers={
        'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error = SyncError(f'GitHub current-state read failed: http_status_{exc.code} ({type(exc).__name__})')
        error.http_status = exc.code
        error.network_category = 'http_status'
        error.reason_type = type(exc).__name__
        raise error from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            category = 'timeout'
        elif isinstance(reason, socket.gaierror):
            category = 'dns'
        elif isinstance(reason, ssl.SSLError):
            category = 'tls'
        elif isinstance(reason, (ConnectionError, OSError)):
            category = 'connection'
        else:
            category = 'network_unknown'
        error = SyncError(f'GitHub current-state read failed: {category} ({type(reason).__name__})')
        error.http_status = None
        error.network_category = category
        error.reason_type = type(reason).__name__
        raise error from exc
    except Exception as exc:
        error = SyncError(f'GitHub current-state read failed: request_error ({type(exc).__name__})')
        error.http_status = getattr(exc, 'code', None)
        error.network_category = 'request_error'
        error.reason_type = type(exc).__name__
        raise error from exc


DEPLOY_EVIDENCE_REPOSITORY = 'akhripunov/gpoint-deploy-evidence'
DEPLOY_EVIDENCE_TYPE = 'application/vnd.gpoint.deploy.evidence.v1'
DEPLOY_EVIDENCE_LAYER = 'application/vnd.gpoint.deploy.evidence.v1+json'
OCI_MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
MAX_DEPLOY_EVIDENCE_BYTES = 16 * 1024**2


def ghcr_get(url, username, password, accept, limit):
    """Read one GHCR object. Credentials are sent only to ghcr.io / its fixed token service."""
    def request(headers):
        req = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            challenge = exc.headers.get('WWW-Authenticate', '')
            match = re.fullmatch(r'Bearer realm="(https://ghcr\.io/token)",service="ghcr\.io",scope="repository:([A-Za-z0-9._/-]+):pull"', challenge)
            expected = url.split('/v2/', 1)[1].split('/manifests/', 1)[0].split('/blobs/', 1)[0]
            if not match or match.group(2) != expected:
                raise SyncError('GHCR returned an unexpected authentication challenge') from None
            token_url = match.group(1) + '?service=ghcr.io&scope=' + urllib.parse.quote(f'repository:{expected}:pull', safe=':')
            auth = base64.b64encode(f'{username}:{password}'.encode()).decode()
            token_req = urllib.request.Request(token_url, headers={'Authorization': f'Basic {auth}', 'Accept': 'application/json'})
            with urllib.request.urlopen(token_req, timeout=30) as token_response:
                token_payload = json.loads(token_response.read(65537))
            bearer = token_payload.get('token') or token_payload.get('access_token')
            if not isinstance(bearer, str) or not re.fullmatch(r'[^\s\r\n]{1,16384}', bearer):
                raise SyncError('GHCR returned an invalid pull token')
            response = urllib.request.urlopen(urllib.request.Request(url, headers={**headers, 'Authorization': f'Bearer {bearer}'}), timeout=30)
        with response:
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise SyncError('GHCR Deploy evidence exceeds size limit')
            return payload, response.headers
    try:
        return request({'Accept': accept})
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f'GHCR Deploy evidence read failed: {type(exc).__name__}') from exc


DEPLOY_EVIDENCE_STEP = 'Publish deployment result as a GHCR OCI artifact'
MAX_DEPLOY_LOG_BYTES = 32 * 1024**2


def deploy_evidence_digest_from_logs(run_id, attempt, sha):
    """Bind the OCI digest to this exact successful Deploy job and its published log step."""
    route = f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page=1'
    data = github_json(route)
    jobs = data.get('jobs') if isinstance(data, dict) else None
    if not isinstance(jobs, list) or data.get('total_count') != len(jobs):
        raise SyncError('Deploy job inventory is incomplete for exact evidence lookup')
    matches = [job for job in jobs if job.get('name') == 'deploy']
    if len(matches) != 1:
        raise SyncError('Expected one exact Deploy job in the selected workflow attempt')
    job = matches[0]
    if (job.get('status') != 'completed' or job.get('conclusion') not in ('success', 'failure')
            or str(job.get('run_id')) != str(run_id) or str(job.get('run_attempt')) != str(attempt)
            or job.get('head_sha') != sha):
        raise SyncError('Deploy job identity is not a completed exact workflow attempt and source SHA')
    steps = job.get('steps')
    if not isinstance(steps, list):
        raise SyncError('Deploy job step inventory is missing')
    evidence_steps = [step for step in steps if step.get('name') == DEPLOY_EVIDENCE_STEP]
    if (len(evidence_steps) != 1 or evidence_steps[0].get('status') != 'completed'
            or evidence_steps[0].get('conclusion') != 'success'
            or not isinstance(evidence_steps[0].get('number'), int)):
        raise SyncError('Exact Deploy evidence publication step did not succeed')
    payload = download_job_logs(job.get('id'))
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        entries = archive.infolist()
        if len(entries) > 500 or sum(entry.file_size for entry in entries) > 64 * 1024**2:
            raise SyncError('Deploy job logs exceed the bounded evidence limit')
        step_number = evidence_steps[0]['number']
        suffix = f'/{step_number}_{DEPLOY_EVIDENCE_STEP}.txt'
        candidates = [entry for entry in entries if entry.filename.endswith(suffix)]
        if len(candidates) != 1 or candidates[0].file_size > 2 * 1024**2:
            raise SyncError('Exact Deploy evidence step log is missing or ambiguous')
        log = archive.read(candidates[0]).decode('utf-8', errors='strict')
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f'Deploy evidence step log is invalid: {type(exc).__name__}') from exc
    digests = re.findall(r'(?m)(?:^|\s)DEPLOY_EVIDENCE_DIGEST=(sha256:[a-f0-9]{64})(?:\s|$)', log)
    if len(digests) != 1:
        raise SyncError('Exact Deploy evidence digest line is missing or ambiguous')
    return digests[0]


def download_job_logs(job_id):
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
        raise SyncError('Deploy job ID is invalid')
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise SyncError('GITHUB_TOKEN is missing: cannot verify Deploy job logs')
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
    request = urllib.request.Request(f'{base}/repos/{REPOSITORY}/actions/jobs/{job_id}/logs', headers={
        'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': '2022-11-28'})
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urllib.parse.urlparse(newurl).scheme != 'https':
                raise SyncError('Deploy job log redirect must use HTTPS')
            if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
                return urllib.request.Request(newurl, headers={'Accept': 'application/octet-stream'})
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    try:
        with urllib.request.build_opener(SafeRedirect).open(request, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme != 'https':
                raise SyncError('Deploy job log URL must use HTTPS')
            data = response.read(MAX_DEPLOY_LOG_BYTES + 1)
            if len(data) > MAX_DEPLOY_LOG_BYTES:
                raise SyncError('Deploy job logs exceed compressed size limit')
            return data
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f'GitHub Deploy job log read failed: {type(exc).__name__}') from exc


def artifact_json(run_id, *, attempt=None, sha=None, deployment_id=None):
    """Resolve Deploy evidence by run-bound digest from GitHub logs and verify OCI content."""
    run_id = str(run_id)
    run = github_json(f'repos/{REPOSITORY}/actions/runs/{run_id}')
    attempt = str(attempt or run.get('run_attempt', ''))
    sha = sha or run.get('head_sha', '')
    if (run.get('path') != '.github/workflows/gpoint-deploy.yml' or run.get('head_branch') != 'main'
            or run.get('event') != 'workflow_dispatch' or str(run.get('id')) != run_id
            or str(run.get('run_attempt')) != attempt or run.get('head_sha') != sha
            or not re.fullmatch(r'[a-f0-9]{40}', sha) or not attempt.isdigit()):
        raise SyncError('Unverified Deploy result workflow identity')
    digest = deploy_evidence_digest_from_logs(run_id, attempt, sha)
    username = os.environ.get('GHCR_READ_USERNAME') or os.environ.get('GITHUB_ACTOR', '')
    password = os.environ.get('GHCR_READ_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
    if not username or not password:
        raise SyncError('GHCR read credentials are missing')
    repository = DEPLOY_EVIDENCE_REPOSITORY
    base = f'https://ghcr.io/v2/{repository}'
    accept = f'{OCI_MANIFEST_TYPE}, application/vnd.oci.image.index.v1+json'
    manifest_bytes, digest_headers = ghcr_get(f'{base}/manifests/{digest}', username, password, accept, 1024 * 1024)
    if digest_headers.get('Docker-Content-Digest') != digest or hashlib.sha256(manifest_bytes).hexdigest() != digest[7:]:
        raise SyncError('GHCR Deploy evidence manifest digest mismatch')
    try:
        manifest = json.loads(manifest_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncError('GHCR Deploy evidence manifest is invalid JSON') from exc
    layers = manifest.get('layers') if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get('mediaType') != OCI_MANIFEST_TYPE
            or manifest.get('artifactType') != DEPLOY_EVIDENCE_TYPE
            or not isinstance(layers, list) or len(layers) != 1):
        raise SyncError('GHCR Deploy evidence OCI artifact type or layout is invalid')
    layer = layers[0]
    blob_digest, blob_size = layer.get('digest', ''), layer.get('size')
    if (layer.get('mediaType') != DEPLOY_EVIDENCE_LAYER or not re.fullmatch(r'sha256:[a-f0-9]{64}', blob_digest)
            or not isinstance(blob_size, int) or isinstance(blob_size, bool)
            or blob_size < 1 or blob_size > MAX_DEPLOY_EVIDENCE_BYTES):
        raise SyncError('GHCR Deploy evidence layer descriptor is invalid')
    blob, _ = ghcr_get(f'{base}/blobs/{blob_digest}', username, password, 'application/octet-stream', MAX_DEPLOY_EVIDENCE_BYTES)
    if len(blob) != blob_size or hashlib.sha256(blob).hexdigest() != blob_digest[7:]:
        raise SyncError('GHCR Deploy evidence content digest or size mismatch')
    try:
        result = json.loads(blob)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncError('GHCR Deploy evidence layer is invalid JSON') from exc
    if (not isinstance(result, dict) or result.get('candidateSha') != sha
            or str(result.get('automationRunId')) != run_id or str(result.get('automationRunAttempt')) != attempt
            or str(result.get('deployRunId')) != run_id or str(result.get('deployRunAttempt')) != attempt
            or (deployment_id is not None and str(result.get('deploymentId')) != str(deployment_id))):
        raise SyncError('GHCR Deploy evidence does not match the exact Deployment and workflow identity')
    return result


def latest_run(workflow, sha=None, successful=False):
    runs = []
    expected_total = None
    for page in range(1, 101):
        data = github_json(f'repos/{REPOSITORY}/actions/workflows/{workflow}/runs?branch=main&per_page=100&page={page}')
        rows = data.get('workflow_runs')
        if not isinstance(rows, list) or not isinstance(data.get('total_count'), int):
            raise SyncError('GitHub workflow run inventory is incomplete')
        if expected_total is None: expected_total = data['total_count']
        if expected_total != data['total_count']: raise SyncError('GitHub workflow run inventory changed during read')
        runs.extend(rows)
        if len(runs) >= expected_total: break
        if len(rows) < 100: break
    if expected_total is None or len(runs) != expected_total:
        raise SyncError('GitHub workflow run inventory pagination is incomplete')
    runs = [r for r in runs if (sha is None or r.get('head_sha') == sha)]
    if successful:
        runs = [r for r in runs if r.get('status') == 'completed' and r.get('conclusion') == 'success']
    return max(runs, key=lambda r: (r.get('created_at', ''), r.get('run_attempt', 0)), default=None)


def current_candidate_status(main_sha, current_main_run, event=None):
    if current_main_run and current_main_run.get('status') == 'completed' and current_main_run.get('conclusion') == 'success':
        return 'READY'
    if (event and event.get('sha') == main_sha and current_main_run and
            str(current_main_run.get('id')) == str(event.get('run_id')) and event.get('status') == 'READY'):
        # PREPARE job completed successfully (needs.prepare.result=success), but
        # the containing workflow is still running its Linear publisher job.
        return 'PREPARE_GATES_PASS_WORKFLOW_PENDING'
    return 'NOT_PREPARED_FOR_CURRENT_MAIN'


def production_evidence():
    """Project current status from GitHub Deployments; VPS state is verified by the Deploy job."""
    deployments = []
    for page in range(1, 101):
        rows = github_json(f'repos/{REPOSITORY}/deployments?environment=production&task=gpoint-production-deploy&per_page=100&page={page}')
        if not isinstance(rows, list): raise SyncError('GitHub production Deployments response is invalid')
        deployments.extend(rows)
        if len(rows) < 100: break
    else:
        raise SyncError('GitHub production Deployments exceeded the pagination limit')
    exact = []
    for item in deployments:
        payload = item.get('payload')
        if isinstance(payload, str):
            try: payload = json.loads(payload)
            except ValueError as exc: raise SyncError('GitHub Deployment provenance payload is invalid') from exc
        if item.get('environment') != 'production' or item.get('production_environment') is not True \
                or item.get('transient_environment') is not False or item.get('task') != 'gpoint-production-deploy':
            raise SyncError('GitHub production Deployment identity is invalid')
        if (not isinstance(payload, dict) or payload.get('schema') != 'gpoint-deployment/v1'
                or payload.get('repository') != REPOSITORY or payload.get('candidateSha') != item.get('sha')
                or payload.get('workflow', {}).get('runId') is None
                or payload.get('workflow', {}).get('attempt') is None):
            raise SyncError('GitHub production Deployment provenance is incomplete')
        exact.append((item, payload))
    if not exact:
        state = {'currentProductionSha': None, 'lastReleaseStatus': 'NOT_DEPLOYED',
                 'serverDeployStatus': 'NOT_DEPLOYED', 'workerOperationalStatus': 'NOT_VERIFIED',
                 'overallServiceReadiness': 'NOT_VERIFIED', 'dependencyCheckedAt': 'not checked'}
        return state, {'status': 'NOT_DEPLOYED'}, None
    exact.sort(key=lambda pair: (pair[0].get('created_at', ''), pair[0].get('id', 0)), reverse=True)
    allowed = {'queued', 'pending', 'in_progress', 'success', 'failure', 'error', 'inactive'}
    observed = []
    for deployment, payload in exact:
        statuses = []
        for page in range(1, 101):
            rows = github_json(f'repos/{REPOSITORY}/deployments/{deployment["id"]}/statuses?per_page=100&page={page}')
            if not isinstance(rows, list): raise SyncError('GitHub Deployment statuses response is invalid')
            statuses.extend(rows)
            if len(rows) < 100: break
        else: raise SyncError('GitHub Deployment statuses exceeded the pagination limit')
        if not statuses: raise SyncError(f'GitHub Deployment {deployment["id"]} has no recorded status')
        statuses.sort(key=lambda row: row.get('created_at', ''), reverse=True)
        current = statuses[0]
        if current.get('state') not in allowed: raise SyncError('GitHub Deployment current status is invalid')
        observed.append((deployment, payload, current))
    deployment, payload, current = observed[0]
    # An unresolved newest attempt may already have changed the VPS. Older
    # successful Deployments are history, not proof of the currently installed SHA.
    installed = observed[0] if observed[0][2].get('state') == 'success' else None
    installed_sha = installed[0].get('sha') if installed else None
    sha = deployment.get('sha')
    run_id = str(payload['workflow']['runId'])
    attempt = str(payload['workflow']['attempt'])
    expected_url = f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}'
    if current.get('log_url') != expected_url:
        raise SyncError('GitHub Deployment status does not link to its exact workflow attempt')
    result = artifact_json(run_id, attempt=attempt, sha=sha, deployment_id=deployment.get('id'))
    state = {'currentProductionSha': installed_sha, 'lastReleaseStatus': current['state'].upper(),
             'serverDeployStatus': current['state'].upper(), 'workerOperationalStatus': 'VERIFIED_BY_DEPLOY' if current['state'] == 'success' else 'NOT_VERIFIED',
             'overallServiceReadiness': 'READY' if current['state'] == 'success' else 'NOT_VERIFIED',
             'dependencyCheckedAt': current.get('created_at', 'unknown')}
    manifest = {'candidateSha': sha, 'productionShaAfter': installed_sha if current['state'] == 'success' else None,
                'status': current['state'].upper(), 'prepare': payload.get('prepare'),
                'description': current.get('description'), 'logUrl': current.get('log_url'),
                'resultStatus': result.get('status')}
    run = {'id': int(run_id), 'run_attempt': int(attempt), 'head_sha': sha,
           'status': 'completed' if current['state'] in {'success', 'failure', 'error'} else 'in_progress',
           'conclusion': current['state'], 'html_url': expected_url}
    return state, manifest, run

def current_state(event=None):
    commit = github_json(f'repos/{REPOSITORY}/commits/main')
    main_sha = commit.get('sha', '')
    if not re.fullmatch('[a-f0-9]{40}', main_sha):
        raise SyncError('GitHub main SHA is invalid')
    latest_prepare_success = latest_run('gpoint-prepare.yml', successful=True)
    latest_for_main = latest_run('gpoint-prepare.yml', sha=main_sha)
    production, manifest, deployment = production_evidence()
    prod_sha = production.get('currentProductionSha')
    in_sync = bool(prod_sha and prod_sha == main_sha)
    candidate = current_candidate_status(main_sha, latest_for_main, event)
    return {
        'synced_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'main_sha': main_sha,
        'main_checked_at': commit.get('commit', {}).get('committer', {}).get('date', 'unknown'),
        'production_sha': prod_sha, 'production_checked_at': production.get('dependencyCheckedAt', 'unknown'),
        'production_ref': '',
        'release_status': production.get('lastReleaseStatus', 'UNKNOWN'), 'in_sync': in_sync,
        'prepare': latest_prepare_success, 'candidate': candidate, 'latest_main_prepare': latest_for_main,
        'deploy': deployment, 'manifest': manifest,
        'server_deploy_status': production.get('serverDeployStatus', manifest.get('status', 'UNKNOWN')),
        'worker_operational_status': production.get('workerOperationalStatus', 'NOT_VERIFIED'),
        'service_readiness': production.get('overallServiceReadiness', 'DEGRADED'),
        'dependency_checked_at': production.get('dependencyCheckedAt', 'not checked'),
    }


def render_current_state(state):
    prep = state['prepare'] or {}
    deploy = state['deploy'] or {}
    m = state['manifest']
    state_name = 'IN_SYNC' if state['in_sync'] else 'MAIN_AHEAD_OF_PRODUCTION'
    lines = [
        f"Синхронизировано: `{state['synced_at']}` UTC.",
        f"GitHub main: `{state['main_sha']}` (commit time `{state['main_checked_at']}`).",
        f"Production: `{state['production_sha']}`; latest GitHub Deployment status **{state['release_status']}** at `{state['production_checked_at']}`. VPS readback is required before success is recorded.",
        f"Сверка SHA: **{state_name}**.",
        f"Последний завершившийся успешный Prepare: `{(prep.get('head_sha') or 'unknown')}` / run `#{prep.get('id', 'unknown')}`; current-main run `#{(state['latest_main_prepare'] or {}).get('id', 'unknown')}`: **{state['candidate']}**.",
        f"Последний production Deploy: `{m.get('productionShaAfter') or m.get('candidateSha', 'unknown')}` / run `#{deploy.get('id', 'unknown')}` / **{state['release_status']}**. Prepare tag: `{(m.get('prepare') or {}).get('tag', 'unknown')}`.",
        f"GitHub Deploy workflow: `{deploy.get('status', 'unknown')}` / status **{state['release_status']}**; latest status URL `{m.get('logUrl', 'unknown')}`.",
        f"Server deploy: **{state.get('server_deploy_status', state['release_status'])}**; Mac worker: **{state.get('worker_operational_status', 'NOT_VERIFIED')}**; service readiness: **{state.get('service_readiness', 'DEGRADED')}**; dependency checked `{state.get('dependency_checked_at', 'not checked')}`.",
        "Пользовательская приёмка: **OWNER_SMOKE_PENDING**; technical COMPLETE не является подтверждением Telegram-ответа.",
        "При degraded worker повторить dependency recheck без Deploy. Реальный Telegram/GSales inference smoke подтверждается отдельно; production ради dependency check повторно не запускать.",
    ]
    return f"<!-- {CURRENT_MARKER}_BEGIN -->\n\n" + "\n\n".join(lines) + f"\n\n<!-- {CURRENT_MARKER}_END -->"


def upsert_current_state(content, block, handoff=False):
    start, end = f'<!-- {CURRENT_MARKER}_BEGIN -->', f'<!-- {CURRENT_MARKER}_END -->'
    starts, ends = content.count(start), content.count(end)
    # Linear preserved two adjacent closing markers in existing documents.
    # Collapse only that exact shape; other mismatches remain fail-closed.
    if ends == 2 and end + end in content:
        content = content.replace(end + end, end if starts == 1 else '', 1)
        ends = content.count(end)
    if starts != ends or starts > 1:
        raise SyncError('GPoint CURRENT STATE markers are malformed')
    if start in content:
        before, rest = content.split(start, 1)
        _, after = rest.split(end, 1)
        return before + block + after
    if handoff:
        return block + '\n\n' + content
    title_end = content.find('\n')
    if not content.startswith('# ') or title_end < 0:
        raise SyncError('GPoint Journal title anchor is missing')
    return content[:title_end + 1] + '\n' + block + '\n\n' + content[title_end + 1:].lstrip('\n')


def replace_section(content, section, body):
    start, end = f'## Автоматизация: {section}', f'## Конец автоматизации: {section}'
    block = f'{start}\n\n{body}\n\n{end}'
    if content.count(start) != content.count(end) or content.count(start) > 1:
        raise SyncError('Ambiguous automatic section; refusing to overwrite handoff')
    if start not in content:
        return content.rstrip() + '\n\n' + block + '\n'
    first, last = content.index(start), content.index(end) + len(end)
    if last < first:
        raise SyncError('Reversed automatic section markers')
    return content[:first] + block + content[last:]


def append_event(content, event_id, body):
    marker = f'GPOINT_RELEASE_EVENT_{event_id}'
    if marker in content:
        return content  # Workflow reruns never duplicate the same event.
    return content.rstrip() + f'\n\n### {marker}\n\n{body}\n'


def event_identity(run_id, attempt, phase, status):
    return f'{run_id}_attempt_{attempt}_{phase}_{status}'


def candidate_status(prepare_result):
    return {'success': 'READY', 'cancelled': 'CANCELLED', 'skipped': 'SKIPPED'}.get(prepare_result, 'FAILED')


def publish(section, event_id, body, key, call=graphql_request, state=None):
    if not key.strip():
        raise SyncError('LINEAR_API_KEY is missing: configure the repository secret; release facts were not published')
    state_block = render_current_state(state or current_state())
    # Journal first: a partial failure keeps the history, and retry is idempotent.
    for kind in ('journal', 'handoff'):
        doc_id, title = DOCUMENTS[kind]
        current = call('query($id: String!) { document(id: $id) { id title content } }', {'id': doc_id}, key).get('document')
        if not current or current.get('title') != title or current.get('id') != doc_id:
            raise SyncError('Release document identity mismatch')
        content = current.get('content') or ''
        if kind == 'journal':
            updated = append_event(content, event_id, body)
            updated = upsert_current_state(updated, state_block)
        else:
            updated = upsert_current_state(content, state_block, handoff=True)
            updated = replace_section(updated, section, body)
        sync_document(updated, doc_id, key, call, expected_title=title)


def sync_current_state(key, call=graphql_request):
    if not key.strip():
        raise SyncError('LINEAR_API_KEY is missing: release state was not published')
    block = render_current_state(current_state())
    for kind in ('journal', 'handoff'):
        doc_id, title = DOCUMENTS[kind]
        current = call('query($id: String!) { document(id: $id) { id title content } }', {'id': doc_id}, key).get('document')
        if not current or current.get('title') != title or current.get('id') != doc_id:
            raise SyncError('Release document identity mismatch')
        content = current.get('content') or ''
        updated = upsert_current_state(content, block, handoff=(kind == 'handoff'))
        sync_document(updated, doc_id, key, call, expected_title=title)


def main():
    if os.environ.get('STATE_SYNC_ONLY') == 'true':
        try:
            sync_current_state(os.environ.get('LINEAR_API_KEY', ''))
            print('GPOINT_CURRENT_STATE_SYNC=PASS')
            return
        except SyncError as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    sha, run_id = os.environ['CANDIDATE_SHA'], os.environ['GITHUB_RUN_ID']
    if not re.fullmatch('[a-f0-9]{40}', sha) or not run_id.isdigit():
        raise SyncError('Invalid release identity')
    phase = os.environ['RELEASE_PHASE']
    status = candidate_status(os.environ.get('PREPARE_RESULT', '')) if phase == 'candidate' else os.environ['RELEASE_STATUS']
    allowed_statuses = {
        'candidate': ('READY', 'FAILED', 'CANCELLED', 'SKIPPED'),
        'preflight': ('PASS', 'PLANNED', 'READY_FOR_OWNER', 'FAILED', 'BLOCKED'),
        'production': ('FAILED', 'COMPLETE', 'INSTALLATION_UNCONFIRMED', 'FAILED_PRE_SWITCH', 'FAILED_MIGRATION', 'FAILED_AGATA_START', 'CANCELLED', 'BLOCKED'),
    }
    if phase not in allowed_statuses or status not in allowed_statuses[phase]:
        raise SyncError('Invalid release status')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not attempt.isdigit() or int(attempt) < 1:
        raise SyncError('Invalid release attempt')
    url = f'https://github.com/akhripunov/gpoint/actions/runs/{run_id}'
    body = f"Время: {datetime.now(timezone.utc).isoformat()}.\n\nЭтап: {phase}; статус: **{status}**.\n\nSHA: `{sha}`.\n\nПопытка GitHub Actions: `{attempt}`.\n\n[Проверки и логи]({url}/attempts/{attempt})."
    telemetry_fields = ('GITHUB_HOSTED_JOB_COUNT', 'GITHUB_HOSTED_RUNNER_SECONDS',
                        'SELF_HOSTED_JOB_COUNT', 'SELF_HOSTED_RUNNER_SECONDS',
                        'DUPLICATE_HOSTED_WORK_REMOVED', 'CACHE_PREVENTED_DOWNLOAD')
    if any(os.environ.get(key) for key in telemetry_fields):
        body += '\n\nRunner/artifact telemetry:\n' + '\n'.join(
            f'* {key}: `{os.environ.get(key, "UNKNOWN")}`' for key in telemetry_fields)
    if int(attempt) > 1:
        body += '\n\nЭто повторный запуск. Предыдущая запись сохранена; данный статус относится к указанной попытке.'
    if phase == 'candidate':
        body += '\n\nREADY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.' if status == 'READY' else '\n\nВыкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.'
    elif phase in ('preflight', 'production'):
        result = Path(os.environ['RELEASE_RESULT'])
        if phase == 'production' and result.exists():
            data = json.loads(result.read_text())
            if data.get('candidateSha') != sha:
                raise SyncError('Release result SHA mismatch')
            actual = data.get('status')
            body += f"\n\nCanonical status: **{actual}**. Production ref: `{data.get('productionRef') or 'не создан'}`."
            body += f"\n\nДо релиза: SHA `{data.get('previousProductionSha')}`; Prisma `{data.get('dbHeadBefore')}`; Alembic `{data.get('agataDbHeadBefore')}`."
            body += f"\n\nServer deploy: **{data.get('serverDeployStatus', actual)}**; Mac worker: **{data.get('workerOperationalStatus', 'NOT_VERIFIED')}**; service readiness: **{data.get('overallServiceReadiness', 'DEGRADED')}**."
            if data.get('failureStage'):
                # No raw shell output, environment, prompts, tokens or user messages.
                body += f"\n\nЭтап сбоя: `{re.sub(r'[^A-Z_0-9]', '', data['failureStage'])}`. Dev обязан сверить runtime/БД до повторной попытки."
        if phase == 'preflight':
            if status == 'READY_FOR_OWNER':
                body += f'\n\n**GPoint SHA {sha} полностью готов к production. Все mandatory gates PASS. Prisma/AGATA heads проверены canonical preflight. Разрешить production?**'
                body += '\n\nЭто отдельный read-only диагностический preflight: runtime и БД не менялись. Подтверждение должно относиться к этому полному SHA; затем запускается один canonical production workflow.'
            elif status == 'PASS':
                body += '\n\nRead-only preflight прошёл внутри этого Deploy до Apply. READY_FOR_OWNER не публикуется для уже выполняемого production цикла.'
        else:
            body += '\n\nПользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.'
    section = {'candidate': 'кандидат', 'preflight': 'preflight', 'production': 'production'}[phase]
    state_event = {'sha': sha, 'run_id': run_id, 'status': status} if phase == 'candidate' else None
    publish(section, event_identity(run_id, attempt, phase, status), body, os.environ.get('LINEAR_API_KEY', ''), state=current_state(state_event))


if __name__ == '__main__':
    try:
        main()
    except SyncError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
