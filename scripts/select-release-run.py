#!/usr/bin/env python3
"""Select only the newest successful preparation of the current main SHA."""
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_assets import resolve_asset
from network_retry import NetworkUnavailable, RetryBudget, retry_network_read

REPO = 'akhripunov/gpoint'
NETWORK_BUDGET = RetryBudget()


def transient(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 425, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, ssl.SSLError):
        return not any(marker in str(reason).lower() for marker in (
            'certificate_verify_failed', 'wrong_version_number', 'protocol_version'))
    return isinstance(reason, (TimeoutError, socket.timeout, socket.gaierror,
                               ConnectionError))


def github(path):
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/{path}', headers={
        'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}",
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    def read():
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    try:
        return retry_network_read(read, 'release_selection', transient, budget=NETWORK_BUDGET)
    except NetworkUnavailable:
        raise RuntimeError('NETWORK_UNAVAILABLE stage=release_selection') from None


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
    asset = resolve_asset('prepare', sha, str(selected['id']), str(selected.get('run_attempt', 1)),
                          'bundle.zip', github)
    if os.environ.get('PREPARE_RUN_ID') and (str(selected['id']) != os.environ['PREPARE_RUN_ID'] or str(selected.get('run_attempt', 1)) != os.environ.get('PREPARE_RUN_ATTEMPT')):
        raise RuntimeError('Prepare run/attempt changed after selection')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f"run_id={selected['id']}\nrun_attempt={selected.get('run_attempt', 1)}\nartifact_id={asset['id']}\ncandidate={sha}\n")
        state = os.environ.get('NETWORK_RETRY_BUDGET_STATE')
        used = Path(state).read_text().strip() if state and Path(state).exists() else '0'
        output.write(f'network_retry_budget_used_seconds={used}\n')


if __name__ == '__main__':
    main()
