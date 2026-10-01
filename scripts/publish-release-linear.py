#!/usr/bin/env python3
"""Publish bounded release facts; preserve architect-owned context/history."""
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

from sync_agents_to_linear import graphql_request, sync_document, SyncError

DOCUMENTS = {
    'handoff': ('1d84a3c6-f297-40c2-b9e7-ab81bc749c48', 'Архитектор — Handoff'),
    'journal': ('56f7f9f5-c6e1-42b7-8c29-5b24e6c41a06', 'Журнал релизов'),
}


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


def publish(section, event_id, body, key, call=graphql_request):
    if not key.strip():
        raise SyncError('LINEAR_API_KEY is missing: configure the repository secret; release facts were not published')
    # Journal first: a partial failure keeps the history, and retry is idempotent.
    for kind in ('journal', 'handoff'):
        doc_id, title = DOCUMENTS[kind]
        current = call('query($id: String!) { document(id: $id) { id title content } }', {'id': doc_id}, key).get('document')
        if not current or current.get('title') != title or current.get('id') != doc_id:
            raise SyncError('Release document identity mismatch')
        content = current.get('content') or ''
        updated = append_event(content, event_id, body) if kind == 'journal' else replace_section(content, section, body)
        sync_document(updated, doc_id, key, call, expected_title=title)


def main():
    sha, run_id = os.environ['CANDIDATE_SHA'], os.environ['GITHUB_RUN_ID']
    if not re.fullmatch('[a-f0-9]{40}', sha) or not run_id.isdigit():
        raise SyncError('Invalid release identity')
    phase = os.environ['RELEASE_PHASE']
    status = os.environ['RELEASE_STATUS']
    if phase not in ('candidate', 'production') or status not in ('READY', 'PLANNED', 'FAILED', 'COMPLETE', 'RECOVERY_REQUIRED', 'FAILED_PRE_SWITCH', 'FAILED_MIGRATION', 'FAILED_AGATA_START', 'CANCELLED', 'BLOCKED'):
        raise SyncError('Invalid release status')
    url = f'https://github.com/akhripunov/gpoint/actions/runs/{run_id}'
    body = f"Время: {datetime.now(timezone.utc).isoformat()}.\n\nЭтап: {phase}; статус: **{status}**.\n\nSHA: `{sha}`.\n\n[Проверки и логи]({url})."
    if phase == 'candidate':
        body += '\n\nREADY означает: unit/typecheck, реальный PostgreSQL, миграции и Docker-артефакты прошли проверки. Production не изменён. Следующее действие: владелец запускает GPoint — Deploy из main с этим SHA.' if status == 'READY' else '\n\nВыкладка заблокирована. Dev открывает первый упавший шаг, исправляет код в GitHub и готовит новый кандидат.'
    else:
        result = Path(os.environ['RELEASE_RESULT'])
        if result.exists():
            data = json.loads(result.read_text())
            if data.get('candidateSha') != sha:
                raise SyncError('Release result SHA mismatch')
            actual = data.get('status')
            body += f"\n\nCanonical status: **{actual}**. Production ref: `{data.get('productionRef') or 'не создан'}`."
            body += f"\n\nДо релиза: SHA `{data.get('previousProductionSha')}`; Prisma `{data.get('dbHeadBefore')}`; Alembic `{data.get('agataDbHeadBefore')}`."
            if data.get('failureStage'):
                # No raw shell output, environment, prompts, tokens or user messages.
                body += f"\n\nЭтап сбоя: `{re.sub(r'[^A-Z_0-9]', '', data['failureStage'])}`. Dev обязан сверить runtime/БД до повторной попытки."
        if os.environ.get('RELEASE_MODE') == 'preflight':
            body += '\n\nЭто preflight: production runtime и БД не изменены. Следующее действие: отдельный запуск в режиме production с подтверждением владельца.'
        else:
            body += '\n\nПользовательская проверка: **ожидается**. Владелец отправляет новое сообщение Models Hub и подтверждает ответ; Dev фиксирует приёмку отдельно. Не считать heartbeat подтверждением ответа.'
    section = 'кандидат' if phase == 'candidate' else ('preflight' if os.environ.get('RELEASE_MODE') == 'preflight' else 'production')
    publish(section, f'{run_id}_{phase}_{status}', body, os.environ.get('LINEAR_API_KEY', ''))


if __name__ == '__main__':
    try:
        main()
    except SyncError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
