#!/usr/bin/env python3
"""Summarize saved GitHub Actions run/job JSON without dispatching workflows."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
import urllib.request


NOT_MEASURED = 'NOT_MEASURED'
UTC = dt.timezone.utc


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value):
    parsed = _timestamp(value)
    return parsed.isoformat().replace('+00:00', 'Z') if parsed else NOT_MEASURED


def _seconds(start, end):
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds(), 3)


def _host_class(job):
    labels = job.get('labels')
    labels = [str(label).lower() for label in labels] if isinstance(labels, list) else []
    if 'self-hosted' in labels:
        return 'SELF_HOSTED'
    if labels or job.get('runner_name'):
        return 'GITHUB_HOSTED'
    return None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _job_key(run_id, attempt, job):
    job_id = job.get('id')
    return (str(run_id), str(attempt), str(job_id)) if job_id is not None else None


def _job_projection(job, run_id, attempt, attempt_started=None, observed_at=None):
    """Return only safe, measured job fields; never copy arbitrary API payloads."""
    created = _timestamp(job.get('created_at'))
    started = _timestamp(job.get('started_at'))
    completed = _timestamp(job.get('completed_at'))
    observed = _timestamp(observed_at)
    skipped = job.get('conclusion') == 'skipped'
    host = _host_class(job)
    runner_seconds = _seconds(started, completed) if not skipped else 0.0
    queue_wait = _seconds(created, started) if not skipped else 0.0
    carried_over = bool(attempt_started and started and completed
                        and started < attempt_started and completed <= attempt_started)
    attempt_timing_ambiguous = bool(
        not skipped and attempt_started and started and started < attempt_started
        and completed and completed > attempt_started
    )
    if carried_over:
        # GitHub's attempt-specific jobs endpoint can return an earlier
        # successful job copied into a retry under a new job ID. It consumed no
        # runner time in this attempt.
        runner_seconds = 0.0
        queue_wait = 0.0
    elif attempt_timing_ambiguous or (int(attempt) > 1 and attempt_started is None and not skipped):
        runner_seconds = queue_wait = None
    elapsed_lower_bound = (
        _seconds(started, observed)
        if job.get('status') == 'in_progress' and started and not completed and observed and observed >= started
        else None
    )
    steps = []
    for step in job.get('steps') or []:
        if not isinstance(step, dict):
            continue
        step_seconds = _seconds(_timestamp(step.get('started_at')),
                                _timestamp(step.get('completed_at')))
        steps.append({
            'name': str(step.get('name') or 'UNKNOWN'),
            'conclusion': str(step.get('conclusion') or 'UNKNOWN'),
            'seconds': step_seconds if step_seconds is not None else NOT_MEASURED,
        })
    return {
        'run_id': str(run_id),
        'attempt': str(attempt),
        'job_id': str(job.get('id')) if job.get('id') is not None else NOT_MEASURED,
        'name': str(job.get('name') or 'UNKNOWN'),
        'status': str(job.get('status') or 'UNKNOWN'),
        'conclusion': str(job.get('conclusion') or 'UNKNOWN'),
        'runner_class': host or 'UNKNOWN',
        'carried_over': carried_over,
        'created_at': _iso(job.get('created_at')),
        'started_at': _iso(job.get('started_at')),
        'completed_at': _iso(job.get('completed_at')),
        'queue_wait_seconds': queue_wait if queue_wait is not None else NOT_MEASURED,
        'runner_seconds': runner_seconds if runner_seconds is not None else NOT_MEASURED,
        'elapsed_seconds_lower_bound_at_collection': (
            elapsed_lower_bound if elapsed_lower_bound is not None else NOT_MEASURED
        ),
        'elapsed_lower_bound_observed_at': _iso(observed_at),
        'job_minutes_estimate': math.ceil(runner_seconds / 60)
        if runner_seconds is not None and runner_seconds > 0 and host else
        (0 if runner_seconds == 0 and host else NOT_MEASURED),
        'steps': steps,
        'needs': job.get('needs') if isinstance(job.get('needs'), list) else None,
    }


def _sum_measured(items, key):
    values = [item[key] for item in items]
    if any(value == NOT_MEASURED for value in values):
        return NOT_MEASURED
    return round(sum(values), 3)


def _dependency_path(jobs, dependencies, path_start=None):
    """Compute a longest observed DAG path, including gaps between dependent jobs."""
    if not isinstance(dependencies, dict) or not jobs or path_start is None:
        return NOT_MEASURED
    by_name = {job['name']: job for job in jobs}
    if len(by_name) != len(jobs) or set(dependencies) != set(by_name):
        return NOT_MEASURED
    intervals = {}
    for name, job in by_name.items():
        # These timestamps are retained privately in the source record, so the
        # caller supplies them through the temporary normalized fields below.
        start, end = job.get('_start'), job.get('_end')
        if start is None or end is None or start < path_start:
            return NOT_MEASURED
        needs = dependencies.get(name)
        if not isinstance(needs, list) or any(parent not in by_name for parent in needs):
            return NOT_MEASURED
        intervals[name] = (start, end, needs)

    memo = {}
    visiting = set()

    def path(name):
        if name in memo:
            return memo[name]
        if name in visiting:
            raise ValueError('cycle')
        visiting.add(name)
        start, end, needs = intervals[name]
        own = (end - start).total_seconds()
        # Include time from the attempt start to the first job on a path. If
        # this is omitted, every root job begins at t=0 and queue delay before
        # the first root is lost from the DAG elapsed-time result.
        root_prefix = (start - path_start).total_seconds() if path_start else 0.0
        best = root_prefix + own
        for parent in needs:
            parent_start, parent_end, _ = intervals[parent]
            if parent_end > start:
                raise ValueError('dependency interval overlap')
            candidate = path(parent) + (start - parent_end).total_seconds() + own
            best = max(best, candidate)
        visiting.remove(name)
        memo[name] = best
        return best

    try:
        return round(max(path(name) for name in by_name), 3)
    except ValueError:
        return NOT_MEASURED


def summarize_runs(records, now=None):
    """Summarize run/job snapshots; duplicate evidence is never double-counted."""
    del now  # Incomplete jobs are NOT_MEASURED, never silently timed to now.
    unique_runs = {}
    duplicate_runs_removed = 0
    duplicate_run_conflicts = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('run'), dict):
            duplicate_run_conflicts += 1
            continue
        run = record['run']
        run_id, attempt = run.get('id'), run.get('run_attempt')
        if run_id is None or attempt is None:
            duplicate_run_conflicts += 1
            continue
        key = (str(run_id), str(attempt))
        encoded = _canonical(record)
        if key in unique_runs:
            if unique_runs[key]['encoded'] == encoded:
                duplicate_runs_removed += 1
            else:
                duplicate_run_conflicts += 1
                unique_runs[key]['conflict'] = True
        else:
            unique_runs[key] = {'record': record, 'encoded': encoded, 'conflict': False}

    reports = []
    all_jobs = []
    duplicate_jobs_removed = 0
    duplicate_job_conflicts = 0
    incomplete = duplicate_run_conflicts > 0
    for (run_id, attempt), entry in unique_runs.items():
        if entry['conflict']:
            incomplete = True
            continue
        record, run = entry['record'], entry['record']['run']
        created = _timestamp(run.get('created_at'))
        started = _timestamp(run.get('run_started_at'))
        finished = (_timestamp(run.get('completed_at') or run.get('updated_at'))
                    if run.get('status') == 'completed' else None)
        workflow_queue_wait = _seconds(created, started)
        workflow_elapsed = _seconds(created, finished)
        workflow_execution = _seconds(started, finished)
        if workflow_queue_wait is None or workflow_elapsed is None or workflow_execution is None:
            incomplete = True

        raw_jobs = record.get('jobs') if isinstance(record.get('jobs'), list) else []
        unique_jobs = {}
        local_conflicts = set()
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                duplicate_job_conflicts += 1
                incomplete = True
                continue
            key = _job_key(run_id, attempt, raw)
            if key is None:
                duplicate_job_conflicts += 1
                incomplete = True
                continue
            encoded = _canonical(raw)
            if key in unique_jobs:
                if unique_jobs[key][1] == encoded:
                    duplicate_jobs_removed += 1
                else:
                    duplicate_job_conflicts += 1
                    local_conflicts.add(key)
                    incomplete = True
            else:
                unique_jobs[key] = (raw, encoded)

        projected = []
        for key, (raw, _) in unique_jobs.items():
            if key in local_conflicts:
                continue
            observed_at = (record.get('job_observed_at') or {}).get(str(raw.get('id')))
            item = _job_projection(raw, run_id, attempt, started, observed_at)
            item['_start'] = _timestamp(raw.get('started_at'))
            item['_end'] = _timestamp(raw.get('completed_at'))
            if item['runner_class'] == 'UNKNOWN' and item['conclusion'] != 'skipped':
                incomplete = True
            if item['status'] != 'completed' and item['conclusion'] != 'skipped':
                incomplete = True
            if item['runner_seconds'] == NOT_MEASURED and item['status'] != 'queued' and item['conclusion'] != 'skipped':
                incomplete = True
            if item['queue_wait_seconds'] == NOT_MEASURED and item['status'] != 'queued' and item['conclusion'] != 'skipped':
                incomplete = True
            projected.append(item)
            all_jobs.append(item)

        active_jobs = [item for item in projected if not item['carried_over']]
        dependency_map = record.get('job_dependencies')
        if isinstance(dependency_map, dict):
            active_names = {item['name'] for item in active_jobs}
            dependency_map = {name: [parent for parent in parents if parent in active_names]
                              for name, parents in dependency_map.items() if name in active_names}
        dependency_path = (_dependency_path(active_jobs, dependency_map, started)
                           if run.get('status') == 'completed' else NOT_MEASURED)
        visible_jobs = [{key: value for key, value in item.items() if not key.startswith('_')}
                        for item in projected]
        stage_values = {}
        stage_source = record.get('prepareStages') or record.get('stage_timings') or {}
        if isinstance(stage_source, dict):
            for name, value in stage_source.items():
                if not isinstance(name, str) or not isinstance(value, dict):
                    continue
                seconds = value.get('seconds')
                if isinstance(seconds, (int, float)) and seconds >= 0:
                    stage_values[name] = float(seconds)
        reports.append({
            'run_id': run_id,
            'attempt': attempt,
            'workflow': str(run.get('name') or run.get('workflowName') or 'UNKNOWN'),
            'status': str(run.get('status') or 'UNKNOWN'),
            'conclusion': str(run.get('conclusion') or 'UNKNOWN'),
            'created_at': _iso(run.get('created_at')),
            'started_at': _iso(run.get('run_started_at')),
            'completed_at': _iso(run.get('completed_at') or run.get('updated_at')
                                 if run.get('status') == 'completed' else None),
            'workflow_queue_wait_seconds': workflow_queue_wait if workflow_queue_wait is not None else NOT_MEASURED,
            'workflow_elapsed_seconds': workflow_elapsed if workflow_elapsed is not None else NOT_MEASURED,
            'workflow_execution_seconds': workflow_execution if workflow_execution is not None else NOT_MEASURED,
            'job_dependency_critical_path_seconds': dependency_path,
            'stage_timings': stage_values,
            'jobs': visible_jobs,
        })

    totals = {}
    for runner_class, prefix in (('GITHUB_HOSTED', 'GITHUB_HOSTED'), ('SELF_HOSTED', 'SELF_HOSTED')):
        selected = [job for job in all_jobs if job['runner_class'] == runner_class and not job['carried_over']
                    and job['status'] == 'completed'
                    and job['conclusion'] != 'skipped']
        totals[prefix + '_JOB_COUNT'] = len(selected)
        seconds = _sum_measured(selected, 'runner_seconds') if selected else 0.0
        minutes = (sum(int(job['job_minutes_estimate']) for job in selected)
                   if all(job['job_minutes_estimate'] != NOT_MEASURED for job in selected) else NOT_MEASURED)
        if duplicate_run_conflicts or duplicate_job_conflicts:
            seconds = minutes = NOT_MEASURED
        totals[prefix + '_RUNNER_SECONDS'] = seconds
        totals[prefix + '_JOB_MINUTES_ESTIMATE'] = minutes
        active = [job for job in all_jobs if job['runner_class'] == runner_class and not job['carried_over']
                  and job['status'] == 'in_progress']
        totals[prefix + '_IN_PROGRESS_JOB_COUNT'] = len(active)
        totals[prefix + '_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND'] = (
            _sum_measured(active, 'elapsed_seconds_lower_bound_at_collection') if active else 0.0
        )
    totals['UNKNOWN_RUNNER_JOB_COUNT'] = sum(
        1 for job in all_jobs if job['runner_class'] == 'UNKNOWN' and not job['carried_over'] and job['status'] == 'completed'
        and job['conclusion'] != 'skipped')
    totals['UNKNOWN_IN_PROGRESS_JOB_COUNT'] = sum(
        1 for job in all_jobs if job['runner_class'] == 'UNKNOWN'
        and not job['carried_over'] and job['status'] == 'in_progress')
    unknown_active = [job for job in all_jobs if job['runner_class'] == 'UNKNOWN'
                      and not job['carried_over'] and job['status'] == 'in_progress']
    totals['UNKNOWN_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND'] = (
        _sum_measured(unknown_active, 'elapsed_seconds_lower_bound_at_collection') if unknown_active else 0.0
    )
    if totals['UNKNOWN_RUNNER_JOB_COUNT']:
        for prefix in ('GITHUB_HOSTED', 'SELF_HOSTED'):
            totals[prefix + '_RUNNER_SECONDS'] = NOT_MEASURED
            totals[prefix + '_JOB_MINUTES_ESTIMATE'] = NOT_MEASURED
    totals['DUPLICATE_RUNS_REMOVED'] = duplicate_runs_removed
    totals['DUPLICATE_JOBS_REMOVED'] = duplicate_jobs_removed

    # Run wall time is the actual observed critical path through the workflow,
    # including dependency waits and platform orchestration. Never add parallel
    # job durations to claim elapsed time.
    critical_path = (max((r['workflow_execution_seconds'] for r in reports
                          if isinstance(r['workflow_execution_seconds'], (int, float))), default=None)
                     if len(reports) == 1 and reports[0]['workflow_execution_seconds'] != NOT_MEASURED else None)
    stage_path = NOT_MEASURED  # Stage receipts have durations, not absolute intervals/dependencies.
    return {
        'schema': 1,
        'status': 'PARTIAL' if incomplete or not reports else 'MEASURED',
        'runs': reports,
        'jobs': [{key: value for key, value in item.items() if not key.startswith('_')} for item in all_jobs],
        'totals': totals,
        'critical_path_elapsed_seconds': critical_path if critical_path is not None else NOT_MEASURED,
        'critical_path_basis': 'completed workflow start-to-finish wall interval; parallel jobs are not summed',
        'job_dependency_critical_path_seconds': (reports[0]['job_dependency_critical_path_seconds']
                                                  if len(reports) == 1 else NOT_MEASURED),
        'stage_critical_path_seconds': stage_path,
        'duplicate_runs_removed': duplicate_runs_removed,
        'duplicate_jobs_removed': duplicate_jobs_removed,
        'duplicate_job_conflicts': duplicate_job_conflicts,
        'duplicate_run_conflicts': duplicate_run_conflicts,
        'incomplete_evidence': incomplete,
        'minute_estimate_note': 'Per-job ceil(seconds / 60) estimate, not billing.',
    }


def render_report(report):
    """Render a bounded report from the allowlisted summarized data only."""
    lines = [
        '# GPoint release telemetry',
        '',
        f"Status: {report['status']}",
        f"Critical-path elapsed: {report['critical_path_elapsed_seconds']} seconds",
        f"Basis: {report['critical_path_basis']}",
        f"Stage critical path: {report['stage_critical_path_seconds']}",
        f"Completed hosted jobs / runner seconds / per-job ceil estimate: {report['totals']['GITHUB_HOSTED_JOB_COUNT']} / "
        f"{report['totals']['GITHUB_HOSTED_RUNNER_SECONDS']} / "
        f"{report['totals']['GITHUB_HOSTED_JOB_MINUTES_ESTIMATE']}",
        f"Completed self-hosted jobs / runner seconds / per-job ceil estimate: {report['totals']['SELF_HOSTED_JOB_COUNT']} / "
        f"{report['totals']['SELF_HOSTED_RUNNER_SECONDS']} / "
        f"{report['totals']['SELF_HOSTED_JOB_MINUTES_ESTIMATE']}",
        f"In-progress hosted jobs / elapsed lower bound: {report['totals']['GITHUB_HOSTED_IN_PROGRESS_JOB_COUNT']} / "
        f"{report['totals']['GITHUB_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND']}s",
        f"In-progress self-hosted jobs / elapsed lower bound: {report['totals']['SELF_HOSTED_IN_PROGRESS_JOB_COUNT']} / "
        f"{report['totals']['SELF_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND']}s",
        report['minute_estimate_note'],
        '',
        '| Run | Attempt | Status | Created | Started | Completed | Created→finish | Queued | Execution | Dependency path |',
        '|---:|---:|---|---|---|---|---:|---:|---:|---:|',
    ]
    for run in report['runs']:
        lines.append('| {run_id} | {attempt} | {status} / {conclusion} | {created} | {started} | {completed} | {elapsed} | {queue} | {execution} | {dependency} |'.format(
            run_id=run['run_id'], attempt=run['attempt'], status=run['status'], conclusion=run['conclusion'],
            created=run['created_at'], started=run['started_at'], completed=run['completed_at'],
            elapsed=run['workflow_elapsed_seconds'], queue=run['workflow_queue_wait_seconds'],
            execution=run['workflow_execution_seconds'], dependency=run['job_dependency_critical_path_seconds']))
        lines += ['', f"### Jobs for run {run['run_id']} attempt {run['attempt']}",
                  '', '| Job | ID | Runner | Status | Queue wait (s) | Runner (s) | Minutes (ceil estimate) | Elapsed at collection (lower bound, s) |',
                  '|---|---:|---|---|---:|---:|---:|---:|']
        for job in run['jobs']:
            lines.append('| {name} | {id} | {runner} | {status}/{conclusion} | {queue} | {seconds} | {minutes} | {lower_bound} |'.format(
                name=job['name'], id=job['job_id'], runner=job['runner_class'], status=job['status'],
                conclusion=job['conclusion'], queue=job['queue_wait_seconds'], seconds=job['runner_seconds'],
                minutes=job['job_minutes_estimate'],
                lower_bound=job['elapsed_seconds_lower_bound_at_collection']))
        if run['stage_timings']:
            lines += ['', 'Measured stages (individual only; no aggregation because overlap is unknown):', '']
            lines.extend(f"- `{name}`: {seconds:.3f}s" for name, seconds in sorted(run['stage_timings'].items()))
    lines += ['', 'Stage timings are listed individually; they are not summed because stage overlap is unknown.']
    return '\n'.join(lines) + '\n'


def summarize(jobs_by_run, now=None):
    """Compatibility adapter for the previous runner-only unit-test API."""
    now = now or dt.datetime.now(UTC)
    result = {'GITHUB_HOSTED_JOB_COUNT': 0, 'GITHUB_HOSTED_RUNNER_SECONDS': 0,
              'SELF_HOSTED_JOB_COUNT': 0, 'SELF_HOSTED_RUNNER_SECONDS': 0}
    for jobs in jobs_by_run:
        for job in jobs:
            if job.get('status') == 'queued' or job.get('conclusion') == 'skipped' or not job.get('started_at'):
                continue
            labels = job.get('labels') or []
            name = str(job.get('runner_name') or '')
            start = _timestamp(job['started_at'])
            end = _timestamp(job.get('completed_at')) or now
            seconds = _seconds(start, end)
            if seconds is None:
                continue
            prefix = 'SELF_HOSTED' if 'self-hosted' in labels or name.startswith('gpoint-owner-mac') else 'GITHUB_HOSTED'
            result[prefix + '_JOB_COUNT'] += 1
            result[prefix + '_RUNNER_SECONDS'] += round(seconds)
    return result


def _fetch_records(repo, run_ids, token):
    records = []
    for run_id in sorted(set(run_ids)):
        base = f'https://api.github.com/repos/{repo}/actions/runs/{run_id}'
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        request = urllib.request.Request(base, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            run = json.loads(response.read())
        last_attempt = run.get('run_attempt')
        if not isinstance(last_attempt, int) or last_attempt < 1:
            raise ValueError('run attempt metadata unavailable')
        for attempt in range(1, last_attempt + 1):
            attempt_request = urllib.request.Request(f'{base}/attempts/{attempt}', headers=headers)
            with urllib.request.urlopen(attempt_request, timeout=20) as response:
                attempt_details = json.loads(response.read())
            if str(attempt_details.get('run_attempt')) != str(attempt):
                raise ValueError('run attempt identity mismatch')
            attempt_run = {**run, **attempt_details, 'id': int(run_id), 'run_attempt': attempt}
            # The API's created_at belongs to the original workflow run. If an
            # attempt detail does not supply its own creation time, do not turn
            # prior attempts/retry delay into this attempt's queue wait.
            if attempt > 1 and not attempt_details.get('created_at'):
                attempt_run['created_at'] = None
            jobs = []
            job_observed_at = {}
            page = 1
            total_count = None
            while total_count is None or len(jobs) < total_count:
                jobs_request = urllib.request.Request(
                    f'{base}/attempts/{attempt}/jobs?per_page=100&page={page}', headers=headers)
                with urllib.request.urlopen(jobs_request, timeout=20) as response:
                    payload = json.loads(response.read())
                page_observed_at = dt.datetime.now(UTC).isoformat().replace('+00:00', 'Z')
                page_jobs = payload.get('jobs')
                if not isinstance(page_jobs, list) or not isinstance(payload.get('total_count'), int):
                    raise ValueError('job metadata unavailable')
                if total_count is None:
                    total_count = payload['total_count']
                jobs.extend(page_jobs)
                for page_job in page_jobs:
                    if isinstance(page_job, dict) and page_job.get('id') is not None:
                        job_observed_at[str(page_job['id'])] = page_observed_at
                if not page_jobs and len(jobs) < total_count:
                    raise ValueError('job metadata pagination incomplete')
                page += 1
            records.append({'run': attempt_run, 'jobs': jobs, 'job_observed_at': job_observed_at})
    return records


def _flat_outputs(report, cache_prevented_download='NO'):
    totals = report['totals']
    cache_value = cache_prevented_download if cache_prevented_download in ('YES', 'NO') else NOT_MEASURED
    values = {
        'GITHUB_HOSTED_JOB_COUNT': totals['GITHUB_HOSTED_JOB_COUNT'],
        'GITHUB_HOSTED_RUNNER_SECONDS': totals['GITHUB_HOSTED_RUNNER_SECONDS'],
        'GITHUB_HOSTED_JOB_MINUTES_ESTIMATE': totals['GITHUB_HOSTED_JOB_MINUTES_ESTIMATE'],
        'SELF_HOSTED_JOB_COUNT': totals['SELF_HOSTED_JOB_COUNT'],
        'SELF_HOSTED_RUNNER_SECONDS': totals['SELF_HOSTED_RUNNER_SECONDS'],
        'SELF_HOSTED_JOB_MINUTES_ESTIMATE': totals['SELF_HOSTED_JOB_MINUTES_ESTIMATE'],
        'GITHUB_HOSTED_IN_PROGRESS_JOB_COUNT': totals['GITHUB_HOSTED_IN_PROGRESS_JOB_COUNT'],
        'GITHUB_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND': totals['GITHUB_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND'],
        'SELF_HOSTED_IN_PROGRESS_JOB_COUNT': totals['SELF_HOSTED_IN_PROGRESS_JOB_COUNT'],
        'SELF_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND': totals['SELF_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND'],
        'UNKNOWN_IN_PROGRESS_JOB_COUNT': totals['UNKNOWN_IN_PROGRESS_JOB_COUNT'],
        'UNKNOWN_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND': totals['UNKNOWN_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND'],
        'WORKFLOW_CRITICAL_PATH_SECONDS': report['critical_path_elapsed_seconds'],
        'WORKFLOW_QUEUE_WAIT_SECONDS': report['runs'][0]['workflow_queue_wait_seconds'] if len(report['runs']) == 1 else NOT_MEASURED,
        'WORKFLOW_ELAPSED_SECONDS': report['runs'][0]['workflow_elapsed_seconds'] if len(report['runs']) == 1 else NOT_MEASURED,
        'OWNER_WAIT_SECONDS': NOT_MEASURED,
        'TELEMETRY_STATUS': report['status'],
        'DUPLICATE_RUNS_REMOVED': totals['DUPLICATE_RUNS_REMOVED'],
        'DUPLICATE_JOBS_REMOVED': totals['DUPLICATE_JOBS_REMOVED'],
        'UNKNOWN_RUNNER_JOB_COUNT': totals['UNKNOWN_RUNNER_JOB_COUNT'],
        'DUPLICATE_HOSTED_WORK_REMOVED': NOT_MEASURED,
        'CACHE_PREVENTED_DOWNLOAD': cache_value,
    }
    return values


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--input':
        if len(argv) != 2:
            raise SystemExit('usage: release-telemetry.py --input <run-job-snapshots.json>')
        data = json.loads(Path(argv[1]).read_text(encoding='utf-8'))
        records = data.get('runs') if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise SystemExit('input must contain a runs array')
        report = summarize_runs(records)
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0 if report['status'] == 'MEASURED' else 1

    token = os.environ.get('GITHUB_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY', 'akhripunov/gpoint')
    run_ids = [value for value in os.environ.get('TELEMETRY_RUN_IDS', '').split(',') if value.isdigit()]
    if not token or not run_ids:
        raise SystemExit('Telemetry requires GITHUB_TOKEN and at least one run ID')
    try:
        records = _fetch_records(repo, run_ids, token)
        report = summarize_runs(records)
    except Exception as exc:  # Keep error output free of URL headers/API payloads.
        print(f'TELEMETRY_COLLECTION_ERROR={type(exc).__name__}', file=sys.stderr)
        report = summarize_runs([])
        report['status'] = 'NOT_MEASURED'
        for key in ('GITHUB_HOSTED_JOB_COUNT', 'GITHUB_HOSTED_RUNNER_SECONDS',
                    'GITHUB_HOSTED_JOB_MINUTES_ESTIMATE', 'SELF_HOSTED_JOB_COUNT',
                    'SELF_HOSTED_RUNNER_SECONDS', 'SELF_HOSTED_JOB_MINUTES_ESTIMATE',
                    'GITHUB_HOSTED_IN_PROGRESS_JOB_COUNT',
                    'GITHUB_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND',
                    'SELF_HOSTED_IN_PROGRESS_JOB_COUNT',
                    'SELF_HOSTED_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND',
                    'UNKNOWN_IN_PROGRESS_JOB_COUNT',
                    'UNKNOWN_IN_PROGRESS_ELAPSED_SECONDS_LOWER_BOUND',
                    'UNKNOWN_RUNNER_JOB_COUNT', 'DUPLICATE_RUNS_REMOVED', 'DUPLICATE_JOBS_REMOVED'):
            report['totals'][key] = NOT_MEASURED
    outputs = _flat_outputs(report, os.environ.get('CACHE_PREVENTED_DOWNLOAD', 'NO'))
    output = os.environ.get('GITHUB_OUTPUT')
    if not output:
        raise SystemExit('GITHUB_OUTPUT is required')
    with open(output, 'a', encoding='utf-8') as stream:
        for key, value in outputs.items():
            stream.write(f'{key}={value}\n')
    for key, value in outputs.items():
        print(f'{key}={value}')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(render_report(report))
    report_path = os.environ.get('TELEMETRY_REPORT_PATH')
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    # Partial measurements and unavailable telemetry are data states, not
    # release-gate failures. Preserve NOT_MEASURED values and keep publication
    # runnable; only malformed local input or unavailable output files fail.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
