"""Exact-SHA GSales config only; preserves the installed unified V3 worker.

Protected VPS credentials enter through JSON stdin. Default mode is read-only.
No request here claims a job or invokes inference; readiness uses real worker polls.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
from uuid import UUID

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home()
SUPPORT = HOME / 'Library/Application Support/GPoint/agata-codex-worker'
CONFIG = HOME / '.config/gpoint/agata-worker.env'
RELEASE_LOCK = HOME / '.config/gpoint/release-production.lock'
STATUS = SUPPORT / 'status/worker-status.json'
INFERENCE = SUPPORT / 'state/inference-process.json'
LABEL = 'one.gpoint.agata-codex-worker'
URL = 'https://gpoint.amiora.one/gsales/agata-worker'
ASSETS = ('gpoint_mac_worker.py', 'agata_worker_pool.py', 'lab_queue.py', 'lab_fixture_manifest.json', 'requirements.txt', 'run_codex_worker.sh')
TEST_TENANTS = {'c4d67889-877e-4d1b-a8d8-597c479d638f', '7508f36f-774a-48aa-8c6b-42f6f160415d'}


def command(args, timeout=5):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=REPO)
    if result.returncode:
        raise RuntimeError('protected_local_command_failed')
    return result.stdout.strip()


def private_file(path):
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError('protected_file_permissions_invalid')
    return path.read_bytes()


def verify_candidate(candidate):
    if command(['git', 'rev-parse', 'HEAD']) != candidate or command(['git', 'status', '--porcelain']):
        raise RuntimeError('candidate_checkout_not_clean_exact_sha')
    hashes = {}
    for name in ASSETS:
        source = REPO / 'services/agata/worker' / name
        installed = SUPPORT / 'runtime/current' / name
        if installed.is_symlink() or not installed.is_file():
            raise RuntimeError('installed_worker_asset_invalid')
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashlib.sha256(installed.read_bytes()).hexdigest() != expected:
            raise RuntimeError('installed_worker_candidate_hash_mismatch')
        hashes[name] = expected
    plist = plistlib.loads(private_file(HOME / 'Library/LaunchAgents' / (LABEL + '.plist')))
    runtime = SUPPORT / 'runtime/current'
    if (plist.get('Label') != LABEL or plist.get('ProgramArguments') != [str(runtime / 'run_codex_worker.sh'), 'watch']
            or plist.get('WorkingDirectory') != str(runtime) or plist.get('RunAtLoad') is not True or plist.get('KeepAlive') is not True):
        raise RuntimeError('installed_launchagent_identity_invalid')
    return hashes


def verified_pid():
    output = command(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + LABEL])
    match = re.search(r'^\s*pid = ([0-9]+)\s*$', output, re.MULTILINE)
    if not match:
        raise RuntimeError('launchagent_pid_ambiguous')
    pid = int(match.group(1))
    if pid <= 1:
        raise RuntimeError('launchagent_pid_ambiguous')
    # macOS ps prints argv joined by spaces without quoting path components.
    actual = command(['/bin/ps', '-p', str(pid), '-o', 'command='])
    runtime = SUPPORT / 'runtime/current'
    expected = ' '.join([str(runtime.resolve() / '.venv/bin/python3.12'), str(runtime.resolve() / 'gpoint_mac_worker.py'), 'watch'])
    if actual != expected:
        raise RuntimeError('launchagent_process_identity_invalid')
    return pid


def signal_pid(pid, sig):
    os.kill(pid, sig)


def read_status():
    return json.loads(private_file(STATUS))


def fresh(value, seconds=30):
    try:
        captured = datetime.fromisoformat(value.replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - captured).total_seconds()
        return 0 <= age <= seconds
    except (ValueError, AttributeError, TypeError):
        return False


def idle_status(status):
    if not isinstance(status, dict):
        return False
    lanes = status.get('targets', {})
    if not isinstance(lanes, dict):
        return False
    return all(isinstance(lane, dict) and lane.get('state') == 'idle'
               and lane.get('current_job_present') is not True and fresh(lane.get('updated_at'))
               for lane in [status, *lanes.values()])


def frozen_idle(pid):
    if INFERENCE.exists() or INFERENCE.is_symlink() or not idle_status(read_status()):
        return False
    children = subprocess.run(['/usr/bin/pgrep', '-P', str(pid)], capture_output=True, timeout=1)
    if children.returncode not in (0, 1):
        raise RuntimeError('worker_children_ambiguous')
    return children.returncode == 1 and not children.stdout.strip()


def freeze_idle(timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        pid = verified_pid()
        stopped = False
        started = time.monotonic()
        try:
            signal_pid(pid, signal.SIGSTOP)
            stopped = True
            if frozen_idle(pid) and time.monotonic() - started < 4:
                # The caller owns the frozen process until its finally resumes it.
                return pid, started
        except BaseException:
            if stopped:
                signal_pid(pid, signal.SIGCONT)
            raise
        if stopped:
            signal_pid(pid, signal.SIGCONT)
        if time.monotonic() >= deadline:
            raise RuntimeError('worker_busy_or_state_ambiguous')
        time.sleep(0.5)


def parse_env(text):
    result = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if key in result:
                raise RuntimeError('worker_config_duplicate_key')
            result[key] = value.strip().strip('\"').strip("'")
    return result


def render_config(text, payload):
    token = payload.get('GSALES_WORKER_TOKEN', '')
    tenant = payload.get('GSALES_TENANT_ID', '')
    try:
        valid = str(UUID(tenant)) == tenant and UUID(tenant).int != 0
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or tenant in TEST_TENANTS or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', token):
        raise RuntimeError('protected_sales_lane_values_invalid')
    old = parse_env(text)
    if not old.get('GPOINT_AGATA_WORKER_TOKEN') or token == old['GPOINT_AGATA_WORKER_TOKEN']:
        raise RuntimeError('lane_credentials_must_be_distinct')
    gp = old.get('GPOINT_AGATA_TENANT_ID')
    if not gp:
        sidecar = SUPPORT / 'config/tenant-bindings.json'
        if sidecar.exists():
            gp = json.loads(private_file(sidecar)).get('tenant_id')
    if gp == tenant:
        raise RuntimeError('lane_tenants_must_be_distinct')
    managed = {'GSALES_AGATA_WORKER_URL': URL, 'GSALES_AGATA_WORKER_TOKEN': token, 'GSALES_AGATA_TENANT_ID': tenant}
    lines = [line for line in text.splitlines() if line.split('=', 1)[0] not in managed]
    lines.extend(key + '=' + value for key, value in sorted(managed.items()))
    return '\n'.join(lines) + '\n'


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix='.gsales-lane-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def poll_confirmed(after, before, lane):
    current = after.get('targets', {}).get(lane, after if lane == 'GPOINT' else {})
    previous = before.get('targets', {}).get(lane, before if lane == 'GPOINT' else {})
    # V3 can retain last_error_code after a recovered empty claim. A new
    # last_poll_at is written only after the authenticated claim succeeds.
    return bool(current.get('last_poll_at') and current.get('last_poll_at') != previous.get('last_poll_at')
                and current.get('state') in {'idle', 'running'})


def wait_exit(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise RuntimeError('worker_exit_not_confirmed')


def restart():
    # KeepAlive may already have started the unchanged job; no -k force kill.
    command(['launchctl', 'kickstart', 'gui/' + str(os.getuid()) + '/' + LABEL], timeout=10)
    verified_pid()


def wait_polls(before, timeout=120, *, require_gsales=True):
    deadline = time.monotonic() + timeout
    gpoint_deadline = time.monotonic() + min(timeout, 30)
    point_seen = False
    while time.monotonic() < deadline:
        current = read_status()
        if poll_confirmed(current, before, 'GPOINT'):
            point_seen = True
        if not point_seen and time.monotonic() >= gpoint_deadline:
            raise RuntimeError('gpoint_worker_poll_not_confirmed')
        point = current.get('targets', {}).get('GPOINT', current)
        if point_seen and not fresh(point.get('last_poll_at')):
            raise RuntimeError('gpoint_worker_poll_became_stale')
        if point_seen and (not require_gsales or poll_confirmed(current, before, 'GSALES')):
            return
        time.sleep(0.5)
    raise RuntimeError('gsales_worker_poll_not_confirmed')


def require_canonical_release_lock():
    try:
        value = json.loads(private_file(RELEASE_LOCK))
        if (not isinstance(value, dict) or set(value) != {'releaseId', 'owner', 'pid', 'startedAt'}
                or not isinstance(value['releaseId'], str) or not value['releaseId'].startswith('gpoint-gsales-worker-')
                or not isinstance(value['owner'], str) or not value['owner']
                or type(value['pid']) is not int or value['pid'] != os.getppid()
                or not fresh(value['startedAt'], seconds=3600)):
            raise ValueError
    except (OSError, ValueError, RuntimeError, TypeError):
        raise RuntimeError('canonical_release_lock_required') from None


def configure(candidate, approved, payload, *, apply=False):
    if not re.fullmatch(r'[0-9a-f]{40}', candidate or '') or approved != candidate:
        raise RuntimeError('exact_sha_approval_required')
    if apply:
        require_canonical_release_lock()
    assets = verify_candidate(candidate)
    original = private_file(CONFIG).decode()
    rendered = render_config(original, payload)
    verified_pid()
    if not apply:
        return {'status': 'PLANNED', 'candidateSha': candidate, 'assets': assets, 'generationInvoked': False}
    if rendered == original:
        wait_polls(read_status(), timeout=30, require_gsales=False)
        return {'status': 'ALREADY_CONFIGURED', 'candidateSha': candidate, 'assets': assets,
                'configSha256': hashlib.sha256(CONFIG.read_bytes()).hexdigest(), 'generationInvoked': False,
                'gpointConfigPreserved': True, 'labConfigPreserved': True}
    lock = os.open(CONFIG.parent / 'unified-worker-transition.lock', os.O_CREAT | os.O_RDWR, 0o600)
    pid = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('worker_configuration_lock_busy') from None
        if private_file(CONFIG).decode() != original:
            raise RuntimeError('worker_config_changed_after_preflight')
        backup = Path(tempfile.mkdtemp(prefix=candidate + '-', dir=SUPPORT))
        backup.chmod(0o700)
        atomic_write(backup / 'worker.env', original)
        before = read_status()
        staged = backup / 'new-worker.env'
        atomic_write(staged, rendered)
        atomic_write(backup / 'manifest.json', json.dumps({'status': 'PREPARED', 'candidateSha': candidate, 'assets': assets, 'generationInvoked': False}) + '\n')
        resume_state = {'done': False}
        pid, frozen_at = freeze_idle()
        try:
            return apply_frozen(candidate, assets, original, backup, staged, before, pid, frozen_at, resume_state)
        finally:
            if not resume_state['done']:
                try:
                    signal_pid(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
    finally:
        os.close(lock)


def apply_frozen(candidate, assets, original, backup, staged, before, pid, frozen_at, resume_state):
    try:
        try:
            for name, expected in assets.items():
                installed = SUPPORT / 'runtime/current' / name
                if installed.is_symlink() or hashlib.sha256(installed.read_bytes()).hexdigest() != expected:
                    raise RuntimeError('installed_worker_asset_changed')
            if private_file(CONFIG).decode() != original or time.monotonic() - frozen_at >= 4:
                raise RuntimeError('frozen_worker_precondition_changed')
            before = read_status()
            os.replace(staged, CONFIG)
            signal_pid(pid, signal.SIGTERM)
        finally:
            if pid is not None:
                try:
                    signal_pid(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                resume_state['done'] = True
        frozen_seconds = time.monotonic() - frozen_at
        if frozen_seconds >= 5:
            raise RuntimeError('worker_freeze_budget_exceeded')
        wait_exit(pid)
        restart()
        wait_polls(before, timeout=30, require_gsales=False)
        evidence = {'status': 'CONFIGURED_GPOINT_AUTHENTICATED_GSALES_PENDING', 'candidateSha': candidate, 'assets': assets,
                    'protectedBackup': str(backup), 'frozenSeconds': frozen_seconds, 'generationInvoked': False,
                    'configSha256': hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                    'restartConfirmed': True, 'gpointConfigPreserved': True, 'labConfigPreserved': True}
        atomic_write(backup / 'manifest.json', json.dumps(evidence) + '\n')
        return evidence
    except BaseException:
        atomic_write(CONFIG, original)
        atomic_write(backup / 'manifest.json', json.dumps({'status': 'ROLLED_BACK_CONFIG', 'candidateSha': candidate,
                     'runtimeRequiresOperatorReview': True, 'generationInvoked': False}) + '\n')
        # Never force restart a new process that may already be generating.
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--owner-approved-sha', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        result = configure(args.candidate, args.owner_approved_sha, payload, apply=args.apply)
    except Exception as error:
        # Only our fixed, secret-free reason codes can reach output.
        code = str(error) if isinstance(error, RuntimeError) and re.fullmatch(r'[a-z_]+', str(error)) else 'protected_configuration_failed'
        raise SystemExit('BLOCKED: ' + code) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
