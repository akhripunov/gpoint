#!/usr/bin/env python3
"""Select only the newest successful preparation of the current main SHA."""
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def validate_request(sha, ref, mode, approval, eros_pilot_approval='false', eros_acl_approval='false'):
    if not re.fullmatch('[a-f0-9]{40}', sha):
        raise RuntimeError('Enter the complete 40-character candidate SHA')
    if ref != 'refs/heads/main' or mode not in ('preflight', 'production', 'task-scoped-auto', 'eros-pilot', 'eros-acl-plan', 'eros-acl-apply', 'eros-acl-rollback', 'eros-acl-reopen'):
        raise RuntimeError('Select main and a valid release mode')
    if mode == 'production' and approval != 'true':
        raise RuntimeError('Explicit exact-candidate production approval is required')
    if mode == 'eros-pilot' and (approval != 'false' or eros_pilot_approval != 'true'):
        raise RuntimeError('Eros Pilot requires its separate exact-candidate approval and cannot use production approval')
    if mode == 'task-scoped-auto' and approval != 'false':
        raise RuntimeError('TASK_SCOPED_AUTO uses its durable authorization record, not the exact-SHA approval input')
    if mode == 'eros-acl-plan' and eros_acl_approval != 'false':
        raise RuntimeError('Eros ACL plan is read-only and does not accept Apply approval')
    if mode in ('eros-acl-apply', 'eros-acl-rollback', 'eros-acl-reopen') and eros_acl_approval != 'true':
        raise RuntimeError('Eros ACL mutation requires its separate exact-SHA approval')


def validate_owner_approval(mode, actor, triggering_actor, repository_owner):
    if mode in ('production', 'eros-pilot', 'eros-acl-apply', 'eros-acl-rollback', 'eros-acl-reopen') and (not repository_owner or actor != repository_owner
                                 or triggering_actor != repository_owner):
        raise RuntimeError('Exact-SHA protected Deploy must be dispatched and triggered by the repository OWNER')


def validate_expected_run(selected, expected_run, expected_attempt):
    expected_run, expected_attempt = (expected_run or '').strip(), (expected_attempt or '').strip()
    if bool(expected_run) != bool(expected_attempt):
        raise RuntimeError('Expected Prepare run ID and attempt must be supplied together')
    if expected_run and (not expected_run.isdigit() or not expected_attempt.isdigit()):
        raise RuntimeError('Expected Prepare run identity is invalid')
    if expected_run and (str(selected['id']) != expected_run
                         or str(selected.get('run_attempt', 1)) != expected_attempt):
        raise RuntimeError('Prepare run/attempt changed after selection')


def validate_current_main(sha, current_main_sha):
    if current_main_sha != sha:
        raise RuntimeError('Candidate is no longer origin/main; prepare the new candidate')


def main():
    sha = os.environ['CANDIDATE_SHA']
    orchestrator_run_id = os.environ.get('ORCHESTRATOR_RUN_ID', '').strip()
    if orchestrator_run_id and not orchestrator_run_id.isdigit():
        raise RuntimeError('Automatic orchestrator run ID is invalid')
    validate_request(sha, os.environ.get('GITHUB_REF'), os.environ.get('RELEASE_MODE'),
                     os.environ.get('APPROVE_PRODUCTION'), os.environ.get('APPROVE_EROS_PILOT', 'false'),
                     os.environ.get('APPROVE_EROS_ACL', 'false'))
    validate_owner_approval(os.environ.get('RELEASE_MODE'), os.environ.get('OWNER_APPROVAL_ACTOR', ''),
                            os.environ.get('OWNER_TRIGGERING_ACTOR', ''),
                            os.environ.get('OWNER_REPOSITORY', ''))
    current_main_sha = github('git/ref/heads/main')['object']['sha']
    validate_current_main(sha, current_main_sha)
    authorization_id = os.environ.get('TASK_AUTHORIZATION_ID', '').strip()
    if os.environ.get('RELEASE_MODE') == 'task-scoped-auto' or (os.environ.get('RELEASE_MODE') == 'preflight' and authorization_id):
        if os.environ.get('RELEASE_MODE') == 'task-scoped-auto' and not authorization_id:
            raise RuntimeError('TASK_SCOPED_AUTO requires a durable authorization ID')
        env = {**os.environ, 'TASK_AUTHORIZATION_ID': authorization_id,
               'TASK_CURRENT_MAIN_SHA': sha, 'CANDIDATE_SHA': sha}
        checked = subprocess.run(['node', 'scripts/validate-task-authorization.mjs'],
                                  cwd=Path(__file__).resolve().parents[1], env=env,
                                  capture_output=True, text=True, timeout=30)
        if checked.returncode != 0:
            raise RuntimeError('TASK_SCOPED_AUTO authorization is invalid')
        print(checked.stdout.strip())
    elif authorization_id:
        raise RuntimeError('Task authorization ID is only valid for task-scoped-auto or linked preflight mode')
    runs = github(f'actions/workflows/gpoint-prepare.yml/runs?branch=main&head_sha={sha}&per_page=100')['workflow_runs']
    selected = select_run(sha, runs)
    expected_run = os.environ.get('PREPARE_RUN_ID')
    expected_attempt = os.environ.get('PREPARE_RUN_ATTEMPT')
    validate_expected_run(selected, expected_run, expected_attempt)
    # Production/task-scoped Deploy performs the read-only preflight on the Mac
    # immediately before apply. A separate diagnostic preflight is optional.
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f"run_id={selected['id']}\nrun_attempt={selected.get('run_attempt', 1)}\ncandidate={sha}\n")
        output.write(f"receipt_tag=prepare-{sha}-{selected['id']}-{selected.get('run_attempt', 1)}\n")
        output.write(f'orchestrator_run_id={orchestrator_run_id}\n')
        output.write(f'task_authorization_id={authorization_id}\n')
        state = os.environ.get('NETWORK_RETRY_BUDGET_STATE')
        used = Path(state).read_text().strip() if state and Path(state).exists() else '0'
        output.write(f'network_retry_budget_used_seconds={used}\n')


if __name__ == '__main__':
    main()
