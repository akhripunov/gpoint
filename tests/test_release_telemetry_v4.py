import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'release_telemetry_v4', ROOT / 'scripts' / 'release-telemetry.py'
)
TELEMETRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TELEMETRY)


def job(job_id, start, end, *, created='2026-10-15T10:00:00Z', labels=None,
        name=None, conclusion='success', needs=None, steps=None):
    result = {
        'id': job_id,
        'name': name or f'job-{job_id}',
        'status': 'completed' if end else 'in_progress',
        'conclusion': conclusion,
        'labels': labels or ['ubuntu-latest'],
        'runner_name': 'GitHub Actions 1',
        'created_at': created,
        'started_at': start,
        'completed_at': end,
        'steps': steps or [],
    }
    if needs is not None:
        result['needs'] = needs
    return result


def run(run_id, attempt, jobs, *, created='2026-10-15T10:00:00Z',
        started='2026-10-15T10:00:00Z', completed='2026-10-15T10:02:00Z',
        dependencies=None, stages=None):
    value = {
        'run': {
            'id': run_id,
            'run_attempt': attempt,
            'created_at': created,
            'run_started_at': started,
            'updated_at': completed,
        'status': 'completed' if completed else 'in_progress',
            'conclusion': 'success' if completed else None,
            'workflowName': 'GPoint — Prepare',
        },
        'jobs': jobs,
    }
    if dependencies is not None:
        value['job_dependencies'] = dependencies
    if stages is not None:
        value['prepareStages'] = stages
    return value


class ReleaseTelemetryV4Tests(unittest.TestCase):
    def test_github_collection_fetches_each_exact_attempt_and_all_job_pages(self):
        base = 'https://api.github.com/repos/akhripunov/gpoint/actions/runs/10'
        payloads = {
            base: {'id': 10, 'run_attempt': 2, 'created_at': '2026-10-15T10:00:00Z',
                   'status': 'completed', 'conclusion': 'success'},
            base + '/attempts/1': {'run_attempt': 1, 'run_started_at': '2026-10-15T10:00:05Z',
                                   'updated_at': '2026-10-15T10:00:45Z', 'status': 'completed',
                                   'conclusion': 'failure'},
            base + '/attempts/2': {'run_attempt': 2, 'run_started_at': '2026-10-15T10:01:00Z',
                                   'updated_at': '2026-10-15T10:02:00Z', 'status': 'completed',
                                   'conclusion': 'success'},
            base + '/attempts/1/jobs?per_page=100&page=1': {'total_count': 1, 'jobs': [
                {'id': 3, 'name': 'first-attempt', 'status': 'completed', 'conclusion': 'failure',
                 'labels': ['ubuntu-latest'], 'runner_name': 'GitHub Actions 0',
                 'created_at': '2026-10-15T10:00:05Z', 'started_at': '2026-10-15T10:00:05Z',
                 'completed_at': '2026-10-15T10:00:45Z'}]},
            base + '/attempts/2/jobs?per_page=100&page=1': {'total_count': 2, 'jobs': [
                {'id': 1, 'name': 'retry-a', 'status': 'completed', 'conclusion': 'success',
                 'labels': ['ubuntu-latest'], 'runner_name': 'GitHub Actions 1',
                 'created_at': '2026-10-15T10:01:00Z', 'started_at': '2026-10-15T10:01:00Z',
                 'completed_at': '2026-10-15T10:01:30Z'}]},
            base + '/attempts/2/jobs?per_page=100&page=2': {'total_count': 2, 'jobs': [
                {'id': 2, 'name': 'retry-b', 'status': 'completed', 'conclusion': 'success',
                 'labels': ['ubuntu-latest'], 'runner_name': 'GitHub Actions 2',
                 'created_at': '2026-10-15T10:01:30Z', 'started_at': '2026-10-15T10:01:30Z',
                 'completed_at': '2026-10-15T10:02:00Z'}]},
        }

        class Response:
            def __init__(self, body):
                self.body = json.dumps(body).encode()
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return self.body

        calls = []
        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            return Response(payloads[request.full_url])

        with patch.object(TELEMETRY.urllib.request, 'urlopen', side_effect=fake_urlopen):
            records = TELEMETRY._fetch_records('akhripunov/gpoint', ['10'], 'secret-token')
        self.assertEqual([record['run']['run_attempt'] for record in records], [1, 2])
        self.assertEqual(len(records[1]['jobs']), 2)
        self.assertIn(base + '/attempts/1/jobs?per_page=100&page=1', calls)
        self.assertIn(base + '/attempts/2/jobs?per_page=100&page=1', calls)

    def test_missing_run_timestamps_are_not_measured(self):
        record = run(10, 1, [job(1, '2026-10-15T10:00:10Z', '2026-10-15T10:01:10Z')])
        record['run'].pop('run_started_at')
        record['run'].pop('updated_at')
        report = TELEMETRY.summarize_runs([record])
        self.assertEqual(report['runs'][0]['workflow_queue_wait_seconds'], 'NOT_MEASURED')
        self.assertEqual(report['runs'][0]['workflow_elapsed_seconds'], 'NOT_MEASURED')
        self.assertEqual(report['critical_path_elapsed_seconds'], 'NOT_MEASURED')
        self.assertEqual(report['job_dependency_critical_path_seconds'], 'NOT_MEASURED')

    def test_queue_wait_and_job_queue_wait_are_separate_from_runner_time(self):
        item = run(10, 1, [job(1, '2026-10-15T10:00:20Z', '2026-10-15T10:01:20Z')],
                   started='2026-10-15T10:00:05Z')
        report = TELEMETRY.summarize_runs([item])
        self.assertEqual(report['runs'][0]['workflow_queue_wait_seconds'], 5.0)
        self.assertEqual(report['jobs'][0]['queue_wait_seconds'], 20.0)
        self.assertEqual(report['jobs'][0]['runner_seconds'], 60.0)

    def test_retry_attempts_are_distinct_but_duplicate_job_records_are_counted_once(self):
        repeated_job = job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:00:30Z')
        first = run(10, 1, [repeated_job, dict(repeated_job)])
        retry = run(10, 2, [job(1, '2026-10-15T10:01:00Z', '2026-10-15T10:02:01Z')],
                    created='2026-10-15T10:00:50Z', started='2026-10-15T10:00:55Z',
                    completed='2026-10-15T10:02:05Z')
        report = TELEMETRY.summarize_runs([first, first, retry])
        self.assertEqual(report['duplicate_runs_removed'], 1)
        self.assertEqual(report['duplicate_jobs_removed'], 1)
        self.assertEqual(len(report['runs']), 2)
        self.assertEqual(report['totals']['GITHUB_HOSTED_JOB_COUNT'], 2)
        self.assertEqual(report['totals']['GITHUB_HOSTED_RUNNER_SECONDS'], 91.0)
        self.assertEqual(report['totals']['GITHUB_HOSTED_JOB_MINUTES_ESTIMATE'], 3)

    def test_successful_job_carried_into_retry_is_not_counted_as_new_runner_work(self):
        completed_job = job(20, '2026-10-15T10:00:10Z', '2026-10-15T10:00:40Z')
        first = run(10, 1, [completed_job], completed='2026-10-15T10:00:45Z')
        carried = job(21, '2026-10-15T10:00:10Z', '2026-10-15T10:00:40Z')
        retry = run(10, 2, [carried], created='2026-10-15T10:00:55Z',
                    started='2026-10-15T10:01:00Z', completed='2026-10-15T10:01:05Z')
        report = TELEMETRY.summarize_runs([first, retry])
        self.assertTrue(report['jobs'][1]['carried_over'])
        self.assertEqual(report['totals']['GITHUB_HOSTED_JOB_COUNT'], 1)
        self.assertEqual(report['totals']['GITHUB_HOSTED_RUNNER_SECONDS'], 30.0)

    def test_conflicting_duplicate_job_makes_aggregate_not_measured(self):
        item = run(10, 1, [
            job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:00:30Z'),
            job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:00:40Z'),
        ])
        report = TELEMETRY.summarize_runs([item])
        self.assertEqual(report['duplicate_job_conflicts'], 1)
        self.assertEqual(report['totals']['GITHUB_HOSTED_RUNNER_SECONDS'], 'NOT_MEASURED')

    def test_parallel_jobs_use_observed_dag_critical_path_not_sum(self):
        item = run(10, 1, [
            job(1, '2026-10-15T10:00:05Z', '2026-10-15T10:01:05Z', name='slow'),
            job(2, '2026-10-15T10:00:05Z', '2026-10-15T10:00:35Z', name='parallel'),
            job(3, '2026-10-15T10:01:05Z', '2026-10-15T10:01:35Z', name='publish', needs=['slow', 'parallel']),
        ], completed='2026-10-15T10:01:40Z',
            dependencies={'slow': [], 'parallel': [], 'publish': ['slow', 'parallel']})
        report = TELEMETRY.summarize_runs([item])
        self.assertEqual(report['critical_path_elapsed_seconds'], 100.0)
        # The DAG path includes the five seconds between attempt start and the
        # first root job, plus the dependent job intervals and their wait gap.
        self.assertEqual(report['job_dependency_critical_path_seconds'], 95.0)
        self.assertNotEqual(report['critical_path_elapsed_seconds'], sum(j['runner_seconds'] for j in report['jobs']))

    def test_missing_dependency_or_job_timestamps_do_not_invent_critical_path(self):
        item = run(10, 1, [job(1, '2026-10-15T10:00:05Z', None)], dependencies={'job-1': []})
        report = TELEMETRY.summarize_runs([item])
        self.assertEqual(report['critical_path_elapsed_seconds'], 120.0)
        self.assertEqual(report['job_dependency_critical_path_seconds'], 'NOT_MEASURED')
        self.assertEqual(report['totals']['GITHUB_HOSTED_RUNNER_SECONDS'], 'NOT_MEASURED')

    def test_each_job_uses_ceil_minute_estimate_and_runner_classes_are_separate(self):
        item = run(10, 1, [
            job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:01:00Z'),
            job(2, '2026-10-15T10:00:00Z', '2026-10-15T10:01:00.001Z',
                labels=['self-hosted', 'macOS', 'gpoint-production'], name='worker'),
        ])
        report = TELEMETRY.summarize_runs([item])
        totals = report['totals']
        self.assertEqual(totals['GITHUB_HOSTED_JOB_MINUTES_ESTIMATE'], 1)
        self.assertEqual(totals['SELF_HOSTED_JOB_MINUTES_ESTIMATE'], 2)
        self.assertEqual(totals['GITHUB_HOSTED_RUNNER_SECONDS'], 60.0)
        self.assertEqual(totals['SELF_HOSTED_RUNNER_SECONDS'], 60.001)
        self.assertIn('estimate, not billing', report['minute_estimate_note'].lower())

    def test_stage_overlap_is_reported_per_stage_and_never_summed(self):
        item = run(10, 1, [job(1, '2026-10-15T10:00:05Z', '2026-10-15T10:01:35Z')],
                   dependencies={'job-1': []}, stages={
                       'docker': {'status': 'PASS', 'seconds': 70},
                       'postgres': {'status': 'PASS', 'seconds': 85},
                   })
        report = TELEMETRY.summarize_runs([item])
        self.assertEqual(report['runs'][0]['stage_timings']['docker'], 70.0)
        self.assertEqual(report['runs'][0]['stage_timings']['postgres'], 85.0)
        self.assertEqual(report['stage_critical_path_seconds'], 'NOT_MEASURED')
        self.assertNotIn('stage_seconds_sum', report)

    def test_secret_free_report_uses_allowlisted_fields_only(self):
        item = run(10, 1, [job(1, '2026-10-15T10:00:05Z', '2026-10-15T10:01:05Z')])
        item['run']['token'] = 'super-secret-token'
        item['jobs'][0]['private_payload'] = 'private user content'
        rendered = TELEMETRY.render_report(TELEMETRY.summarize_runs([item]))
        self.assertNotIn('super-secret-token', rendered)
        self.assertNotIn('private user content', rendered)
        self.assertIn('NOT_MEASURED', rendered)

    def test_unavailable_collection_and_unverified_cache_flag_stay_not_measured(self):
        report = TELEMETRY.summarize_runs([])
        report['status'] = 'NOT_MEASURED'
        for name in report['totals']:
            report['totals'][name] = 'NOT_MEASURED'
        values = TELEMETRY._flat_outputs(report, 'token-or-unexpected-value')
        self.assertEqual(values['TELEMETRY_STATUS'], 'NOT_MEASURED')
        self.assertEqual(values['OWNER_WAIT_SECONDS'], 'NOT_MEASURED')
        self.assertEqual(values['CACHE_PREVENTED_DOWNLOAD'], 'NOT_MEASURED')
        self.assertEqual(values['WORKFLOW_CRITICAL_PATH_SECONDS'], 'NOT_MEASURED')

    def test_conflicting_run_duplicate_is_not_selected_as_authoritative(self):
        first = run(10, 1, [job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:00:30Z')])
        conflicting = run(10, 1, [job(1, '2026-10-15T10:00:00Z', '2026-10-15T10:00:45Z')])
        report = TELEMETRY.summarize_runs([first, conflicting])
        self.assertEqual(report['duplicate_run_conflicts'], 1)
        self.assertEqual(report['runs'], [])
        self.assertEqual(report['totals']['GITHUB_HOSTED_RUNNER_SECONDS'], 'NOT_MEASURED')


if __name__ == '__main__':
    unittest.main()
