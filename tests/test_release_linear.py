import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import base64
import json
import io
import zipfile
import hashlib

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
        upload = workflow.index('scripts/release_assets.py publish prepare')
        publish = workflow.index('name: Publish candidate result to existing Linear documents')
        self.assertLess(upload, publish)
        self.assertIn('needs.prepare.result', workflow)
        self.assertIn('PREPARE_RESULT: ${{ needs.prepare.result }}', workflow)
        self.assertEqual([SYNC.candidate_status(x) for x in ('success', 'failure', 'cancelled', 'skipped')],
                         ['READY', 'FAILED', 'CANCELLED', 'SKIPPED'])

    def test_preflight_only_does_not_publish_production_and_failed_apply_has_real_status(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        publisher = workflow.split('  publish:', 1)[1]
        self.assertIn("if: always() && github.ref == 'refs/heads/main'", publisher)
        self.assertIn('needs.validate.outputs.candidate || inputs.candidate || github.sha', publisher)
        self.assertIn("inputs.mode == 'production' || inputs.mode == 'task-scoped-auto'", workflow)
        self.assertIn("needs.deploy.outputs.status != ''", workflow)
        self.assertIn('RELEASE_PHASE: preflight', workflow)
        self.assertIn("RELEASE_STATUS: ${{ needs.deploy.outputs.preflight == 'PLANNED' && 'READY_FOR_OWNER'", workflow)
        self.assertIn("needs.deploy.outputs.status == 'COMPLETE' && 'RECOVERY_REQUIRED'", workflow)
        self.assertIn('preflight=${preflightStatus}', (ROOT / 'scripts/automated-release.mjs').read_text())

    def test_release_workflow_keeps_publishable_when_telemetry_fails(self):
        workflow = (ROOT / '.github/workflows/gpoint-deploy.yml').read_text()
        telemetry = workflow.split('- name: Measure release runner usage', 1)[1].split('- name:', 1)[0]
        fallback = workflow.split('- name: Mark release telemetry unknown', 1)[1].split('- name:', 1)[0]
        self.assertIn('continue-on-error: true', telemetry)
        self.assertIn("if: always() && steps.telemetry.outcome != 'success'", fallback)
        for key in ('GITHUB_HOSTED_JOB_COUNT', 'GITHUB_HOSTED_RUNNER_SECONDS', 'SELF_HOSTED_JOB_COUNT',
                    'SELF_HOSTED_RUNNER_SECONDS', 'DUPLICATE_HOSTED_WORK_REMOVED', 'CACHE_PREVENTED_DOWNLOAD'):
            self.assertIn(f'{key}=NOT_MEASURED', fallback)
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

    def test_production_selector_requires_latest_immutable_ready_preflight(self):
        sha = 'a' * 40
        prepare = {'id': 12, 'run_attempt': 2}
        ready = {'id': 50, 'run_number': 8, 'run_attempt': 1, 'head_sha': sha,
                 'head_branch': 'main', 'event': 'workflow_dispatch',
                 'display_title': f'GPoint preflight {sha} by owner',
                 'status': 'completed', 'conclusion': 'success'}
        result = {'candidateSha': sha, 'automationRunId': '50',
                  'automationRunAttempt': '1', 'mode': 'preflight',
                  'releaseState': 'READY_FOR_OWNER', 'status': 'PLANNED',
                  'preflight': 'PASS', 'productionChanged': False,
                  'dryRun': True, 'applicationVerification': 'NOT_DEPLOYED',
                  'authorizationMode': 'EXACT_SHA', 'authorizationConsumed': False,
                  'prepareRunId': '12', 'prepareRunAttempt': '2'}
        selected = SELECT.validate_ready_preflight(sha, prepare, [ready],
                                                    lambda *_args: result)
        self.assertEqual(selected['id'], 50)
        with self.assertRaisesRegex(RuntimeError, 'successful READY_FOR_OWNER'):
            SELECT.validate_ready_preflight(sha, prepare, [], lambda *_args: result)
        failed = {**ready, 'id': 51, 'run_number': 9, 'conclusion': 'failure'}
        with self.assertRaisesRegex(RuntimeError, 'Latest exact-SHA'):
            SELECT.validate_ready_preflight(sha, prepare, [ready, failed],
                                            lambda *_args: result)
        wrong_prepare = {**result, 'prepareRunAttempt': '1'}
        with self.assertRaisesRegex(RuntimeError, 'does not prove READY_FOR_OWNER'):
            SELECT.validate_ready_preflight(sha, prepare, [ready],
                                            lambda *_args: wrong_prepare)

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

    def test_linear_release_publication_includes_me35_measured_and_unknown_fields(self):
        source = (ROOT / 'scripts/publish-release-linear.py').read_text()
        fields = source.split("telemetry_fields = (", 1)[1].split(')', 1)[0]
        for name in ('GITHUB_HOSTED_JOB_MINUTES_ESTIMATE', 'SELF_HOSTED_JOB_MINUTES_ESTIMATE',
                     'WORKFLOW_CRITICAL_PATH_SECONDS', 'WORKFLOW_QUEUE_WAIT_SECONDS',
                     'WORKFLOW_ELAPSED_SECONDS', 'OWNER_WAIT_SECONDS', 'TELEMETRY_STATUS',
                     'DUPLICATE_RUNS_REMOVED', 'DUPLICATE_JOBS_REMOVED', 'UNKNOWN_RUNNER_JOB_COUNT'):
            self.assertIn(name, fields)

    def test_prepare_telemetry_failure_does_not_block_candidate_publication(self):
        workflow = (ROOT / '.github/workflows/gpoint-prepare.yml').read_text()
        measure = workflow.split('- name: Measure Prepare runner usage', 1)[1].split('- name:', 1)[0]
        self.assertIn('id: telemetry', measure)
        self.assertIn('continue-on-error: true', measure)
        fallback = workflow.split('- name: Mark Prepare telemetry unknown', 1)[1].split('- name:', 1)[0]
        self.assertIn("if: always() && steps.telemetry.outcome != 'success'", fallback)
        for name in ('GITHUB_HOSTED_JOB_MINUTES_ESTIMATE', 'WORKFLOW_CRITICAL_PATH_SECONDS',
                     'WORKFLOW_QUEUE_WAIT_SECONDS', 'WORKFLOW_ELAPSED_SECONDS', 'TELEMETRY_STATUS'):
            self.assertIn(f'{name}=NOT_MEASURED', fallback)
        publish = workflow.split('- name: Publish candidate result to existing Linear documents', 1)[1].split('- name:', 1)[0]
        self.assertIn('run: python scripts/publish-release-linear.py', publish)

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
        runs = {'workflow_runs': [
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

    def test_production_self_run_in_progress_requires_completed_successful_deploy_job(self):
        github, run, result = self.production_fixture()
        with patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'artifact_json', return_value=result):
            self.assertEqual(SYNC.production_evidence()[2], run)
        self.assertEqual(run['status'], 'in_progress')
        self.assertIsNone(run['conclusion'])
        github, _, result = self.production_fixture(deploy_conclusion='failure')
        with patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'artifact_json', return_value=result):
            with self.assertRaises(SYNC.SyncError):
                SYNC.production_evidence()

    def test_complete_runtime_reconciles_after_publication_failure_without_faking_workflow_success(self):
        github, run, result = self.production_fixture('completed', 'failure')
        with patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'artifact_json', return_value=result):
            self.assertEqual(SYNC.production_evidence()[2]['conclusion'], 'failure')
        github, _, result = self.production_fixture('completed', 'success')
        with patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'artifact_json', return_value=result):
            self.assertEqual(SYNC.production_evidence()[2]['conclusion'], 'success')

    def test_reconciled_ledger_can_sync_without_rewriting_failed_historical_deploy(self):
        github, run, _result = self.production_fixture('completed', 'failure')
        original = github
        def reconciled(path, token=None):
            data = original(path, token)
            if 'production.json?' in path:
                state = json.loads(base64.b64decode(data['content']))
                state['publicationRepair'] = {'runId': 55}
                state['releaseOrigin'] = 'reconciled-production-release'
                return {'content': base64.b64encode(json.dumps(state).encode()).decode()}
            if '/manifests/' in path:
                manifest = json.loads(base64.b64decode(data['content']))
                manifest.update({
                    'eventType': 'RECONCILED_PRODUCTION_RELEASE', 'automationRunId': 55,
                    'automationRunConclusion': 'failure', 'serverDeployStatus': 'COMPLETE',
                    'originalFailure': {'rewritten': False},
                    'publicationRepair': {'completedAt': '2026-10-05T04:36:09Z', 'basis': 'fresh read-only VPS reconciliation'},
                    'dbHeadAfter': 'prisma-head', 'agataDbHeadAfter': 'agata0011',
                    'gsalesDbHeadAfter': '001_gsales',
                    'serverReconciliation': {'vpsSha': 'a' * 40, 'prismaHead': 'prisma-head',
                        'agataHead': 'agata0011', 'gsalesHead': '001_gsales',
                        'gsalesRlsSecurity': 'PASS', 'externalMiniappSha': 'a' * 40,
                        'externalApiHealth': 'PASS'},
                    'smoke': {'vpsHealth': 'PASS', 'imageRuntimeParity': 'PASS',
                        'gsalesRuntimeReconciliation': 'PASS'},
                    'runtimeFacts': {'LIVE_RELEASE_ENV_SHA': 'a' * 40,
                        'LIVE_AGATA_API_SHA': 'a' * 40, 'LIVE_GSALES_API_SHA': 'a' * 40},
                })
                return {'content': base64.b64encode(json.dumps(manifest).encode()).decode()}
            return data
        with (patch.object(SYNC, 'github_json', side_effect=reconciled),
              patch.object(SYNC, 'artifact_json', side_effect=AssertionError('reconciled manifest is the publication evidence'))):
            state, manifest, deployment = SYNC.production_evidence()
        self.assertEqual(state['publicationRepair']['runId'], 55)
        self.assertEqual(manifest['eventType'], 'RECONCILED_PRODUCTION_RELEASE')
        self.assertEqual(deployment['conclusion'], 'failure')
        rendered = SYNC.render_current_state({
            'synced_at': 'now', 'main_sha': 'b' * 40, 'main_checked_at': 'then',
            'production_sha': 'a' * 40, 'production_checked_at': 'then',
            'production_ref': 'prod-new', 'release_status': 'COMPLETE', 'in_sync': False,
            'prepare': {}, 'candidate': 'NOT_PREPARED_FOR_CURRENT_MAIN',
            'latest_main_prepare': {}, 'deploy': deployment, 'manifest': manifest,
            'server_deploy_status': 'COMPLETE', 'worker_operational_status': 'NOT_VERIFIED',
            'service_readiness': 'DEGRADED', 'dependency_checked_at': 'not checked',
        })
        self.assertIn('conclusion **failure** сохранён как есть', rendered)
        self.assertNotIn('deploy job **SUCCESS**', rendered)

    def test_reconciled_ledger_requires_matching_server_and_historical_run_evidence(self):
        github, _run, _result = self.production_fixture('completed', 'failure')
        original = github
        def mismatch(path, token=None):
            data = original(path, token)
            if '/manifests/' in path:
                manifest = json.loads(base64.b64decode(data['content']))
                manifest.update({'eventType': 'RECONCILED_PRODUCTION_RELEASE', 'automationRunId': 55,
                    'automationRunConclusion': 'failure', 'serverDeployStatus': 'COMPLETE',
                    'serverReconciliation': {'vpsSha': 'b' * 40}})
                return {'content': base64.b64encode(json.dumps(manifest).encode()).decode()}
            return data
        with patch.object(SYNC, 'github_json', side_effect=mismatch), patch.object(SYNC, 'artifact_json', return_value={}):
            with self.assertRaises(SYNC.SyncError):
                SYNC.production_evidence()

    def test_production_artifact_cannot_replace_newer_ledger_release_or_use_other_run(self):
        for field, value in [('releaseId', 'release-old'), ('automationRunId', '54'),
                             ('productionShaAfter', 'b' * 40), ('productionRef', 'prod-old'),
                             ('status', 'RECOVERY_REQUIRED')]:
            with self.subTest(field=field):
                github, _, result = self.production_fixture(result_overrides={field: value})
                with patch.object(SYNC, 'github_json', side_effect=github), patch.object(SYNC, 'artifact_json', return_value=result):
                    with self.assertRaises(SYNC.SyncError):
                        SYNC.production_evidence()

    def test_production_ledger_manifest_sha_mismatch_is_not_reported_complete(self):
        github, _, result = self.production_fixture('completed', 'failure')
        def mismatch(path, token=None):
            data = github(path, token)
            if '/manifests/' in path:
                manifest = json.loads(base64.b64decode(data['content']))
                manifest['productionShaAfter'] = 'b' * 40
                return {'content': base64.b64encode(json.dumps(manifest).encode()).decode()}
            return data
        with patch.object(SYNC, 'github_json', side_effect=mismatch), patch.object(SYNC, 'artifact_json', return_value=result):
            with self.assertRaisesRegex(SYNC.SyncError, 'STATE_MISMATCH'):
                SYNC.production_evidence()

    def test_immutable_production_result_zip_checksum_is_required(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('result.json', json.dumps({'status': 'COMPLETE'}))
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        for expected in [digest, '0' * 64, '']:
            with self.subTest(expected=expected):
                metadata = {'artifacts': [{'name': 'gpoint-production-result-55-1',
                    'expired': False, 'digest': 'sha256:' + expected,
                    'archive_download_url': 'https://api.github.com/artifact'}]}
                class Opener:
                    def open(self, request, timeout=None):
                        if request.get_header('Accept') != 'application/vnd.github+json':
                            raise AssertionError('Legacy Actions ZIP requires its original media type')
                        return io.BytesIO(payload)
                with patch.object(SYNC, 'github_json', return_value=metadata), patch.object(SYNC.urllib.request, 'build_opener', return_value=Opener()):
                    if expected == digest:
                        self.assertEqual(SYNC.artifact_json(55, 'gpoint-production-result-')['status'], 'COMPLETE')
                    else:
                        with self.assertRaisesRegex(SYNC.SyncError, 'checksum mismatch'):
                            SYNC.artifact_json(55, 'gpoint-production-result-')

    def test_new_immutable_release_result_is_checksum_verified_and_mutable_is_blocked(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('result.json', json.dumps({'status': 'PLANNED', 'candidateSha': 'a'*40,
                'automationRunId': '55', 'automationRunAttempt': '2'}))
        payload = buffer.getvalue()
        sha = 'a' * 40
        tag = f'gpoint-evidence-{sha}-55-2'
        for immutable, digest in [(True, hashlib.sha256(payload).hexdigest()), (False, '0'*64), (True, '0'*64)]:
            def github(path, token=None):
                if path.endswith('/artifacts'): return {'artifacts': []}
                if path.endswith('/55'): return {'head_sha': sha, 'head_branch': 'main', 'event': 'workflow_dispatch',
                                                 'path': '.github/workflows/gpoint-deploy.yml', 'run_attempt': 2}
                if '/actions/runs/55/attempts/2/jobs' in path:
                    return {'total_count': 1, 'jobs': [{'name': 'deploy', 'status': 'completed',
                                                        'conclusion': 'success', 'head_sha': sha}]}
                if '/git/ref/' in path: return {'object': {'type': 'commit', 'sha': sha}}
                return {'tag_name': tag, 'draft': False, 'immutable': immutable, 'assets': [
                    {'id': 99, 'name': 'result.zip', 'state': 'uploaded', 'size': len(payload), 'digest': 'sha256:'+digest}]}
            class Opener:
                def open(self, request, timeout=None):
                    if request.get_header('Accept') != 'application/octet-stream':
                        raise AssertionError('Release assets require binary media type')
                    return io.BytesIO(payload)
            with self.subTest(immutable=immutable, digest=digest), patch.object(SYNC, 'github_json', side_effect=github), \
                 patch.object(SYNC.urllib.request, 'build_opener', return_value=Opener()):
                if immutable and digest == hashlib.sha256(payload).hexdigest():
                    self.assertEqual(SYNC.artifact_json(55, 'gpoint-production-result-')['status'], 'PLANNED')
                else:
                    with self.assertRaises(SYNC.SyncError): SYNC.artifact_json(55, 'gpoint-production-result-')

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

        self.assertIn("if (mode === 'preflight') validateWorkerMigrationReadiness(candidate, evidence)",source)
        self.assertIn('workerMigrationReadiness: workerCompatibility.migrationReadiness',source)

    def test_compatibility_is_independent_of_live_worker_health(self):
        self.assertEqual(self.contract.verify_contract(self.values,self.point,self.sales), {'compatibility':'PASS'})

    def test_read_only_worker_migration_plan_is_candidate_bound_and_keeps_apply_jit(self):
        sha='a'*40
        assets={'gpoint_mac_worker.py':'b'*64,'lab_queue.py':'c'*64}
        exact=self.contract.build_migration_readiness(sha,assets,'VERIFIED','VERIFIED_CURRENT',True,candidate_differs=False)
        self.assertEqual(exact['status'],'NO_TRANSITION_REQUIRED')
        self.assertFalse(exact['mutationPerformed'])
        self.assertEqual(exact['readinessBasis'],'STATIC_PLAN_ONLY')
        changed=self.contract.build_migration_readiness(sha,assets,'VERIFIED','VERIFIED_LEGACY',True)
        self.assertEqual(changed['status'],'TRANSITION_REQUIRED_PLAN_VERIFIED')
        self.assertEqual(changed['rollbackSnapshotCapture'],'REQUIRED_AT_APPLY')
        self.assertEqual(changed['startupValidation'],'REQUIRED_DURING_APPLY')
        self.assertIn('QUEUE_REVALIDATION_BEFORE_SWITCH',changed['remainingJitGates'])
        self.assertIn('FRESH_GPOINT_QUEUE',changed['remainingJitGates'])
        configured=self.contract.build_migration_readiness(
            sha,assets,'VERIFIED','VERIFIED_CONFIG',True)
        self.assertEqual(configured['tenantBindingSource'],'VERIFIED_CONFIG')
        for kwargs, reason in [
            ({'installed_source_provenance':'MISSING'},'provenance'),
            ({'tenant_binding_source':'UNVERIFIED'},'tenant_binding'),
            ({'tenant_binding_source':'STALE'},'tenant_binding'),
            ({'legacy_runtime_present':False},'rollback_source'),
        ]:
            options={'installed_source_provenance':'VERIFIED','tenant_binding_source':'VERIFIED_CURRENT','legacy_runtime_present':True}
            options.update(kwargs)
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError,reason):
                self.contract.build_migration_readiness(sha,assets,**options)
        with self.assertRaisesRegex(RuntimeError,'assets_invalid'):
            self.contract.build_migration_readiness(sha,{**assets,'bad':'not-a-hash'},'VERIFIED','VERIFIED_CURRENT',True)

    def test_read_only_worker_plan_rejects_runtime_current_symlink_outside_root_with_configured_tenant(self):
        import hashlib
        import tempfile
        from unittest.mock import patch

        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / 'repo'
            support = base / 'support'
            runtime_root = support / 'runtime'
            runtime_root.mkdir(parents=True, mode=0o700)
            external_runtime = base / 'external' / sha
            external_runtime.mkdir(parents=True)

            worker_source = repo / 'services/agata/worker'
            worker_source.mkdir(parents=True)
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'candidate_worker_migration_test',
                ROOT / 'services/agata/worker/migrate_worker_runtime.py')
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)

            for name in migration.ASSETS:
                source_asset = worker_source / name
                installed_asset = external_runtime / name
                source_asset.write_text('candidate-' + name)
                installed_asset.write_text('installed-' + name)
                installed_asset.chmod(0o700 if name == 'run_codex_worker.sh' else 0o600)
            external_runtime.chmod(0o700)
            assets = {name: hashlib.sha256((external_runtime / name).read_bytes()).hexdigest()
                      for name in migration.ASSETS}
            bundle_sha = hashlib.sha256(json.dumps(
                assets, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            manifest = {'source_sha': sha, 'bundle_sha256': bundle_sha, 'assets': assets}
            manifest_path = external_runtime / 'bundle-manifest.json'
            manifest_path.write_text(json.dumps(manifest))
            manifest_path.chmod(0o600)

            (support / 'legacy').mkdir()
            (support / 'legacy/run_codex_worker.sh').write_text('#!/bin/sh\n')
            (runtime_root / 'current').symlink_to(external_runtime, target_is_directory=True)

            no_op_spec = type('Spec', (), {'loader': type('Loader', (), {
                'exec_module': staticmethod(lambda _module: None)})()})()
            with patch.object(self.contract.lane, 'REPO', repo), \
                 patch.object(self.contract.lane, 'SUPPORT', support), \
                 patch.object(migration, 'ROOT', support), \
                 patch.object(migration, 'LEGACY', support / 'legacy'), \
                 patch.object(migration, 'WORKER_DIR', worker_source), \
                 patch.object(migration, '_validated_current_tenant_binding', return_value=None), \
                 patch.object(migration, '_validated_legacy_tenant_binding', return_value=None), \
                 patch.object(importlib.util, 'module_from_spec', return_value=migration), \
                 patch.object(importlib.util, 'spec_from_file_location', return_value=no_op_spec):
                with self.assertRaisesRegex(RuntimeError, 'worker_migration_runtime_invalid'):
                    self.contract.inspect_migration_readiness(sha, {}, self.point)

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
