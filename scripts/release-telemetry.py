#!/usr/bin/env python3
"""Measure hosted/self-hosted wall time for existing GPoint release runs."""
import datetime as dt
import json
import os
import urllib.request


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
    for run_id in ids:
        request = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100',
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=20) as response:
            groups.append(json.loads(response.read()).get('jobs', []))
    values = summarize(groups)
    values['DUPLICATE_HOSTED_WORK_REMOVED'] = 'YES'
    values['CACHE_PREVENTED_DOWNLOAD'] = os.environ.get('CACHE_PREVENTED_DOWNLOAD', 'NO')
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
