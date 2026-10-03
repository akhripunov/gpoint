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
        upload = workflow.index('uses: actions/upload-artifact@v4')
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
        self.assertIn("inputs.mode == 'production' && needs.deploy.outputs.status != ''", workflow)
        self.assertIn('RELEASE_PHASE: preflight', workflow)
        self.assertIn("RELEASE_STATUS: ${{ needs.deploy.outputs.preflight || 'BLOCKED' }}", workflow)
        self.assertIn("needs.deploy.outputs.status == 'COMPLETE' && 'RECOVERY_REQUIRED'", workflow)
        self.assertIn('preflight=${preflightStatus}', (ROOT / 'scripts/automated-release.mjs').read_text())

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
                        return io.BytesIO(payload)
                with patch.object(SYNC, 'github_json', return_value=metadata), patch.object(SYNC.urllib.request, 'build_opener', return_value=Opener()):
                    if expected == digest:
                        self.assertEqual(SYNC.artifact_json(55, 'gpoint-production-result-')['status'], 'COMPLETE')
                    else:
                        with self.assertRaisesRegex(SYNC.SyncError, 'checksum mismatch'):
                            SYNC.artifact_json(55, 'gpoint-production-result-')

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
               'status': 'completed', 'conclusion': 'success'}
        self.assertEqual(SELECT.select_run('a' * 40, [run]), run)

    def test_preflight_can_run_without_production_permission(self):
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'preflight', 'false')
        with self.assertRaisesRegex(RuntimeError, 'approval'):
            SELECT.validate_request('a' * 40, 'refs/heads/main', 'production', 'false')
        SELECT.validate_request('a' * 40, 'refs/heads/main', 'production', 'true')

    def test_branch_and_invalid_release_mode_are_rejected(self):
        for ref, mode in [('refs/heads/other', 'production'), ('refs/heads/main', 'anything')]:
            with self.assertRaises(RuntimeError):
                SELECT.validate_request('a' * 40, ref, mode, 'true')


if __name__ == '__main__':
    unittest.main()
