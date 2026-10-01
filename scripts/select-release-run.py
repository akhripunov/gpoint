#!/usr/bin/env python3
"""Select only the newest successful preparation of the current main SHA."""
import json
import os
import re
import urllib.request

REPO = 'akhripunov/gpoint'


def github(path):
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/{path}', headers={
        'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}",
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def select_run(sha, runs):
    matches = [r for r in runs if r.get('head_sha') == sha and r.get('head_branch') == 'main'
               and r.get('event') in ('push', 'workflow_dispatch')]
    if not matches:
        raise RuntimeError('No preparation run exists for this candidate')
    latest = max(matches, key=lambda r: (r['id'], r.get('run_attempt', 1)))
    if latest.get('path') not in ('.github/workflows/gpoint-prepare.yml', None):
        raise RuntimeError('Unexpected preparation workflow identity')
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise RuntimeError('Latest candidate preparation is not successful; deploy is blocked')
    return latest


def validate_request(sha, ref, mode, approval):
    if not re.fullmatch('[a-f0-9]{40}', sha):
        raise RuntimeError('Enter the complete 40-character candidate SHA')
    if ref != 'refs/heads/main' or mode not in ('preflight', 'production'):
        raise RuntimeError('Select main and a valid release mode')
    if mode == 'production' and approval != 'true':
        raise RuntimeError('Explicit production approval is required')


def main():
    sha = os.environ['CANDIDATE_SHA']
    validate_request(sha, os.environ.get('GITHUB_REF'), os.environ.get('RELEASE_MODE'), os.environ.get('APPROVE_PRODUCTION'))
    if github('git/ref/heads/main')['object']['sha'] != sha:
        raise RuntimeError('Candidate is no longer origin/main; prepare the new candidate')
    runs = github(f'actions/workflows/gpoint-prepare.yml/runs?branch=main&head_sha={sha}&per_page=100')['workflow_runs']
    selected = select_run(sha, runs)
    artifacts = github(f"actions/runs/{selected['id']}/artifacts")['artifacts']
    matches = [a for a in artifacts if a['name'] == f'gpoint-release-{sha}' and not a['expired']]
    if len(matches) != 1:
        raise RuntimeError('The immutable candidate artifact is missing or expired')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f"run_id={selected['id']}\nartifact_id={matches[0]['id']}\ncandidate={sha}\n")


if __name__ == '__main__':
    main()
