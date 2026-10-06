#!/usr/bin/env python3
"""Measure hosted/self-hosted wall time for existing GPoint release runs."""
import datetime as dt
import json
import os
import urllib.request


def _timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def _seconds(start, end):
    if start is None or end is None:
        return None
    seconds = round((end - start).total_seconds())
    if seconds < 0:
        raise ValueError('Release timeline timestamps are not monotonic')
    return seconds


def timeline_metrics(*, commit_at, prepare, preflight, production=None):
    commit = _timestamp(commit_at)
    prepare_start, prepare_end = _timestamp(prepare.get('run_started_at')), _timestamp(prepare.get('updated_at'))
    preflight_start, preflight_end = _timestamp(preflight.get('run_started_at')), _timestamp(preflight.get('updated_at'))
    if not all((commit, prepare_start, prepare_end, preflight_start, preflight_end)):
        raise ValueError('Release run timestamps are incomplete')
    result = {
        'COMMIT_TO_PREPARE_START_SECONDS': _seconds(commit, prepare_start),
        'PREPARE_DURATION_SECONDS': _seconds(prepare_start, prepare_end),
        'PREPARE_TO_PREFLIGHT_START_SECONDS': _seconds(prepare_end, preflight_start),
        'PREFLIGHT_DURATION_SECONDS': _seconds(preflight_start, preflight_end),
    }
    if production:
        deploy_start, deploy_end = _timestamp(production.get('run_started_at')), _timestamp(production.get('updated_at'))
        if not all((deploy_start, deploy_end)):
            raise ValueError('Production Deploy timestamps are incomplete')
        result['PREFLIGHT_TO_DEPLOY_START_SECONDS'] = _seconds(preflight_end, deploy_start)
        result['DEPLOY_DURATION_SECONDS'] = _seconds(deploy_start, deploy_end)
        result['COMMIT_TO_COMPLETE_SECONDS'] = _seconds(commit, deploy_end)
    else:
        result['COMMIT_TO_READY_SECONDS'] = _seconds(commit, preflight_end)
    return result


def summarize(jobs_by_run, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    result = {'GITHUB_HOSTED_JOB_COUNT': 0, 'GITHUB_HOSTED_RUNNER_SECONDS': 0,
              'SELF_HOSTED_JOB_COUNT': 0, 'SELF_HOSTED_RUNNER_SECONDS': 0}
    for jobs in jobs_by_run:
        for job in jobs:
            if job.get('status') == 'queued' or job.get('conclusion') == 'skipped' or not job.get('started_at'):
                continue
            labels = job.get('labels') or []
            name = str(job.get('runner_name') or '')
            start = dt.datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))
            end = (dt.datetime.fromisoformat(job['completed_at'].replace('Z', '+00:00'))
                   if job.get('completed_at') else now)
            seconds = max(0, round((end - start).total_seconds()))
            prefix = 'SELF_HOSTED' if 'self-hosted' in labels or name.startswith('gpoint-owner-mac') else 'GITHUB_HOSTED'
            result[prefix + '_JOB_COUNT'] += 1
            result[prefix + '_RUNNER_SECONDS'] += seconds
    return result


def main():
    token = os.environ['GITHUB_TOKEN']
    repo = os.environ.get('GITHUB_REPOSITORY', 'akhripunov/gpoint')
    ids = sorted({value for value in os.environ.get('TELEMETRY_RUN_IDS', '').split(',') if value.isdigit()})
    if not ids:
        raise SystemExit('Telemetry requires at least the current workflow run ID')
    groups = []
    jobs_by_run = {}
    runs = {}
    for run_id in ids:
        request = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100',
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=20) as response:
            jobs = json.loads(response.read()).get('jobs', [])
            groups.append(jobs)
            jobs_by_run[run_id] = jobs
        run_request = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/runs/{run_id}',
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(run_request, timeout=20) as response:
            runs[run_id] = json.loads(response.read())
    values = summarize(groups)
    values['DUPLICATE_HOSTED_WORK_REMOVED'] = 'YES'
    values['CACHE_PREVENTED_DOWNLOAD'] = os.environ.get('CACHE_PREVENTED_DOWNLOAD', 'NO')
    values['BUNDLE_DOWNLOAD_SECONDS'] = os.environ.get('BUNDLE_DOWNLOAD_SECONDS', 'UNKNOWN')
    mode = os.environ.get('RELEASE_MODE', 'preflight')
    prepare_id = os.environ.get('PREPARE_RUN_ID', '')
    preflight_id = os.environ.get('READY_PREFLIGHT_RUN_ID', '')
    production_id = os.environ.get('PRODUCTION_RUN_ID', '')
    if mode == 'preflight':
        preflight_id = os.environ.get('GITHUB_RUN_ID', '')
    elif mode == 'production':
        production_id = os.environ.get('GITHUB_RUN_ID', '')
    if not prepare_id.isdigit() or not preflight_id.isdigit():
        raise SystemExit('Telemetry requires exact Prepare and READY preflight run IDs')
    candidate = os.environ.get('CANDIDATE_SHA', '')
    commit_request = urllib.request.Request(
        f'https://api.github.com/repos/{repo}/commits/{candidate}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(commit_request, timeout=20) as response:
        commit_date = json.loads(response.read()).get('commit', {}).get('committer', {}).get('date')
    def job_bounds(run_id, name):
        matches = [job for job in jobs_by_run.get(run_id, []) if job.get('name', '').split(' ', 1)[0] == name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or not matches[0].get('completed_at'):
            raise ValueError('canonical stage job is not uniquely complete')
        return {'run_started_at': matches[0]['started_at'], 'updated_at': matches[0]['completed_at']}
    timeline_keys = ('COMMIT_TO_PREPARE_START_SECONDS', 'PREPARE_DURATION_SECONDS',
        'PREPARE_TO_PREFLIGHT_START_SECONDS', 'PREFLIGHT_DURATION_SECONDS', 'COMMIT_TO_READY_SECONDS',
        'PREFLIGHT_TO_DEPLOY_START_SECONDS', 'DEPLOY_DURATION_SECONDS', 'COMMIT_TO_COMPLETE_SECONDS')
    try:
        values.update(timeline_metrics(commit_at=commit_date,
            prepare=job_bounds(prepare_id, 'prepare'), preflight=job_bounds(preflight_id, 'deploy'),
            production=job_bounds(production_id, 'deploy') if production_id else None))
        values['TIMELINE_EVIDENCE'] = 'PASS'
    except (KeyError, ValueError):
        values.update({key: 'UNKNOWN' for key in timeline_keys})
        values['TIMELINE_EVIDENCE'] = 'INCOMPLETE'
    output = os.environ.get('GITHUB_OUTPUT')
    if not output:
        raise SystemExit('GITHUB_OUTPUT is required')
    with open(output, 'a', encoding='utf-8') as stream:
        for key, value in values.items():
            stream.write(f'{key}={value}\n')
    for key, value in values.items():
        print(f'{key}={value}')


if __name__ == '__main__':
    main()
