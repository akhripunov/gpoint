#!/usr/bin/env python3
"""Publish bounded release facts; preserve architect-owned context/history."""
import json
import os
from pathlib import Path
import re
import sys
import base64
import io
import urllib.request
from urllib.parse import urlparse
import zipfile
import hashlib
from datetime import datetime, timezone

from release_assets import resolve_asset, validate_result
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
    except Exception as exc:
        error = SyncError(f'GitHub current-state read failed: {type(exc).__name__}')
        error.http_status = getattr(exc, 'code', None)
        raise error from exc


def artifact_json(run_id, prefix):
    release_run = None
    data = github_json(f'repos/{REPOSITORY}/actions/runs/{run_id}/artifacts')
    matches = [a for a in data.get('artifacts', []) if a.get('name', '').startswith(prefix) and not a.get('expired')]
    if not matches:
        run = github_json(f'repos/{REPOSITORY}/actions/runs/{run_id}')
        if (run.get('path') != '.github/workflows/gpoint-deploy.yml'
                or run.get('head_branch') != 'main' or run.get('event') != 'workflow_dispatch'):
            raise SyncError('Unverified Deploy result provenance')
        try:
            asset = resolve_asset('evidence', run['head_sha'], str(run_id), str(run['run_attempt']),
                                  'result.zip', lambda route: github_json(f'repos/{REPOSITORY}/{route}'))
        except SyncError as exc:
            if getattr(exc, 'http_status', None) == 404:
                raise SyncError(f'Expected one canonical result artifact for run {run_id}; found 0') from exc
            raise
        except (RuntimeError, KeyError) as exc:
            raise SyncError(f'Immutable result validation failed: {type(exc).__name__}') from exc
        release_run = run
        matches = [dict(asset, archive_download_url=f'https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset["id"]}')]
    if len(matches) != 1:
        raise SyncError(f'Expected one canonical result artifact for run {run_id}; found {len(matches)}')
    request = urllib.request.Request(matches[0]['archive_download_url'], headers={
        'Accept': 'application/octet-stream' if release_run else 'application/vnd.github+json',
        'Authorization': f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
    })
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # Artifact endpoints redirect to signed object storage URLs. Never
            # forward the GitHub bearer token to a different host.
            if urlparse(newurl).scheme != 'https':
                raise SyncError('Result redirect must use HTTPS')
            if urlparse(newurl).netloc != urlparse(req.full_url).netloc:
                return urllib.request.Request(newurl, headers={'Accept': 'application/octet-stream'})
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    try:
        with urllib.request.build_opener(SafeRedirect).open(request, timeout=30) as response:
            payload = response.read(16 * 1024**2 + 1)
            if len(payload) > 16 * 1024**2:
                raise SyncError('Result evidence exceeds size limit')
            digest = matches[0].get('digest', '')
            if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest) or hashlib.sha256(payload).hexdigest() != digest[7:]:
                raise SyncError(f'Canonical result artifact checksum mismatch for run {run_id}')
            archive = zipfile.ZipFile(io.BytesIO(payload))
            files = [name for name in archive.namelist() if name.endswith('.json')]
            if len(files) != 1 or len(archive.infolist()) != 1 or archive.infolist()[0].file_size > 16 * 1024**2:
                raise SyncError(f'Expected one JSON result in run {run_id} artifact')
            result = json.loads(archive.read(files[0]))
            if release_run:
                validate_result(result, release_run['head_sha'], run_id, release_run['run_attempt'])
            return result
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f'GitHub result artifact read failed for run {run_id}: {type(exc).__name__}') from exc


def latest_run(workflow, sha=None, successful=False):
    data = github_json(f'repos/{REPOSITORY}/actions/workflows/{workflow}/runs?branch=main&per_page=100')
    runs = [r for r in data.get('workflow_runs', []) if (sha is None or r.get('head_sha') == sha)]
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
    path = 'repos/akhripunov/gpoint/contents/docs/release/state/production.json?ref=gpoint-release-ledger'
    try:
        state_response = github_json(path)
        state = json.loads(base64.b64decode(state_response['content']).decode())
        manifest_path = state['lastReleaseId']
        manifest_response = github_json(f'repos/akhripunov/gpoint/contents/docs/release/manifests/{manifest_path}.json?ref=gpoint-release-ledger')
        manifest = json.loads(base64.b64decode(manifest_response['content']).decode())
    except (KeyError, ValueError, TypeError) as exc:
        raise SyncError('GPoint canonical production ledger or manifest is invalid') from exc
    sha = state.get('currentProductionSha')
    if (state.get('lastReleaseStatus') != 'COMPLETE' or manifest.get('status') != 'COMPLETE' or
            manifest.get('releaseId') != state.get('lastReleaseId') or
            manifest.get('candidateSha') != sha or manifest.get('productionShaAfter') != sha):
        raise SyncError('STATE_MISMATCH: GPoint canonical ledger and manifest SHA/release identity disagree')
    runs = github_json(f'repos/{REPOSITORY}/actions/workflows/gpoint-deploy.yml/runs?branch=main&per_page=100').get('workflow_runs', [])
    candidates = sorted((r for r in runs if r.get('head_sha') == state.get('currentProductionSha')),
                        key=lambda r: r.get('created_at', ''), reverse=True)
    deployment = None
    for run in candidates:
        try:
            result = artifact_json(run['id'], 'gpoint-production-result-')
        except SyncError as exc:
            if 'found 0' in str(exc):
                continue
            raise
        # Publish runs before the workflow concludes. A failed publisher also
        # leaves the overall workflow red after a successful runtime release.
        # Only the exact ledger release, its immutable result and a confirmed
        # successful deploy job prove COMPLETE; never manufacture conclusion.
        if (result.get('status') != 'COMPLETE' or
                result.get('candidateSha') != state.get('currentProductionSha') or
                result.get('productionShaAfter') != state.get('currentProductionSha') or
                result.get('releaseId') != state.get('lastReleaseId') or
                result.get('releaseId') != manifest.get('releaseId') or
                result.get('productionRef') != manifest.get('productionRef') or
                str(result.get('automationRunId')) != str(run['id'])):
            continue
        jobs = github_json(f'repos/{REPOSITORY}/actions/runs/{run["id"]}/jobs').get('jobs', [])
        deploy_jobs = [job for job in jobs if job.get('name') == 'deploy']
        if len(deploy_jobs) != 1 or deploy_jobs[0].get('status') != 'completed' or deploy_jobs[0].get('conclusion') != 'success':
            continue
        deployment = run
        break
    if deployment is None:
        raise SyncError('No verified GPoint production result and successful deploy job match the canonical ledger')
    return state, manifest, deployment


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
        'production_sha': prod_sha, 'production_checked_at': production.get('lastReleaseCompletedAt', 'unknown'),
        'production_ref': production.get('currentProductionRef', production.get('currentProductionRefName', '')),
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
        f"Production: `{state['production_sha']}`; canonical `gpoint-release-ledger/docs/release/state/production.json` + manifest; completed `{state['production_checked_at']}`; `LEDGER VERIFIED` (отдельный свежий VPS readback не выполнялся).",
        f"Сверка SHA: **{state_name}**.",
        f"Последний завершившийся успешный Prepare: `{(prep.get('head_sha') or 'unknown')}` / run `#{prep.get('id', 'unknown')}`; current-main run `#{(state['latest_main_prepare'] or {}).get('id', 'unknown')}`: **{state['candidate']}**.",
        f"Последний production Deploy: `{m.get('productionShaAfter', 'unknown')}` / run `#{deploy.get('id', 'unknown')}` / canonical **{state['release_status']}**.",
        f"GitHub Deploy workflow: `{deploy.get('status', 'unknown')}` / conclusion `{deploy.get('conclusion') or 'pending'}`; deploy job **SUCCESS** подтверждён отдельно.",
        f"Prisma head: `{m.get('dbHeadAfter', 'unconfirmed')}`; AGATA Alembic head: `{m.get('agataDbHeadAfter', 'unconfirmed')}` (manifest).",
        f"Server deploy: **{state.get('server_deploy_status', state['release_status'])}**; Mac worker: **{state.get('worker_operational_status', 'NOT_VERIFIED')}**; service readiness: **{state.get('service_readiness', 'DEGRADED')}**; dependency checked `{state.get('dependency_checked_at', 'not checked')}`.",
        "Пользовательская приёмка: **OWNER_SMOKE_PENDING**; technical COMPLETE не является подтверждением Telegram-ответа.",
        "При degraded worker повторить dependency recheck без Deploy. Реальный Telegram/GSales inference smoke подтверждается отдельно; production ради dependency check повторно не запускать.",
    ]
    return f"<!-- {CURRENT_MARKER}_BEGIN -->\n\n" + "\n\n".join(lines) + f"\n\n<!-- {CURRENT_MARKER}_END -->"


def upsert_current_state(content, block, handoff=False):
    start, end = f'<!-- {CURRENT_MARKER}_BEGIN -->', f'<!-- {CURRENT_MARKER}_END -->'
    if content.count(start) != content.count(end) or content.count(start) > 1:
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
        'preflight': ('PLANNED', 'FAILED', 'BLOCKED'),
        'production': ('FAILED', 'COMPLETE', 'RECOVERY_REQUIRED', 'FAILED_PRE_SWITCH', 'FAILED_MIGRATION', 'FAILED_AGATA_START', 'CANCELLED', 'BLOCKED'),
    }
    if phase not in allowed_statuses or status not in allowed_statuses[phase]:
        raise SyncError('Invalid release status')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not attempt.isdigit() or int(attempt) < 1:
        raise SyncError('Invalid release attempt')
    url = f'https://github.com/akhripunov/gpoint/actions/runs/{run_id}'
    body = f"Время: {datetime.now(timezone.utc).isoformat()}.\n\nЭтап: {phase}; статус: **{status}**.\n\nSHA: `{sha}`.\n\nПопытка GitHub Actions: `{attempt}`.\n\n[Проверки и логи]({url}/attempts/{attempt})."
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
            body += '\n\nЭто результат preflight, который сам не меняет runtime или БД. Выкладка, если запрошена, фиксируется отдельным production-событием.'
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
