#!/usr/bin/env python3
"""Select only the newest successful preparation of the current main SHA."""
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_assets import resolve_asset, find_prepare_evidence
from network_retry import NetworkUnavailable, RetryBudget, retry_network_read

REPO = 'akhripunov/gpoint'
RECOVERY_CANDIDATE_SHA = 'd367c2ec24242a733dbb2c4145c91d2d4682ba2c'
RECOVERY_FAILURE_RUN_ID = '39010750184'
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
    if ref != 'refs/heads/main' or mode not in ('preflight', 'production', 'task-scoped-auto', 'recovery-finalization', 'recovery-diagnostics'):
        raise RuntimeError('Select main and a valid release mode')
    if mode in ('production', 'recovery-finalization') and approval != 'true':
        raise RuntimeError('Explicit exact-candidate production approval is required')
    if mode == 'recovery-diagnostics' and approval != 'false':
        raise RuntimeError('Read-only recovery diagnostics cannot accept production approval')
    if mode == 'task-scoped-auto' and approval != 'false':
        raise RuntimeError('TASK_SCOPED_AUTO uses its durable authorization record, not the exact-SHA approval input')


def validate_recovery_finalization_inputs(sha, environment):
    baseline = environment.get('RECOVERY_DB_RISK_BASELINE_SHA', '')
    decision_id = environment.get('RECOVERY_DB_RISK_DECISION_ID', '')
    if (environment.get('RECOVERY_DB_RISK_ACCEPTED') != 'true'
            or environment.get('GITHUB_ACTOR', '').lower() != 'akhripunov'
            or not re.fullmatch('[a-f0-9]{40}', baseline)
            or not decision_id or len(decision_id) < 8
            or environment.get('CREATE_RECOVERY_BACKUP') != 'true'
            or environment.get('RECOVERY_FAILURE_RUN_ID') != '39010750184'
            or environment.get('RECOVERY_FAILURE_RUN_ATTEMPT') != '1'):
        raise RuntimeError('Recovery finalization requires OWNER dispatch, exact incident, DB-risk decision, and current backup authorization')
    return {'candidate_sha': sha, 'baseline_sha': baseline, 'decision_id': decision_id}


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


def validate_recovery_executor(candidate_sha, executor_sha, current_main_sha, repo_root):
    if candidate_sha != RECOVERY_CANDIDATE_SHA:
        raise RuntimeError('Recovery mode is pinned to the original ME-38 incident candidate')
    if not re.fullmatch('[a-f0-9]{40}', executor_sha or '') or current_main_sha != executor_sha:
        raise RuntimeError('Recovery executor is not the exact current main SHA')
    ancestry = subprocess.run(['git', 'merge-base', '--is-ancestor', candidate_sha, executor_sha],
                              cwd=repo_root, capture_output=True, timeout=15)
    if ancestry.returncode != 0:
        raise RuntimeError('Recovery candidate is not an ancestor of the exact current main executor')


def original_prepare_identity(candidate_sha, repo_root, environment):
    """Bind the selected bundle to the Prepare run named by original immutable failure evidence."""
    with tempfile.TemporaryDirectory(prefix='gpoint-recovery-selector-') as temporary:
        output = Path(temporary) / 'failure.json'
        command = ['python3', 'scripts/release_assets.py', 'read-recovery-failure', candidate_sha,
                   RECOVERY_FAILURE_RUN_ID, '1', str(output)]
        result = subprocess.run(command, cwd=repo_root, env=environment, capture_output=True,
                                text=True, timeout=180)
        if result.returncode != 0 or not output.is_file():
            raise RuntimeError('Original immutable recovery failure evidence could not be verified')
        failure = json.loads(output.read_text())
    prepare = failure.get('prepare') or {}
    run_id, attempt = str(prepare.get('runId', '')), str(prepare.get('attempt', ''))
    if not run_id.isdigit() or not attempt.isdigit() or prepare.get('candidateSha') != candidate_sha:
        raise RuntimeError('Original immutable failure does not bind an exact Prepare run')
    return run_id, attempt


def validate_ready_preflight(sha, selected_prepare, deploy_runs, read_result):
    runs = [r for r in deploy_runs if r.get('head_sha') == sha and r.get('head_branch') == 'main'
            and r.get('event') == 'workflow_dispatch'
            and str(r.get('display_title', '')).startswith(f'GPoint preflight {sha} by ')]
    if not runs:
        raise RuntimeError('Production requires a successful READY_FOR_OWNER preflight for this exact SHA')
    latest = max(runs, key=lambda r: (r.get('run_number', 0), r.get('run_attempt', 0)))
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise RuntimeError('Latest exact-SHA Deploy preflight is not successful')
    run_id, attempt = str(latest.get('id', '')), str(latest.get('run_attempt', 1))
    if not run_id.isdigit() or not attempt.isdigit():
        raise RuntimeError('READY preflight run identity is invalid')
    result = read_result(sha, run_id, attempt)
    if (result.get('candidateSha') != sha
            or str(result.get('automationRunId')) != run_id
            or str(result.get('automationRunAttempt')) != attempt
            or result.get('mode') != 'preflight'
            or result.get('preflight') != 'PASS'
            or result.get('releaseState') != 'READY_FOR_OWNER'
            or result.get('status') != 'PLANNED'
            or result.get('productionChanged') is not False
            or result.get('dryRun') is not True
            or result.get('applicationVerification') != 'NOT_DEPLOYED'
            or result.get('authorizationMode') != 'EXACT_SHA'
            or result.get('authorizationConsumed') is not False
            or str(result.get('prepareRunId')) != str(selected_prepare['id'])
            or str(result.get('prepareRunAttempt', 1)) != str(selected_prepare.get('run_attempt', 1))):
        raise RuntimeError('Immutable Deploy evidence does not prove READY_FOR_OWNER for this exact Prepare run')
    return latest


def main():
    sha = os.environ['CANDIDATE_SHA']
    orchestrator_run_id = os.environ.get('ORCHESTRATOR_RUN_ID', '').strip()
    if orchestrator_run_id and not orchestrator_run_id.isdigit():
        raise RuntimeError('Automatic orchestrator run ID is invalid')
    validate_request(sha, os.environ.get('GITHUB_REF'), os.environ.get('RELEASE_MODE'), os.environ.get('APPROVE_PRODUCTION'))
    recovery_mode = os.environ.get('RELEASE_MODE') in ('recovery-finalization', 'recovery-diagnostics')
    if os.environ.get('RELEASE_MODE') == 'recovery-finalization':
        validate_recovery_finalization_inputs(sha, os.environ)
    current_main_sha = github('git/ref/heads/main')['object']['sha']
    if recovery_mode:
        if os.environ.get('RECOVERY_FAILURE_RUN_ID') != RECOVERY_FAILURE_RUN_ID:
            raise RuntimeError('Recovery mode is pinned to the original ME-38 incident run')
        repo_root = Path(__file__).resolve().parents[1]
        executor_sha = os.environ.get('GITHUB_SHA', '')
        validate_recovery_executor(sha, executor_sha, current_main_sha, repo_root)
        failed = github(f'actions/runs/{RECOVERY_FAILURE_RUN_ID}')
        if (failed.get('head_sha') != sha or failed.get('head_branch') != 'main'
                or failed.get('event') not in ('workflow_dispatch', 'push')
                or failed.get('status') != 'completed' or failed.get('conclusion') != 'failure'
                or str(failed.get('run_attempt', 1)) != '1'):
            raise RuntimeError('Original immutable post-switch failure run identity is unavailable')
        original_prepare_id, original_prepare_attempt = original_prepare_identity(sha, repo_root, os.environ)
    else:
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
    executed_attempt, asset = find_prepare_evidence(
        sha, str(selected['id']), str(selected.get('run_attempt', 1)), github)
    # Preserve GitHub's actual workflow run attempt for audit while pinning
    # the immutable bundle to the attempt which executed Prepare.
    selected = {**selected, 'run_attempt': int(executed_attempt)}
    expected_run = os.environ.get('PREPARE_RUN_ID')
    expected_attempt = os.environ.get('PREPARE_RUN_ATTEMPT')
    if recovery_mode:
        expected_run, expected_attempt = original_prepare_id, original_prepare_attempt
    validate_expected_run(selected, expected_run, expected_attempt)
    # Production/task-scoped Deploy performs the read-only preflight on the Mac
    # immediately before apply. A separate diagnostic preflight is optional.
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f"run_id={selected['id']}\nrun_attempt={selected.get('run_attempt', 1)}\nartifact_id={asset['id']}\ncandidate={sha}\n")
        output.write(f'orchestrator_run_id={orchestrator_run_id}\n')
        output.write(f'task_authorization_id={authorization_id}\n')
        state = os.environ.get('NETWORK_RETRY_BUDGET_STATE')
        used = Path(state).read_text().strip() if state and Path(state).exists() else '0'
        output.write(f'network_retry_budget_used_seconds={used}\n')


if __name__ == '__main__':
    main()
