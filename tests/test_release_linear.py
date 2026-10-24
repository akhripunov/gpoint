import importlib.util
from pathlib import Path
import sys
import socket
import ssl
import unittest
import tempfile
import re
import urllib.error
from unittest.mock import patch
import base64
import json
import io
import zipfile
import hashlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

SYNC = load('release_linear', 'publish-release-linear.py')
SELECT = load('select_release', 'select-release-run.py')
TELEMETRY = load('release_telemetry', 'release-telemetry.py')


class ReleaseTests(unittest.TestCase):
    def test_github_json_reports_safe_urlerror_category_without_raw_details(self):
        token = 'private-token-value'
        failures = [
            (urllib.error.URLError(TimeoutError('secret-host timeout')), 'timeout'),
            (urllib.error.URLError(socket.gaierror('secret-host lookup failed')), 'dns'),
            (urllib.error.URLError(ssl.SSLError('secret-host tls failed')), 'tls'),
            (urllib.error.URLError(OSError('secret-host connection failed')), 'connection'),
        ]
        for failure, category in failures:
            with self.subTest(category=category), patch.object(SYNC.urllib.request, 'urlopen', side_effect=failure):
                with self.assertRaises(SYNC.SyncError) as raised:
                    SYNC.github_json('repos/akhripunov/gpoint/commits/main', token=token)
            self.assertEqual(getattr(raised.exception, 'network_category', None), category)
            self.assertEqual(getattr(raised.exception, 'reason_type', None), type(failure.reason).__name__)
            self.assertNotIn(token, str(raised.exception))
            self.assertNotIn('secret-host', str(raised.exception))

    def test_github_json_preserves_http_status_as_safe_category(self):
        failure = urllib.error.HTTPError('https://private.invalid/path', 403, 'secret response', {}, None)
        with patch.object(SYNC.urllib.request, 'urlopen', side_effect=failure):
            with self.assertRaises(SYNC.SyncError) as raised:
                SYNC.github_json('repos/akhripunov/gpoint/commits/main', token='private-token')
        self.assertEqual(raised.exception.http_status, 403)
        self.assertEqual(raised.exception.network_category, 'http_status')
        self.assertIn('http_status_403', str(raised.exception))
        self.assertNotIn('private.invalid', str(raised.exception))
        self.assertNotIn('secret response', str(raised.exception))

    def test_handoff_preserves_architect_context_and_replaces_only_owned_section(self):
        source = '# Architect\n\nImportant constraints\n'
        first = SYNC.replace_section(source, 'кандидат', 'READY')
        second = SYNC.replace_section(first, 'кандидат', 'FAILED')
        self.assertTrue(second.startswith(source))
        self.assertIn('FAILED', second)
        self.assertNotIn('READY', second)
        self.assertEqual(second.count('## Автоматизация: кандидат'), 1)

    def test_malformed_markers_refuse_overwrite(self):
        with self.assertRaises(SYNC.SyncError):
            SYNC.replace_section('## Автоматизация: production\nimportant', 'production', 'replacement')

    def test_journal_is_idempotent_and_keeps_old_releases(self):
        first = SYNC.append_event('Original history', '123_candidate_READY', 'facts')
        self.assertEqual(first, SYNC.append_event(first, '123_candidate_READY', 'facts'))
        self.assertTrue(first.startswith('Original history'))

    def test_prepare_success_can_only_publish_ready_after_artifact_step_job_success(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        upload = workflow.index('id: ghcr-publish')
        publish = workflow.index('name: Publish candidate result to existing Linear documents')
        self.assertLess(upload, publish)
        self.assertIn('needs.prepare.result', workflow)
        self.assertIn('PREPARE_RESULT: ${{ needs.prepare.result }}', workflow)
        self.assertIn('packages: write', workflow)
        self.assertNotIn('release_assets.py publish', workflow)
        self.assertEqual([SYNC.candidate_status(x) for x in ('success', 'failure', 'cancelled', 'skipped')],
                         ['READY', 'FAILED', 'CANCELLED', 'SKIPPED'])
        publish_job = workflow.split('  publish:', 1)[1]
        self.assertIn('runs-on: ubuntu-latest', publish_job)
        self.assertIn('persist-credentials: false', publish_job)


    def test_deploy_validation_stays_hosted_while_publication_moves_to_mac(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        validate = workflow.split('  validate:', 1)[1].split('  deploy:', 1)[0]
        publish = workflow.split('  publish:', 1)[1]
        self.assertIn('runs-on: ubuntu-latest', validate)
        self.assertIn('python scripts/select-release-run.py', validate)
        self.assertIn('runs-on: ubuntu-latest', publish)
        self.assertIn('contents: read', workflow)
        self.assertIn('actions: read', workflow)
        self.assertIn('persist-credentials: false', publish)


    def test_linear_maintenance_jobs_run_on_mac_with_source_provenance(self):
        reconcile = (ROOT / '.github/workflows/gpoint-linear-reconcile.yml').read_text()
        agents = (ROOT / '.github/workflows/sync-agents-to-linear.yml').read_text()
        self.assertIn('runs-on: [self-hosted, macOS, gpoint-production]', reconcile)
        self.assertIn('ref: main', reconcile)
        self.assertIn('persist-credentials: false', reconcile)
        self.assertIn('runs-on: [self-hosted, macOS, gpoint-production]', agents)
        self.assertIn('group: gpoint-agents-linear-sync', agents)
        self.assertIn('persist-credentials: false', agents)
        self.assertIn('GITHUB_TOKEN: ${{ github.token }}', agents)

    def test_gpoint_deploy_runs_read_only_preflight_before_any_setup_or_apply(self):
        source = (ROOT / 'scripts/automated-release.mjs').read_text()
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        controller = (ROOT / '.github/workflows/gpoint-auto-preflight.yml').read_text()
        self.assertNotIn('--provision-gsales-config', source)
        self.assertNotIn('--configure-gsales-worker', source)
        self.assertLess(source.index("['release:gpoint', '--dry-run'"),
                        source.index("['release:gpoint', '--apply'"))
        self.assertIn("inputs.mode == 'production' || inputs.mode == 'task-scoped-auto'", workflow)
        self.assertIn('GPOINT_RELEASE_SKIP_BUILD', source)
        self.assertIn('workflows: ["GPoint — Prepare"]', controller)
        self.assertNotIn('workflows: ["GPoint — Prepare", "GPoint — Deploy"]', controller)

    def test_release_workflow_keeps_publishable_when_telemetry_fails(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        telemetry = workflow.split('- name: Measure release runner usage', 1)[1].split('- name:', 1)[0]
        fallback = workflow.split('- name: Mark release telemetry unknown', 1)[1].split('- name:', 1)[0]
        self.assertIn('continue-on-error: true', telemetry)
        self.assertIn("if: always() && steps.telemetry.outcome != 'success'", fallback)
        for key in ('GITHUB_HOSTED_JOB_COUNT', 'GITHUB_HOSTED_RUNNER_SECONDS', 'SELF_HOSTED_JOB_COUNT',
                    'SELF_HOSTED_RUNNER_SECONDS', 'DUPLICATE_HOSTED_WORK_REMOVED', 'CACHE_PREVENTED_DOWNLOAD'):
            self.assertIn(f'{key}=UNKNOWN', fallback)
        self.assertIn('Publish preflight result to GPoint Linear', workflow)

    def test_project_linear_document_targets_are_gpoint_scoped(self):
        self.assertIn('Архитектор — Handoff', SYNC.DOCUMENTS['handoff'][1])
        self.assertIn('Журнал релизов', SYNC.DOCUMENTS['journal'][1])
        self.assertNotIn('MAXIBOT', (ROOT / 'scripts/publish-release-linear.py').read_text())

    def test_rerun_attempts_have_distinct_audit_events(self):
        source = (ROOT / 'scripts/publish-release-linear.py').read_text()
        self.assertIn('GITHUB_RUN_ATTEMPT', source)
        self.assertIn("_attempt_{attempt}_{phase}_{status}", source)
        self.assertIn('Предыдущая запись сохранена', source)
        first = SYNC.event_identity('123', '1', 'candidate', 'READY')
        second = SYNC.event_identity('123', '2', 'candidate', 'READY')
        journal = SYNC.append_event('history', first, 'first attempt')
        journal = SYNC.append_event(journal, second, 'second attempt')
        self.assertIn(first, journal)
        self.assertIn(second, journal)


    def test_production_selector_accepts_exact_prepare_without_separate_preflight(self):
        sha = 'a' * 40
        prepare = {'id': 12, 'run_attempt': 2, 'head_sha': sha, 'head_branch': 'main',
                   'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
                   'path': '.github/workflows/gpoint-prepare.yml'}
        calls = []

        def github(path):
            calls.append(path)
            if path == 'git/ref/heads/main':
                return {'object': {'sha': sha}}
            if path.startswith('actions/workflows/gpoint-prepare.yml/runs'):
                return {'workflow_runs': [prepare]}
            raise AssertionError(f'unexpected GitHub read: {path}')

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'github-output'
            with patch.dict('os.environ', {
                'CANDIDATE_SHA': sha, 'GITHUB_REF': 'refs/heads/main', 'RELEASE_MODE': 'production',
                'APPROVE_PRODUCTION': 'true', 'GITHUB_TOKEN': 'test-token',
                'OWNER_APPROVAL_ACTOR': 'akhripunov',
                'OWNER_TRIGGERING_ACTOR': 'akhripunov',
                'OWNER_REPOSITORY': 'akhripunov',
                'GITHUB_OUTPUT': str(output),
            }), patch.object(SELECT, 'github', side_effect=github), \
                    patch.object(SELECT, 'validate_expected_run') as validate:
                SELECT.main()
            rendered = output.read_text()
        validate.assert_called_once_with(prepare, None, None)
        self.assertFalse(any('gpoint-deploy.yml/runs' in path for path in calls))
        self.assertIn('run_id=12', rendered)
        self.assertIn('run_attempt=2', rendered)

    def test_automatic_preflight_run_name_is_distinguishable_from_production(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        self.assertIn('run-name: GPoint ${{ inputs.mode }} ${{ inputs.candidate }}', workflow)

    def test_runner_telemetry_classifies_runner_wall_clock_and_ignores_queued_jobs(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 5, 10, 0, 30, tzinfo=timezone.utc)
        values = TELEMETRY.summarize([[
            {'labels': ['ubuntu-latest'], 'runner_name': 'GitHub Actions 1',
             'started_at': '2026-10-05T10:00:00Z', 'completed_at': '2026-10-05T10:00:12Z'},
            {'labels': ['self-hosted', 'macOS', 'gpoint-production'], 'runner_name': 'gpoint-owner-mac',
             'started_at': '2026-10-05T10:00:05Z', 'completed_at': None},
            {'labels': ['ubuntu-latest'], 'status': 'queued', 'started_at': None},
        ]], now)
        self.assertEqual(values, {'GITHUB_HOSTED_JOB_COUNT': 1,
                                  'GITHUB_HOSTED_RUNNER_SECONDS': 12,
                                  'SELF_HOSTED_JOB_COUNT': 1,
                                  'SELF_HOSTED_RUNNER_SECONDS': 25})

    def test_candidate_does_not_replace_production(self):
        source = SYNC.replace_section('Architect', 'production', 'production facts')
        self.assertIn('production facts', SYNC.replace_section(source, 'кандидат', 'candidate facts'))

    def test_current_state_is_idempotent_and_handoff_first(self):
        block = SYNC.render_current_state({
            'synced_at': '2026-10-03T07:00:00+00:00', 'main_sha': 'a' * 40,
            'main_checked_at': 'now', 'production_sha': 'b' * 40, 'production_checked_at': 'then',
            'production_ref': 'release-x', 'release_status': 'COMPLETE', 'in_sync': False,
            'prepare': {'head_sha': 'a' * 40, 'id': 12}, 'candidate': 'READY',
            'latest_main_prepare': {'conclusion': 'success'}, 'deploy': {'id': 14},
            'manifest': {'productionShaAfter': 'b' * 40, 'dbHeadAfter': 'p1', 'agataDbHeadAfter': 'a1'},
        })
        handoff = SYNC.upsert_current_state('## Manual section\nkeep', block, handoff=True)
        self.assertTrue(handoff.startswith('<!-- GPOINT_CURRENT_STATE_BEGIN -->'))
        self.assertIn('## Manual section\nkeep', handoff)
        self.assertEqual(SYNC.upsert_current_state(handoff, block, handoff=True), handoff)
        journal = SYNC.upsert_current_state('# Journal\n\nHistory', block)
        self.assertIn('GPOINT_CURRENT_STATE_BEGIN', journal.split('History')[0])
        self.assertIn('MAIN_AHEAD_OF_PRODUCTION', block)
        self.assertNotIn('production SHA', block.lower().split('prepare')[1].split('deploy')[0])

    def test_current_state_rejects_bad_markers_and_preserves_unowned_text(self):
        block = SYNC.render_current_state({'synced_at':'t','main_sha':'a'*40,'main_checked_at':'t',
            'production_sha':'a'*40,'production_checked_at':'t','production_ref':'r','release_status':'COMPLETE',
            'in_sync':True,'prepare':None,'candidate':'READY','latest_main_prepare':None,'deploy':{'id':1},
            'manifest':{'productionShaAfter':'a'*40,'dbHeadAfter':'p','agataDbHeadAfter':'a'}})
        original = 'Architect rules\n\n<!-- GPOINT_CURRENT_STATE_BEGIN -->\nstale\n<!-- GPOINT_CURRENT_STATE_END -->\n\nManual notes'
        updated = SYNC.upsert_current_state(original, block, handoff=True)
        self.assertIn('Architect rules', updated)
        self.assertIn('Manual notes', updated)
        with self.assertRaises(SYNC.SyncError):
            SYNC.upsert_current_state('<!-- GPOINT_CURRENT_STATE_BEGIN -->x', block)

    def test_current_state_collapses_only_duplicate_adjacent_end_markers(self):
        block = SYNC.render_current_state({'synced_at':'t','main_sha':'a'*40,'main_checked_at':'t',
            'production_sha':'a'*40,'production_checked_at':'t','production_ref':'r','release_status':'COMPLETE',
            'in_sync':True,'prepare':None,'candidate':'READY','latest_main_prepare':None,'deploy':{'id':1},
            'manifest':{'productionShaAfter':'a'*40,'dbHeadAfter':'p','agataDbHeadAfter':'a'}})
        duplicated = 'Manual notes\n\n<!-- GPOINT_CURRENT_STATE_BEGIN -->old<!-- GPOINT_CURRENT_STATE_END --><!-- GPOINT_CURRENT_STATE_END -->\n\nkeep this'
        updated = SYNC.upsert_current_state(duplicated, block, handoff=True)
        self.assertEqual(updated.count('<!-- GPOINT_CURRENT_STATE_BEGIN -->'), 1)
        self.assertEqual(updated.count('<!-- GPOINT_CURRENT_STATE_END -->'), 1)
        self.assertIn('Manual notes', updated)
        self.assertIn('keep this', updated)

    def test_latest_current_sha_failure_does_not_reuse_older_success(self):
        runs = {'total_count': 2, 'workflow_runs': [
            {'id':1,'head_sha':'a'*40,'created_at':'2026-10-03T06:00:00Z','conclusion':'success','status':'completed'},
            {'id':2,'head_sha':'a'*40,'created_at':'2026-10-03T07:00:00Z','conclusion':'failure','status':'completed'},
        ]}
        original = SYNC.github_json
        try:
            SYNC.github_json = lambda path, token=None: runs
            latest = SYNC.latest_run('gpoint-prepare.yml', sha='a'*40)
            self.assertEqual(latest['id'], 2)
            self.assertNotEqual(latest['conclusion'], 'success')
        finally:
            SYNC.github_json = original

    def test_current_state_is_gpoint_scoped(self):
        source = (ROOT / 'scripts/publish-release-linear.py').read_text()
        self.assertIn("CURRENT_MARKER = 'GPOINT_CURRENT_STATE'", source)
        self.assertIn("REPOSITORY = 'akhripunov/gpoint'", source)
        self.assertNotIn('MAXIBOT_CURRENT_STATE', source)

    def test_self_run_in_progress_reports_gates_pending_not_workflow_success(self):
        run = {'id': 44, 'head_sha': 'a' * 40, 'status': 'in_progress', 'conclusion': None}
        event = {'sha': 'a' * 40, 'run_id': '44', 'status': 'READY'}
        self.assertEqual(SYNC.current_candidate_status('a' * 40, run, event), 'PREPARE_GATES_PASS_WORKFLOW_PENDING')
        self.assertEqual(SYNC.current_candidate_status('a' * 40, run), 'NOT_PREPARED_FOR_CURRENT_MAIN')
        self.assertEqual(run['status'], 'in_progress')

    def test_completed_run_reconciliation_and_stale_run_protection(self):
        completed = {'id': 44, 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success'}
        self.assertEqual(SYNC.current_candidate_status('a' * 40, completed), 'READY')
        stale_event = {'sha': 'a' * 40, 'run_id': '43', 'status': 'READY'}
        current = {'id': 44, 'head_sha': 'a' * 40, 'status': 'in_progress'}
        self.assertEqual(SYNC.current_candidate_status('a' * 40, current, stale_event), 'NOT_PREPARED_FOR_CURRENT_MAIN')
        workflow = (ROOT / '.github/workflows/gpoint-linear-reconcile.yml').read_text()
        self.assertIn('types: [completed]', workflow)
        self.assertIn("STATE_SYNC_ONLY: 'true'", workflow)
        self.assertIn('group: gpoint-linear-release', workflow)

    def production_fixture(self, workflow_status='in_progress', conclusion=None,
                           deploy_conclusion='success', result_overrides=None):
        sha = 'a' * 40
        state = {'currentProductionSha': sha, 'lastReleaseId': 'release-new', 'lastReleaseStatus': 'COMPLETE'}
        manifest = {'releaseId': 'release-new', 'productionRef': 'prod-new', 'candidateSha': sha,
                    'productionShaAfter': sha, 'status': 'COMPLETE'}
        run = {'id': 55, 'head_sha': sha, 'status': workflow_status,
               'conclusion': conclusion, 'created_at': '2026-10-03T08:00:00Z'}
        result = {'status': 'COMPLETE', 'candidateSha': sha, 'productionShaAfter': sha,
                  'releaseId': 'release-new', 'productionRef': 'prod-new', 'automationRunId': '55'}
        result.update(result_overrides or {})
        def github(path, token=None):
            if 'production.json?' in path:
                return {'content': base64.b64encode(json.dumps(state).encode()).decode()}
            if '/manifests/' in path:
                return {'content': base64.b64encode(json.dumps(manifest).encode()).decode()}
            if path.endswith('/55/jobs'):
                return {'jobs': [{'name': 'deploy', 'status': 'completed', 'conclusion': deploy_conclusion}]}
            return {'workflow_runs': [run]}
        return github, run, result







    def test_deploy_evidence_uses_exact_successful_job_log_digest(self):
        sha, run_id, attempt = 'a' * 40, '55', '2'
        result = {'candidateSha': sha, 'automationRunId': run_id, 'automationRunAttempt': attempt,
                  'deployRunId': run_id, 'deployRunAttempt': attempt, 'deploymentId': 99, 'status': 'COMPLETE'}
        blob = json.dumps(result).encode()
        blob_digest = 'sha256:' + hashlib.sha256(blob).hexdigest()
        manifest = {'schemaVersion': 2, 'mediaType': SYNC.OCI_MANIFEST_TYPE,
                    'artifactType': SYNC.DEPLOY_EVIDENCE_TYPE,
                    'layers': [{'mediaType': SYNC.DEPLOY_EVIDENCE_LAYER, 'digest': blob_digest, 'size': len(blob)}]}
        manifest_bytes = json.dumps(manifest).encode()
        digest = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
        plain_logs = f'2026-10-19T01:23:45.123Z DEPLOY_EVIDENCE_DIGEST={digest}\n'.encode()
        job = {'id': 700, 'name': 'deploy', 'run_id': 55, 'run_attempt': 2, 'head_sha': sha,
               'status': 'completed', 'conclusion': 'success', 'steps': [
                   {'number': 10, 'name': SYNC.DEPLOY_EVIDENCE_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        routes = []
        def github(path, token=None):
            routes.append(path)
            return {'jobs': [job], 'total_count': 1} if '/jobs?' in path else run
        def get(url, username, password, accept, limit):
            if '/blobs/' in url: return blob, {}
            return manifest_bytes, {'Docker-Content-Digest': digest}
        run = {'id': int(run_id), 'head_sha': sha, 'head_branch': 'main', 'event': 'workflow_dispatch',
               'path': '.github/workflows/gpoint-deploy.yml', 'run_attempt': int(attempt)}
        with patch.dict(SYNC.os.environ, {'GHCR_READ_USERNAME': 'owner', 'GHCR_READ_TOKEN': 'token', 'GITHUB_TOKEN': 'token'}), \
             patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'download_job_logs', return_value=plain_logs), \
             patch.object(SYNC, 'ghcr_get', side_effect=get):
            self.assertEqual(SYNC.artifact_json(run_id, attempt=attempt, sha=sha, deployment_id=99), result)
        self.assertIn(f'repos/{SYNC.REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}', routes)
        self.assertNotIn(f'repos/{SYNC.REPOSITORY}/actions/runs/{run_id}', routes)

    def test_deploy_evidence_rejects_wrong_workflow_identity_content_digest_and_deployment(self):
        sha, run_id, attempt = 'b' * 40, '56', '1'
        run = {'id': 56, 'head_sha': sha, 'head_branch': 'main', 'event': 'workflow_dispatch',
               'path': '.github/workflows/gpoint-deploy.yml', 'run_attempt': 1}
        with patch.object(SYNC, 'github_json', return_value={**run, 'head_branch': 'other'}), \
             self.assertRaisesRegex(SYNC.SyncError, 'workflow identity'):
            SYNC.artifact_json(run_id, attempt=attempt, sha=sha)
        result = {'candidateSha': sha, 'automationRunId': run_id, 'automationRunAttempt': attempt,
                  'deployRunId': run_id, 'deployRunAttempt': attempt, 'deploymentId': 7}
        blob = json.dumps(result).encode()
        manifest = {'mediaType': SYNC.OCI_MANIFEST_TYPE, 'artifactType': SYNC.DEPLOY_EVIDENCE_TYPE,
                    'layers': [{'mediaType': SYNC.DEPLOY_EVIDENCE_LAYER,
                                'digest': 'sha256:' + hashlib.sha256(blob).hexdigest(), 'size': len(blob)}]}
        manifest_bytes = json.dumps(manifest).encode()
        digest = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
        job = {'id': 701, 'name': 'deploy', 'run_id': 56, 'run_attempt': 1, 'head_sha': sha,
               'status': 'completed', 'conclusion': 'success', 'steps': [
                   {'number': 10, 'name': SYNC.DEPLOY_EVIDENCE_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        def github(path, token=None):
            return {'jobs': [job], 'total_count': 1} if '/jobs?' in path else run
        def get(url, username, password, accept, limit):
            return (blob, {}) if '/blobs/' in url else (manifest_bytes, {'Docker-Content-Digest': digest})
        with patch.dict(SYNC.os.environ, {'GHCR_READ_USERNAME': 'owner', 'GHCR_READ_TOKEN': 'token'}), \
             patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'download_job_logs', return_value=f'DEPLOY_EVIDENCE_DIGEST={digest}\n'.encode()), \
             patch.object(SYNC, 'ghcr_get', side_effect=get):
            with self.assertRaisesRegex(SYNC.SyncError, 'exact Deployment'):
                SYNC.artifact_json(run_id, attempt=attempt, sha=sha, deployment_id=8)
            with patch.object(SYNC, 'ghcr_get', side_effect=lambda *args: (b'corrupt', {}) if '/blobs/' in args[0]
                              else (manifest_bytes, {'Docker-Content-Digest': digest})):
                with self.assertRaisesRegex(SYNC.SyncError, 'content digest'):
                    SYNC.artifact_json(run_id, attempt=attempt, sha=sha)

    def test_deploy_evidence_digest_accepts_failed_job_when_publisher_succeeds(self):
        sha, run_id, attempt = 'c' * 40, '57', '1'
        job = {'id': 702, 'name': 'deploy', 'run_id': 57, 'run_attempt': 1, 'head_sha': sha,
               'status': 'completed', 'conclusion': 'failure', 'steps': [
                   {'number': 4, 'name': SYNC.DEPLOY_EVIDENCE_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        repeated = io.BytesIO()
        with zipfile.ZipFile(repeated, 'w') as archive:
            archive.writestr(f'deploy/4_{SYNC.DEPLOY_EVIDENCE_STEP}.txt',
                             'DEPLOY_EVIDENCE_DIGEST=sha256:' + 'd' * 64 + '\n')
        with patch.object(SYNC, 'github_json', return_value={'jobs': [job], 'total_count': 1}), \
             patch.object(SYNC, 'download_job_logs', return_value=repeated.getvalue()):
            self.assertEqual(SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha), 'sha256:' + 'd' * 64)
        with zipfile.ZipFile(repeated, 'w') as archive:
            archive.writestr(f'deploy/4_{SYNC.DEPLOY_EVIDENCE_STEP}.txt',
                             'DEPLOY_EVIDENCE_DIGEST=sha256:' + 'd' * 64 + '\nDEPLOY_EVIDENCE_DIGEST=sha256:' + 'd' * 64 + '\n')
        with patch.object(SYNC, 'github_json', return_value={'jobs': [job], 'total_count': 1}), \
             patch.object(SYNC, 'download_job_logs', return_value=repeated.getvalue()):
            with self.assertRaisesRegex(SYNC.SyncError, 'missing or ambiguous'):
                SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha)

    def test_deploy_evidence_plain_logs_reject_malformed_duplicate_and_oversized_markers(self):
        sha, run_id, attempt = 'f' * 40, '59', '1'
        digest = 'sha256:' + 'd' * 64
        job = {'id': 704, 'name': 'deploy', 'run_id': 59, 'run_attempt': 1, 'head_sha': sha,
               'status': 'completed', 'conclusion': 'failure', 'steps': [
                   {'number': 4, 'name': SYNC.DEPLOY_EVIDENCE_STEP, 'status': 'completed', 'conclusion': 'success'}]}
        cases = [
            (f'2026-10-19T01:23:45Z DEPLOY_EVIDENCE_DIGEST={digest}\n'.encode(), digest),
            (f'2026-10-19T01:23:45Z \x1b[36;1mecho "DEPLOY_EVIDENCE_DIGEST=$digest"\x1b[0m\n'
             f'2026-10-19T01:23:46Z DEPLOY_EVIDENCE_DIGEST={digest}\n'.encode(), digest),
            (f'2026-10-19T01:23:45Z DEPLOY_EVIDENCE_DIGEST={digest}\nDEPLOY_EVIDENCE_DIGEST={digest}\n'.encode(), 'ambiguous'),
            (f'2026-10-19T01:23:45Z DEPLOY_EVIDENCE_DIGEST={digest} extra\n'.encode(), 'malformed'),
            (b'PK\x03\x04truncated', 'BadZipFile'),
            (b'x' * (SYNC.MAX_DEPLOY_LOG_BYTES + 1), 'bounded evidence limit'),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), \
                 patch.object(SYNC, 'github_json', return_value={'jobs': [job], 'total_count': 1}), \
                 patch.object(SYNC, 'download_job_logs', return_value=payload):
                if expected == digest:
                    self.assertEqual(SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha), digest)
                else:
                    with self.assertRaisesRegex(SYNC.SyncError, expected):
                        SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha)

    def test_reconciliation_digest_uses_its_own_publisher_step_name(self):
        sha, run_id, attempt = 'e' * 40, '61', '2'
        step_name = SYNC.RECONCILIATION_EVIDENCE_STEP
        job = {'id': 706, 'name': 'reconcile-authorization', 'run_id': 61, 'run_attempt': 2,
               'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'steps': [
                   {'number': 9, 'name': step_name, 'status': 'completed', 'conclusion': 'success'}]}
        digest = 'sha256:' + 'b' * 64
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w') as archive:
            archive.writestr(f'reconcile-authorization/9_{step_name}.txt', f'DEPLOY_EVIDENCE_DIGEST={digest}\n')
        with patch.object(SYNC, 'github_json', return_value={'jobs': [job], 'total_count': 1}), \
             patch.object(SYNC, 'download_job_logs', return_value=zipped.getvalue()):
            self.assertEqual(SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha,
                job_name='reconcile-authorization', step_name=step_name), digest)
        failed_job = {**job, 'conclusion': 'failure'}
        with patch.object(SYNC, 'github_json', return_value={'jobs': [failed_job], 'total_count': 1}), \
             patch.object(SYNC, 'download_job_logs') as download:
            with self.assertRaisesRegex(SYNC.SyncError, 'reconciliation job did not complete successfully'):
                SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha,
                    job_name='reconcile-authorization', step_name=step_name)
            download.assert_not_called()

    def test_authorization_reconciliation_attestation_requires_original_failure_and_exact_current_main(self):
        candidate, executor, deployment_id = 'a' * 40, 'b' * 40, 77
        original = {'status': 'INSTALLATION_UNCONFIRMED', 'releaseState': 'INSTALLATION_UNCONFIRMED',
                    'candidateSha': candidate, 'deploymentId': deployment_id, 'authorizationMode': 'TASK_SCOPED_AUTO',
                    'authorizationId': 'ME-42', 'authorizationConsumed': False,
                    'executionFailure': {'stage': 'POST_APPLY_READBACK', 'executionStatus': 'FAILED',
                                         'diagnostic': 'Task authorization consumption failed (HTTP 403)'},
                    'applicationVerification': 'VPS_READBACK_VERIFIED', 'serverDeployStatus': 'COMPLETE',
                    'prepareRunId': '400', 'prepareRunAttempt': '1',
                    'ciReceipt': {'ghcrManifestDigest': 'sha256:' + '1' * 64},
                    'componentImages': {'app': {'imageId': 'sha256:' + '2' * 64}},
                    'dbHeadAfter': 'prisma_001', 'agataDbHeadAfter': 'agata0010',
                    'gsalesDbHeadAfter': '001_gsales', 'gsalesMigrationSha256After': '3' * 64,
                    'workerOperationalStatus': 'HEALTHY',
                    'artifactIdentities': {'api': {'sourceSha': candidate}},
                    'productionShaAfter': candidate, 'agataRuntimeSha': candidate, 'miniappSourceSha': candidate,
                    'gsalesRuntime': {'migrationSha256': '3' * 64}}
        integrity = {'manifestDigest': 'sha256:' + '4' * 64, 'contentSha256': '5' * 64,
                     'runId': '500', 'runAttempt': '2'}
        automation = {'id': 600, 'run_attempt': 3, 'head_sha': executor,
                      'path': '.github/workflows/gpoint-deploy.yml', 'head_branch': 'main',
                      'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success'}

        def evidence(original_value=original, overrides=None):
            value = {
                'schema': 1, 'operation': SYNC.AUTHORIZATION_RECONCILIATION_OPERATION,
                'status': 'COMPLETE', 'releaseState': 'COMPLETE', 'candidateSha': candidate,
                'executorSha': executor, 'deployRunId': '500', 'deployRunAttempt': '2',
                'automationRunId': '600', 'automationRunAttempt': '3', 'deploymentId': deployment_id,
                'prepareRunId': '400', 'prepareRunAttempt': '1',
                'authorizationMode': 'TASK_SCOPED_AUTO', 'authorizationId': 'ME-42',
                'authorizationConsumed': True, 'applicationVerification': 'VPS_READBACK_VERIFIED',
                'serverDeployStatus': 'COMPLETE',
                'originalEvidence': integrity,
                'reconciledFrom': {'status': original_value['status'], 'releaseState': original_value['releaseState'],
                                   'executionFailure': original_value['executionFailure']},
                'reconciliation': {'operation': SYNC.AUTHORIZATION_RECONCILIATION_OPERATION,
                                   'originalDeploymentId': deployment_id, 'consumedCandidateSha': candidate,
                                   'statusVerified': True, 'liveVerified': True},
                **{key: original_value[key] for key in ('ciReceipt', 'componentImages', 'dbHeadAfter',
                    'agataDbHeadAfter', 'gsalesDbHeadAfter', 'gsalesMigrationSha256After', 'workerOperationalStatus',
                    'artifactIdentities', 'productionShaAfter', 'agataRuntimeSha', 'miniappSourceSha', 'gsalesRuntime')},
            }
            value.update(overrides or {})
            return value

        def call(original_value=original, attestation=None, current_main=executor, run=automation):
            def github(route):
                if route.endswith('/commits/main'):
                    return {'sha': current_main}
                if '/actions/runs/500/attempts/2' in route:
                    return {'id': 500, 'run_attempt': 2, 'head_sha': candidate,
                            'path': '.github/workflows/gpoint-deploy.yml', 'head_branch': 'main',
                            'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure'}
                if '/actions/runs/600/attempts/3' in route:
                    return run
                raise AssertionError(route)
            with patch.object(SYNC, 'github_json', side_effect=github), \
                 patch.object(SYNC, 'artifact_json', side_effect=[(original_value, integrity['manifestDigest'], integrity['contentSha256']),
                                                                    attestation or evidence(original_value)]):
                return SYNC.authorization_reconciliation_evidence('600', '3', candidate, deployment_id, '500', '2')

        self.assertEqual(call()['status'], 'COMPLETE')
        active = {**automation, 'status': 'in_progress', 'conclusion': None}
        with patch.dict(SYNC.os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                         'GITHUB_RUN_ID': '600', 'GITHUB_RUN_ATTEMPT': '3'}):
            self.assertEqual(call(run=active)['status'], 'COMPLETE')
        with patch.dict(SYNC.os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                         'GITHUB_RUN_ID': '601', 'GITHUB_RUN_ATTEMPT': '3'}):
            with self.assertRaisesRegex(SYNC.SyncError, 'current-main'):
                call(run=active)
        with patch.dict(SYNC.os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                         'GITHUB_RUN_ID': '600', 'GITHUB_RUN_ATTEMPT': '3'}):
            with self.assertRaisesRegex(SYNC.SyncError, 'current-main'):
                call(current_main='c' * 40, run=active)
        with patch.dict(SYNC.os.environ, {'GITHUB_ACTIONS': 'false', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                         'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1'}):
            self.assertEqual(call(current_main='c' * 40)['status'], 'COMPLETE')
        for overrides in ({'executorSha': 'c' * 40}, {'deploymentId': 78},
                          {'authorizationConsumed': False}, {'applicationVerification': 'NOT_VERIFIED'},
                          {'originalEvidence': {**integrity, 'contentSha256': '0' * 64}},
                          {'reconciliation': {'operation': SYNC.AUTHORIZATION_RECONCILIATION_OPERATION,
                           'originalDeploymentId': deployment_id, 'consumedCandidateSha': candidate,
                           'statusVerified': False, 'liveVerified': True}},
                          {'componentImages': {'app': {'imageId': 'sha256:tampered'}}},
                          {'artifactIdentities': {'api': {'sourceSha': 'c' * 40}}},
                          {'gsalesRuntime': {'migrationSha256': '0' * 64}}):
            with self.subTest(overrides=overrides), self.assertRaises(SYNC.SyncError):
                call(attestation=evidence(overrides=overrides))
        with self.assertRaisesRegex(SYNC.SyncError, 'unconsumed task-scoped failure'):
            call(original_value={**original, 'authorizationConsumed': True})
        with self.assertRaisesRegex(SYNC.SyncError, 'unconsumed task-scoped failure'):
            call(original_value={**original, 'executionFailure': {
                'stage': 'POST_APPLY_READBACK', 'executionStatus': 'FAILED', 'diagnostic': 'HTTP 500'}})
        with self.assertRaisesRegex(SYNC.SyncError, 'unconsumed task-scoped failure'):
            call(original_value={**original, 'applicationVerification': 'NOT_VERIFIED'})

    def test_deploy_evidence_digest_requires_publisher_step_success(self):
        sha, run_id, attempt = 'e' * 40, '58', '1'
        job = {'id': 703, 'name': 'deploy', 'run_id': 58, 'run_attempt': 1, 'head_sha': sha,
               'status': 'completed', 'conclusion': 'failure', 'steps': [
                   {'number': 4, 'name': SYNC.DEPLOY_EVIDENCE_STEP, 'status': 'completed', 'conclusion': 'failure'}]}
        with patch.object(SYNC, 'github_json', return_value={'jobs': [job], 'total_count': 1}):
            with self.assertRaisesRegex(SYNC.SyncError, 'publication step did not succeed'):
                SYNC.deploy_evidence_digest_from_logs(run_id, attempt, sha)

    def test_newer_failed_run_blocks_older_success(self):
        base = {'head_sha': 'a' * 40, 'head_branch': 'main', 'event': 'push', 'status': 'completed'}
        with self.assertRaisesRegex(RuntimeError, 'not successful'):
            SELECT.select_run('a' * 40, [dict(base, id=1, conclusion='success'), dict(base, id=2, conclusion='failure')])

    def test_foreign_branch_and_sha_do_not_qualify(self):
        with self.assertRaisesRegex(RuntimeError, 'No preparation'):
            SELECT.select_run('a' * 40, [{'head_sha': 'b' * 40, 'head_branch': 'main', 'event': 'push'},
                                       {'head_sha': 'a' * 40, 'head_branch': 'evil', 'event': 'push'}])

    def test_exact_completed_run_selected(self):
        run = {'id': 22, 'head_sha': 'a' * 40, 'head_branch': 'main', 'event': 'push',
               'status': 'completed', 'conclusion': 'success', 'run_attempt': 2}
        self.assertEqual(SELECT.select_run('a' * 40, [run]), run)
        SELECT.validate_expected_run(run, '22', '2')

    def test_exact_prepare_run_and_attempt_are_pinned_after_selection(self):
        run = {'id': 22, 'run_attempt': 2}
        for expected_run, expected_attempt in [('21', '2'), ('22', '1'), ('22', ''), ('', '2')]:
            with self.subTest(expected_run=expected_run, expected_attempt=expected_attempt), self.assertRaises(RuntimeError):
                SELECT.validate_expected_run(run, expected_run, expected_attempt)

    def test_preflight_can_run_without_production_permission(self):
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'preflight', 'false')
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            SELECT.validate_request('a' * 40, 'refs/heads/main', 'production', 'false')
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'production', 'true')

    def test_branch_and_invalid_release_mode_are_rejected(self):
        for ref, mode in [('refs/heads/other', 'production'), ('refs/heads/main', 'anything')]:
            with self.assertRaises(RuntimeError):
                SELECT.validate_request('a' * 40, ref, mode, 'true')

    def test_exact_sha_production_requires_repository_owner_to_dispatch_and_trigger(self):
        SELECT.validate_owner_approval('production', 'akhripunov', 'akhripunov', 'akhripunov')
        for actor, triggering, owner in [
            ('maintainer', 'maintainer', 'akhripunov'),
            ('akhripunov', 'maintainer', 'akhripunov'),
            ('akhripunov', 'akhripunov', 'other-repository-owner'),
        ]:
            with self.subTest(actor=actor, triggering=triggering, owner=owner), \
                    self.assertRaisesRegex(RuntimeError, 'repository OWNER'):
                SELECT.validate_owner_approval('production', actor, triggering, owner)
        SELECT.validate_owner_approval('task-scoped-auto', 'github-actions[bot]',
                                       'github-actions[bot]', 'akhripunov')



class WorkerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.contract = load('worker_release_contract', 'verify-release-worker-compatibility.py')
        self.point = '11111111-1111-4111-8111-111111111111'
        self.sales = '22222222-2222-4222-8222-222222222222'
        self.values = {'GPOINT_AGATA_WORKER_TOKEN':'point-test','GSALES_AGATA_WORKER_TOKEN':'sales-test',
          'GPOINT_AGATA_TENANT_ID':self.point,'GSALES_AGATA_TENANT_ID':self.sales,
          'GSALES_AGATA_WORKER_URL':'https://gpoint.amiora.one/gsales/agata-worker'}

    def test_launch_overrides_cannot_bypass_static_contract(self):
        canonical={'HOME':str(self.contract.lane.HOME),'GPOINT_AGATA_WORKER_CONFIG':str(self.contract.lane.CONFIG)}
        self.contract.verify_launch_environment(canonical,{})
        for key in self.contract.CONTRACT_ENV_KEYS:
            with self.subTest(key=key,empty=True):
                with self.assertRaisesRegex(RuntimeError,'launch_contract_override'):
                    self.contract.verify_launch_environment({key:''},{})
            for source in ('plist','inherited'):
                with self.subTest(key=key,source=source):
                    with self.assertRaisesRegex(RuntimeError,'launch_contract_override'):
                        self.contract.verify_launch_environment({key:'wrong'} if source=='plist' else {}, {key:'wrong'} if source=='inherited' else {})

    def test_source_protocol_mismatch_blocks_before_any_worker_operation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'repo/services/agata/worker'; support=root/'installed'
            installed=support/'runtime/current'
            source.mkdir(parents=True); installed.mkdir(parents=True)
            for name in self.contract.lane.ASSETS:
                (source/name).write_text('candidate protocol')
                (installed/name).write_text('incompatible protocol')
            with patch.object(self.contract.lane,'REPO',root/'repo'), patch.object(self.contract.lane,'SUPPORT',support), patch.object(self.contract.lane,'command',side_effect=lambda args: '' if 'status' in args else 'a'*40):
                with self.assertRaisesRegex(RuntimeError,'candidate_hash_mismatch'):
                    self.contract.lane.verify_candidate('a'*40)

    def test_read_only_preflight_reports_candidate_worker_source_drift_without_weakening_apply(self):
        import tempfile
        import plistlib
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); repo=base/'repo'; home=base/'home'
            support=home/'Library/Application Support/GPoint/agata-codex-worker'
            runtime=support/'runtime/current'; source=repo/'services/agata/worker'
            source.mkdir(parents=True); runtime.mkdir(parents=True)
            for name in self.contract.lane.ASSETS:
                (source/name).write_text('candidate source '+name)
                (runtime/name).write_text('installed source '+name if name=='gpoint_mac_worker.py' else 'candidate source '+name)
            launch_agents=home/'Library/LaunchAgents'; launch_agents.mkdir(parents=True)
            plist=launch_agents/(self.contract.lane.LABEL+'.plist')
            plist.write_bytes(plistlib.dumps({'Label':self.contract.lane.LABEL,
                'ProgramArguments':[str(runtime/'run_codex_worker.sh'),'watch'],
                'WorkingDirectory':str(runtime),'RunAtLoad':True,'KeepAlive':True}))
            plist.chmod(0o600)
            def git_command(args):
                return self.point if args[1:3]==['rev-parse','HEAD'] else ''
            with patch.object(self.contract.lane,'REPO',repo), patch.object(self.contract.lane,'HOME',home), \
                 patch.object(self.contract.lane,'SUPPORT',support), patch.object(self.contract.lane,'command',side_effect=git_command):
                evidence=self.contract.lane.verify_candidate(self.point, allow_source_drift=True)
                self.assertEqual(evidence['sourceStatus'],'CANDIDATE_DIFFERS')
                self.assertFalse(evidence['assets']['gpoint_mac_worker.py']['matches'])
                self.assertTrue(evidence['assets']['agata_worker_pool.py']['matches'])
                with self.assertRaisesRegex(RuntimeError,'candidate_hash_mismatch'):
                    self.contract.lane.verify_candidate(self.point)

    def test_dry_run_uses_read_only_worker_compatibility_and_apply_stays_strict(self):
        source=(ROOT/'scripts/release-gpoint.mjs').read_text()
        self.assertRegex(source,r'const workerCompatibility = verifyWorkerCompatibility\(candidate, baseline\.facts, [\'\"]preflight[\'\"]\)')
        self.assertRegex(source,r'const workerCompatibility = verifyWorkerCompatibility\(candidate, baseline\.facts, [\'\"]apply[\'\"]\)')

    def test_compatibility_is_independent_of_live_worker_health(self):
        self.assertEqual(self.contract.verify_contract(self.values,self.point,self.sales), {'compatibility':'PASS'})

    def test_known_static_credential_mismatch_is_compatibility_failure(self):
        expected={'GPOINT_AGATA_WORKER_TOKEN':'point-test','GSALES_WORKER_TOKEN':'sales-test'}
        self.assertEqual(self.contract.verify_contract(self.values,self.point,self.sales,expected_credentials=expected), {'compatibility':'PASS'})
        with self.assertRaises(RuntimeError):
            self.contract.verify_contract(self.values,self.point,self.sales,expected_credentials={**expected,'GSALES_WORKER_TOKEN':'other-test'})

    def test_read_only_contract_evidence_reports_lanes_without_credentials(self):
        expected={'GPOINT_AGATA_WORKER_TOKEN':'point-test','GSALES_WORKER_TOKEN':'sales-test'}
        evidence=self.contract.contract_evidence(self.values,self.point,self.sales,expected)
        self.assertEqual(evidence['protocols'],{
            'GPOINT':'https://gpoint.amiora.one/api/agata-worker',
            'GSALES':'https://gpoint.amiora.one/gsales/agata-worker'})
        self.assertEqual(evidence['tenants'],{'GPOINT':self.point,'GSALES':self.sales})
        self.assertEqual(evidence['lanes'],{'GPOINT':'CONFIGURED','GSALES':'CONFIGURED'})
        self.assertEqual(evidence['credentialParity'],{'GPOINT':'MATCH','GSALES':'MATCH','distinct':'YES'})
        self.assertNotIn('point-test',repr(evidence))
        self.assertNotIn('sales-test',repr(evidence))

    def test_incompatible_gateway_tenant_or_lane_credentials_block_before_apply(self):
        for change in ({'GSALES_AGATA_WORKER_URL':'https://wrong.invalid/worker'},
            {'GSALES_AGATA_TENANT_ID':self.point}, {'GSALES_AGATA_WORKER_TOKEN':'point-test'},
            {'GPOINT_AGATA_TENANT_ID':self.sales}, {'GPOINT_AGATA_WORKER_TOKEN':''}):
            with self.subTest(change=list(change)):
                with self.assertRaises(RuntimeError):
                    self.contract.verify_contract({**self.values,**change},self.point,self.sales)


if __name__ == '__main__':
    unittest.main()
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

SYNC = load('release_linear', 'publish-release-linear.py')
SELECT = load('select_release', 'select-release-run.py')

SHA = 'a' * 40
OLD = 'b' * 40
REPOSITORY = 'akhripunov/gpoint'

def deployment(identity, candidate, run_id, attempt, created_at):
    url = f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}'
    payload = {'schema': 'gpoint-deployment/v1', 'repository': REPOSITORY, 'candidateSha': candidate,
        'workflow': {'runId': str(run_id), 'attempt': str(attempt), 'url': url},
        'prepare': {'releaseId': 41, 'tag': f'gpoint-prepare-{candidate}-10-1', 'runId': '10', 'attempt': '1',
                    'assetId': 42, 'assetSha256': 'sha256:' + 'c' * 64},
        'authorization': {'mode': 'EXACT_SHA', 'id': None}}
    return {'id': identity, 'environment': 'production', 'production_environment': True,
        'transient_environment': False, 'task': 'gpoint-production-deploy', 'sha': candidate,
        'ref': candidate, 'created_at': created_at, 'payload': payload}, url

def status(state, url, created_at):
    return {'state': state, 'description': f'stage={state}', 'log_url': url,
            'environment_url': 'https://gpoint.one', 'created_at': created_at}

class DeploymentReleaseTests(unittest.TestCase):
    def test_deploy_publish_job_has_minimal_deployments_read_permission(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        publish = re.search(r'^  publish:\n(?P<job>.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)',
                            workflow, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(publish)
        permission_block = re.search(
            r'^    permissions:\n((?:      [a-z_]+: [a-z]+\n)+)', publish.group('job'), re.MULTILINE)
        self.assertIsNotNone(permission_block)
        permissions = dict(line.strip().split(': ', 1) for line in permission_block.group(1).splitlines())
        self.assertEqual(permissions, {
            'contents': 'read', 'actions': 'read', 'packages': 'read', 'deployments': 'read'})
        self.assertNotRegex(permission_block.group(1), r': write\b')
        publisher = (ROOT / 'scripts/publish-release-linear.py').read_text()
        self.assertIn("/deployments?environment=production&task=gpoint-production-deploy", publisher)
        self.assertIn("/deployments/{deployment[\"id\"]}/statuses?", publisher)

    def test_reconcile_workflow_has_minimal_private_ghcr_read_permission(self):
        workflow = (ROOT / '.github/workflows/gpoint-linear-reconcile.yml').read_text()
        self.assertIsNone(re.search(r'^permissions:', workflow, re.MULTILINE))
        job = re.search(r'^  reconcile:\n(?P<job>.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)',
                        workflow, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(job)
        match = re.search(r'^    permissions:\n((?:      [a-z_]+: [a-z]+\n)+)',
                          job.group('job'), re.MULTILINE)
        self.assertIsNotNone(match)
        permissions = dict(line.strip().split(': ', 1) for line in match.group(1).splitlines())
        self.assertEqual(permissions, {
            'contents': 'read', 'actions': 'read', 'deployments': 'read', 'packages': 'read'})
        self.assertNotRegex(match.group(1), r': write\b')
        source = (ROOT / 'scripts/publish-release-linear.py').read_text()
        self.assertIn("/deployments?environment=production&task=gpoint-production-deploy", source)
        self.assertIn("/deployments/{deployment[\"id\"]}/statuses?", source)
        self.assertIn("ghcr.io/v2/{repository}", source)
        self.assertIn("DEPLOY_EVIDENCE_REPOSITORY = 'akhripunov/gpoint-deploy-evidence'", source)
        self.assertIn("os.environ.get('GITHUB_TOKEN', '')", source)

    def test_empty_production_deployment_inventory_is_not_deployed(self):
        routes = []
        def read(route):
            routes.append(route)
            if '/deployments?' in route:
                return []
            raise AssertionError(f'unexpected GitHub read: {route}')
        with patch.object(SYNC, 'github_json', side_effect=read), \
                patch.object(SYNC, 'artifact_json') as artifact:
            state, manifest, deployment_run = SYNC.production_evidence()
        self.assertEqual(len(routes), 1)
        self.assertIsNone(state['currentProductionSha'])
        self.assertEqual(state['lastReleaseStatus'], 'NOT_DEPLOYED')
        self.assertEqual(state['serverDeployStatus'], 'NOT_DEPLOYED')
        self.assertEqual(state['workerOperationalStatus'], 'NOT_VERIFIED')
        self.assertEqual(state['overallServiceReadiness'], 'NOT_VERIFIED')
        self.assertEqual(manifest, {'status': 'NOT_DEPLOYED'})
        self.assertIsNone(deployment_run)
        artifact.assert_not_called()

    def test_successful_deployment_is_reported_from_deployment_status_and_exact_prepare_link(self):
        item, url = deployment(100, SHA, 501, 2, '2026-10-18T10:00:00Z')
        def read(route):
            if '/deployments?' in route: return [item]
            if '/statuses?' in route: return [status('success', url, '2026-10-18T10:05:00Z')]
            raise AssertionError(route)
        with patch.object(SYNC, 'github_json', side_effect=read), patch.object(SYNC, 'artifact_json', return_value={
                'status': 'COMPLETE', 'candidateSha': SHA, 'deploymentId': 100,
                'applicationVerification': 'VPS_READBACK_VERIFIED'}):
            state, manifest, run = SYNC.production_evidence()
        self.assertEqual(state['currentProductionSha'], SHA)
        self.assertEqual(state['lastReleaseStatus'], 'SUCCESS')
        self.assertEqual(manifest['prepare']['tag'], f'gpoint-prepare-{SHA}-10-1')
        self.assertEqual(run['id'], 501)
        self.assertEqual(manifest['logUrl'], url)

    def test_metadata_reconciliation_is_discovered_only_from_exact_success_status_tuple(self):
        item, url = deployment(102, SHA, 503, 1, '2026-10-19T10:00:00Z')
        status_row = status('success', url, '2026-10-19T10:05:00Z')
        status_row['description'] = f'stage=complete sha={SHA[:12]} run=503/1 reconciled=600/3'
        attestation = {'status': 'COMPLETE', 'candidateSha': SHA, 'deploymentId': 102,
                       'automationRunId': '600', 'automationRunAttempt': '3'}
        def read(route):
            if '/deployments?' in route: return [item]
            if '/statuses?' in route: return [status_row]
            raise AssertionError(route)
        with patch.object(SYNC, 'github_json', side_effect=read), \
             patch.object(SYNC, 'artifact_json') as artifact, \
             patch.object(SYNC, 'authorization_reconciliation_evidence', return_value=attestation) as reconcile:
            # The patched verifier isolates projection routing; complete-evidence
            # field validation remains exercised by the verifier's unit tests.
            reconcile.return_value.update({'applicationVerification': 'VPS_READBACK_VERIFIED'})
            state, manifest, run = SYNC.production_evidence()
        reconcile.assert_called_once_with('600', '3', SHA, 102, '503', '1')
        artifact.assert_not_called()
        self.assertEqual(state['currentProductionSha'], SHA)
        self.assertEqual(manifest['authorizationReconciliationRunId'], '600')
        self.assertEqual(manifest['authorizationReconciliationRunAttempt'], '3')
        self.assertEqual(run['id'], 503)

    def test_interrupted_apply_recovery_is_read_from_recovery_attestation_not_missing_original_oci(self):
        item, url = deployment(104, SHA, 505, 1, '2026-10-19T12:00:00Z')
        item['payload']['route'] = 'FULL_RELEASE'
        item['payload']['prepare'].update({'receiptDigest': 'sha256:' + 'd' * 64,
            'receiptSha256': 'e' * 64, 'imageDigests': {role: 'sha256:' + char * 64
                for role, char in (('app', '1'), ('agata', '2'), ('gsales', '3'))}})
        recovered = status('success', url, '2026-10-19T12:05:00Z')
        recovered['description'] = f'stage=complete sha={SHA[:12]} run=505/1 recovered=600/2'
        history = [recovered, {**status('error', url, '2026-10-19T12:01:00Z'), 'id': 700},
                   {**status('in_progress', url, '2026-10-19T12:00:30Z'), 'id': 699}]
        attestation = {'status': 'COMPLETE', 'candidateSha': SHA, 'deploymentId': 104,
                       'applicationVerification': 'VPS_READBACK_VERIFIED', 'recoveryRunId': '600',
                       'recoveryRunAttempt': '2', 'recoveryAttestationVerified': True}
        def read(route):
            if '/deployments?' in route: return [item]
            if '/statuses?' in route: return history
            raise AssertionError(route)
        with patch.object(SYNC, 'github_json', side_effect=read), \
             patch.object(SYNC, 'fetch_interrupted_recovery_evidence', return_value=attestation) as recover, \
             patch.object(SYNC, 'artifact_json') as original_artifact:
            state, manifest, run = SYNC.production_evidence()
        recover.assert_called_once_with('600', '2', SHA, 104, '505', '1', item['payload']['prepare'], item, history)
        original_artifact.assert_not_called()
        self.assertEqual(state['currentProductionSha'], SHA)
        self.assertEqual(manifest['resultStatus'], 'COMPLETE')
        self.assertEqual(manifest['recoveryRunId'], '600')
        self.assertEqual(run['id'], 505)
        self.assertEqual(run['conclusion'], 'failure')
        self.assertEqual(run['recoveryRunConclusion'], 'success')
        self.assertEqual(state['serverDeployStatus'], 'SUCCESS')
        self.assertEqual(state['workerOperationalStatus'], 'NOT_VERIFIED')
        self.assertEqual(state['overallServiceReadiness'], 'NOT_VERIFIED')
        rendered = SYNC.render_current_state({
            'synced_at': 'now', 'main_sha': SHA, 'main_checked_at': 'then',
            'production_sha': SHA, 'production_checked_at': 'then', 'production_ref': '',
            'release_status': state['lastReleaseStatus'], 'in_sync': True,
            'prepare': item['payload']['prepare'], 'candidate': 'READY', 'latest_main_prepare': None,
            'deploy': run, 'manifest': manifest, 'server_deploy_status': state['serverDeployStatus'],
            'worker_operational_status': state['workerOperationalStatus'],
            'service_readiness': state['overallServiceReadiness'],
            'dependency_checked_at': state['dependencyCheckedAt'],
        })
        self.assertIn('Original GitHub Deploy workflow: `completed` / **FAILURE**', rendered)
        self.assertIn('recovery workflow `#600` attempt `2`: **SUCCESS** for the same Deployment', rendered)
        self.assertIn('Mac worker: **NOT_VERIFIED**', rendered)
        self.assertIn('service readiness: **NOT_VERIFIED**', rendered)

    def test_recovery_reader_requires_owner_workflow_and_passes_bound_proof_to_node_verifier(self):
        prepare = {'runId': '10', 'attempt': '1', 'receiptDigest': 'sha256:' + 'd' * 64,
                   'receiptSha256': 'e' * 64,
                   'imageDigests': {role: 'sha256:' + char * 64
                       for role, char in (('app', '1'), ('agata', '2'), ('gsales', '3'))}}
        item, url = deployment(105, SHA, 506, 1, '2026-10-19T13:00:00Z')
        item['payload']['route'] = 'FULL_RELEASE'
        item['payload']['prepare'] = dict(prepare)
        item['payload']['authorization'] = {'mode': 'EXACT_SHA', 'id': ''}
        item['payload'] = json.dumps(item['payload'])
        statuses = [
            {'id': 800, 'state': 'success', 'description': f'stage=complete sha={SHA[:12]} run=506/1 recovered=601/2', 'log_url': url},
            {'id': 799, 'state': 'error', 'description': f'stage=post_apply sha={SHA[:12]} run=506/1', 'log_url': url},
            {'id': 798, 'state': 'in_progress', 'description': f'stage=pre_apply sha={SHA[:12]} run=506/1', 'log_url': url},
        ]
        proof = {'status': 'COMPLETE', 'candidateSha': SHA, 'deploymentId': 105,
                 'applicationVerification': 'VPS_READBACK_VERIFIED',
                 'originalRunId': '506', 'originalRunAttempt': '1', 'prepareRunId': '10',
                 'prepareRunAttempt': '1', 'originalFailure': {'statusId': 799},
                 'componentImages': {role: {'registryDigest': value}
                    for role, value in prepare['imageDigests'].items()}}
        owner_run = {'path': '.github/workflows/gpoint-recover-interrupted.yml', 'head_branch': 'main',
            'event': 'workflow_dispatch', 'id': 601, 'run_attempt': 2, 'status': 'completed',
            'conclusion': 'success', 'head_sha': 'f' * 40,
            'actor': {'login': 'akhripunov'}, 'triggering_actor': {'login': 'akhripunov'}}
        original_run = {'path': '.github/workflows/gpoint-deploy.yml', 'head_branch': 'main',
            'event': 'workflow_dispatch', 'id': 506, 'run_attempt': 1, 'status': 'completed',
            'conclusion': 'failure', 'head_sha': SHA}
        def read(route):
            if route.endswith('/actions/runs/601/attempts/2'): return owner_run
            if route.endswith('/actions/runs/506/attempts/1'): return original_run
            raise AssertionError(route)
        completed = subprocess.CompletedProcess(['node'], 0, stdout='{"verified":true}\n', stderr='')
        with patch.object(SYNC, 'github_json', side_effect=read), \
             patch.object(SYNC, 'artifact_json', return_value=proof) as artifact, \
             patch.object(SYNC.subprocess, 'run', return_value=completed) as bridge:
            result = SYNC.fetch_interrupted_recovery_evidence('601', '2', SHA, 105,
                '506', '1', prepare, item, statuses)
        artifact.assert_called_once_with('601', attempt='2', sha='f' * 40, deployment_id=105,
            job_name='recover', step_name='Publish independently verified recovery evidence',
            evidence_candidate_sha=SHA, expected_operation='INTERRUPTED_APPLY_INDEPENDENT_RECOVERY',
            recovery_deployment_id=105)
        bridge_input = json.loads(bridge.call_args.kwargs['input'])
        self.assertEqual(bridge_input['proof'], proof)
        self.assertEqual(bridge_input['expected']['prepareReceiptDigest'], prepare['receiptDigest'])
        self.assertTrue(result['recoveryAttestationVerified'])

    def test_recovery_reader_rejects_nonowner_before_reading_recovery_artifact(self):
        run = {'path': '.github/workflows/gpoint-recover-interrupted.yml', 'head_branch': 'main',
            'event': 'workflow_dispatch', 'id': 602, 'run_attempt': 1, 'status': 'completed',
            'conclusion': 'success', 'head_sha': 'f' * 40,
            'actor': {'login': 'other'}, 'triggering_actor': {'login': 'akhripunov'}}
        with patch.object(SYNC, 'github_json', return_value=run), \
             patch.object(SYNC, 'artifact_json') as artifact:
            with self.assertRaisesRegex(SYNC.SyncError, 'exact successful owner workflow'):
                SYNC.fetch_interrupted_recovery_evidence('602', '1', SHA, 106, '507', '1',
                    {'runId': '10', 'attempt': '1', 'receiptDigest': 'sha256:' + 'd' * 64,
                     'receiptSha256': 'e' * 64}, {}, [])
        artifact.assert_not_called()

    def test_success_status_with_failed_or_unverified_evidence_is_rejected(self):
        item, url = deployment(103, SHA, 504, 1, '2026-10-19T11:00:00Z')
        def read(route):
            if '/deployments?' in route: return [item]
            if '/statuses?' in route: return [status('success', url, '2026-10-19T11:05:00Z')]
            raise AssertionError(route)
        for result in ({'status': 'INSTALLATION_UNCONFIRMED', 'candidateSha': SHA, 'deploymentId': 103,
                        'applicationVerification': 'VPS_READBACK_VERIFIED'},
                       {'status': 'COMPLETE', 'candidateSha': SHA, 'deploymentId': 103,
                        'applicationVerification': 'NOT_VERIFIED'}):
            with self.subTest(result=result), patch.object(SYNC, 'github_json', side_effect=read), \
                 patch.object(SYNC, 'artifact_json', return_value=result):
                with self.assertRaisesRegex(SYNC.SyncError, 'not backed by complete exact-run VPS evidence'):
                    SYNC.production_evidence()

    def test_failed_attempt_makes_current_installed_sha_unknown(self):
        failed, failed_url = deployment(101, SHA, 502, 1, '2026-10-18T11:00:00Z')
        prior, prior_url = deployment(100, OLD, 501, 2, '2026-10-17T10:00:00Z')
        def read(route):
            if '/deployments?' in route: return [failed, prior]
            if '/deployments/101/statuses?' in route: return [status('error', failed_url, '2026-10-18T11:02:00Z')]
            if '/deployments/100/statuses?' in route: return [status('success', prior_url, '2026-10-17T10:05:00Z')]
            raise AssertionError(route)
        with patch.object(SYNC, 'github_json', side_effect=read), patch.object(SYNC, 'artifact_json', return_value={'status': 'FAILED'}):
            state, manifest, run = SYNC.production_evidence()
        self.assertIsNone(state['currentProductionSha'])
        self.assertEqual(state['lastReleaseStatus'], 'ERROR')
        self.assertIsNone(manifest['productionShaAfter'])
        self.assertEqual(manifest['candidateSha'], SHA)
        self.assertEqual(run['conclusion'], 'error')

    def test_invalid_environment_or_run_link_fails_closed(self):
        item, url = deployment(100, SHA, 501, 2, '2026-10-18T10:00:00Z')
        item['environment'] = 'staging'
        with patch.object(SYNC, 'github_json', return_value=[item]):
            with self.assertRaisesRegex(SYNC.SyncError, 'Deployment identity'):
                SYNC.production_evidence()
        item['environment'] = 'production'
        with patch.object(SYNC, 'github_json', side_effect=[[item], [status('success', 'https://example.com', '2026-10-18T10:05:00Z')]]):
            with self.assertRaisesRegex(SYNC.SyncError, 'exact workflow attempt'):
                SYNC.production_evidence()

    def test_latest_prepare_run_inventory_is_paginated_and_complete(self):
        runs = [{'id': 1, 'head_sha': SHA, 'status': 'completed', 'conclusion': 'success', 'created_at': '2026-10-16'}]
        calls = []
        def read(route):
            calls.append(route)
            return {'total_count': 101, 'workflow_runs': [{'id': index} for index in range(2, 102)]} if route.endswith('page=1') else {
                'total_count': 101, 'workflow_runs': runs}
        with patch.object(SYNC, 'github_json', side_effect=read):
            result = SYNC.latest_run('gpoint-prepare.yml', sha=SHA, successful=True)
        self.assertEqual(result['id'], 1)
        self.assertEqual(len(calls), 2)

    def test_latest_run_restarts_full_snapshot_after_count_drift_and_discards_old_rows(self):
        calls = []
        selected = {'id': 202, 'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
                    'created_at': '2026-10-22'}

        def read(route):
            calls.append(route)
            page = int(route.rsplit('page=', 1)[1])
            if len(calls) <= 2:
                return {'total_count': 101 if page == 1 else 102,
                        'workflow_runs': [{'id': value} for value in range(1, 101)] if page == 1 else [{'id': 101}]}
            return {'total_count': 1, 'workflow_runs': [selected]}

        with patch.object(SYNC, 'github_json', side_effect=read):
            result = SYNC.latest_run('gpoint-prepare.yml', sha=SHA, successful=True)
        self.assertEqual(result, selected)
        self.assertEqual(len(calls), 3)

    def test_latest_run_retries_duplicate_ids_even_when_count_matches(self):
        calls = []
        selected = {'id': 202, 'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
                    'created_at': '2026-10-22'}

        def read(route):
            calls.append(route)
            if len(calls) == 1:
                return {'total_count': 101, 'workflow_runs': [{'id': value} for value in range(1, 101)]}
            if len(calls) == 2:
                return {'total_count': 101, 'workflow_runs': [{'id': 100}]}
            return {'total_count': 1, 'workflow_runs': [selected]}

        with patch.object(SYNC, 'github_json', side_effect=read):
            result = SYNC.latest_run('gpoint-prepare.yml', sha=SHA, successful=True)
        self.assertEqual(result, selected)
        self.assertEqual(len(calls), 3)

    def test_latest_run_persistent_inventory_drift_fails_after_three_snapshots(self):
        calls = []

        def read(route):
            calls.append(route)
            page = int(route.rsplit('page=', 1)[1])
            return {'total_count': 101 if page == 1 else 102,
                    'workflow_runs': [{'id': value} for value in range(1, 101)] if page == 1 else [{'id': 101}]}

        with patch.object(SYNC, 'github_json', side_effect=read):
            with self.assertRaisesRegex(SYNC.SyncError, 'inventory changed'):
                SYNC.latest_run('gpoint-prepare.yml')
        self.assertEqual(len(calls), 6)

    def test_latest_run_retries_incomplete_page_snapshot_but_fails_closed_after_three(self):
        calls = []

        def read(route):
            calls.append(route)
            page = int(route.rsplit('page=', 1)[1])
            return {'total_count': 101,
                    'workflow_runs': [{'id': value} for value in range(1, 101)] if page == 1 else []}

        with patch.object(SYNC, 'github_json', side_effect=read):
            with self.assertRaisesRegex(SYNC.SyncError, 'inventory changed'):
                SYNC.latest_run('gpoint-prepare.yml')
        self.assertEqual(len(calls), 6)

    def test_latest_run_does_not_retry_invalid_schema_or_github_errors(self):
        failures = [
            {'total_count': True, 'workflow_runs': []},
            {'total_count': 1, 'workflow_runs': [{'id': True}]},
            SYNC.SyncError('GitHub current-state read failed: http_status_403'),
        ]
        for failure in failures:
            calls = []

            def read(_route):
                calls.append(1)
                if isinstance(failure, Exception):
                    raise failure
                return failure

            with self.subTest(failure=failure), patch.object(SYNC, 'github_json', side_effect=read):
                with self.assertRaises(SYNC.SyncError):
                    SYNC.latest_run('gpoint-prepare.yml')
            self.assertEqual(len(calls), 1)

    def test_selector_accepts_only_current_deploy_modes(self):
        for mode, approval in [('preflight', 'false'), ('production', 'true'), ('task-scoped-auto', 'false')]:
            SELECT.validate_request(SHA, 'refs/heads/main', mode, approval)
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'eros-acl-plan', 'false', eros_acl_approval='false')
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'eros-acl-apply', 'false', eros_acl_approval='true')
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'eros-acl-rollback', 'false', eros_acl_approval='true')
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'eros-acl-reopen', 'false', eros_acl_approval='true')
        SELECT.validate_owner_approval('eros-acl-apply', 'owner', 'owner', 'owner')
        SELECT.validate_owner_approval('eros-acl-rollback', 'owner', 'owner', 'owner')
        SELECT.validate_owner_approval('eros-acl-reopen', 'owner', 'owner', 'owner')
        with self.assertRaisesRegex(RuntimeError, 'separate exact-SHA approval'):
            SELECT.validate_request('a' * 40, 'refs/heads/main', 'eros-acl-apply', 'false', eros_acl_approval='false')
        with self.assertRaisesRegex(RuntimeError, 'repository OWNER'):
            SELECT.validate_owner_approval('eros-acl-apply', 'collaborator', 'owner', 'owner')
        with self.assertRaisesRegex(RuntimeError, 'valid release mode'):
            SELECT.validate_request('a' * 40, 'refs/heads/main', 'recovery-finalization', 'true')

if __name__ == '__main__':
    unittest.main()
