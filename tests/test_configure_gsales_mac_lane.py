"""No production process or credential is used by these controller tests."""
import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import time
import plistlib
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/configure-gsales-mac-lane.py'
SHA = 'a' * 40
TENANT = '23d6aafb-53da-4b5b-b70f-e345ce3aaf17'


class LaneControllerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'GSales exact-SHA Mac lane controller is missing')
        spec = importlib.util.spec_from_file_location('mac_lane', SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mod.CONFIG = root / 'worker.env'
        self.mod.SUPPORT = root
        self.mod.STATUS = root / 'worker-status.json'
        self.mod.INFERENCE = root / 'inference-process.json'
        self.mod.RELEASE_LOCK = root / 'release-production.lock'
        self.mod.RELEASE_LOCK.write_text(json.dumps({'releaseId': 'gpoint-gsales-worker-fixture', 'owner': 'fixture-owner', 'pid': self.mod.os.getppid(), 'startedAt': datetime.now(timezone.utc).isoformat()}))
        self.mod.RELEASE_LOCK.chmod(0o600)
        self.mod.CONFIG.write_text('# existing\nGPOINT_AGATA_WORKER_TOKEN=point-secret\nAGATA_LAB_WORKER_ENABLED=true\n')
        self.mod.CONFIG.chmod(0o600)
        self.payload = {'GSALES_WORKER_TOKEN': 'sales-secret', 'GSALES_TENANT_ID': TENANT}

    def test_dry_run_has_no_signals_or_config_write(self):
        before = self.mod.CONFIG.read_bytes()
        with patch.object(self.mod, 'verify_candidate', return_value={}), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod.os, 'kill') as kill:
            result = self.mod.configure(SHA, SHA, self.payload, apply=False)
        self.assertEqual(result['status'], 'PLANNED')
        self.assertEqual(self.mod.CONFIG.read_bytes(), before)
        kill.assert_not_called()

    def test_wrong_approval_or_reused_secret_fails_before_write(self):
        before = self.mod.CONFIG.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'exact_sha_approval'):
            self.mod.configure(SHA, 'b' * 40, self.payload, apply=True)
        with patch.object(self.mod, 'verify_candidate', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'lane_credentials_must_be_distinct'):
                self.mod.configure(SHA, SHA, {**self.payload, 'GSALES_WORKER_TOKEN': 'point-secret'}, apply=True)
        self.assertEqual(self.mod.CONFIG.read_bytes(), before)

    def test_render_preserves_gpoint_lab_and_replaces_sales_only(self):
        output = self.mod.render_config(self.mod.CONFIG.read_text(), self.payload)
        self.assertIn('GPOINT_AGATA_WORKER_TOKEN=point-secret\n', output)
        self.assertIn('AGATA_LAB_WORKER_ENABLED=true\n', output)
        self.assertIn('GSALES_AGATA_WORKER_URL=https://gpoint.amiora.one/gsales/agata-worker\n', output)
        self.assertIn('GSALES_AGATA_TENANT_ID=' + TENANT + '\n', output)
        self.assertEqual(output.count('GSALES_AGATA_WORKER_TOKEN='), 1)

    def test_busy_or_stale_status_is_not_safe_to_stop(self):
        now = datetime.now(timezone.utc).isoformat()
        idle = {'state': 'idle', 'updated_at': now, 'last_poll_at': now}
        self.assertTrue(self.mod.idle_status(idle))
        for data in ({**idle, 'current_job_present': True}, {**idle, 'updated_at': '2000-01-01T00:00:00Z'}, {**idle, 'targets': {'LAB': {**idle, 'state': 'running'}}}):
            self.assertFalse(self.mod.idle_status(data))

    def test_frozen_worker_is_resumed_when_busy_or_exception_occurs(self):
        for failure in (False, RuntimeError('fixture')):
            with patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'frozen_idle', side_effect=[failure] if isinstance(failure, Exception) else None, return_value=failure), patch.object(self.mod, 'signal_pid') as signal:
                with self.assertRaises(RuntimeError):
                    self.mod.freeze_idle(timeout=0)
                self.assertEqual([call.args[1] for call in signal.call_args_list], [self.mod.signal.SIGSTOP, self.mod.signal.SIGCONT])

    def test_atomic_config_is_private(self):
        self.mod.atomic_write(self.mod.CONFIG, 'fixture\n')
        self.assertEqual(self.mod.CONFIG.read_text(), 'fixture\n')
        self.assertEqual(self.mod.CONFIG.stat().st_mode & 0o777, 0o600)

    def test_readiness_requires_new_authenticated_poll_for_both_lanes(self):
        old = {'last_poll_at': 'old'}
        fresh = {'last_poll_at': 'new', 'state': 'idle', 'last_error_code': None}
        before = {'targets': {'GPOINT': old, 'GSALES': old}}
        after = {'targets': {'GPOINT': fresh, 'GSALES': old}}
        self.assertFalse(self.mod.poll_confirmed(after, before, 'GSALES'))
        self.assertTrue(self.mod.poll_confirmed(after, before, 'GPOINT'))
        after['targets']['GSALES'] = fresh
        self.assertTrue(self.mod.poll_confirmed(after, before, 'GSALES'))

    def test_apply_resumes_before_restart_and_proves_gpoint_only(self):
        original = self.mod.CONFIG.read_text()
        with patch.object(self.mod, 'verify_candidate', return_value={}), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', return_value={}), patch.object(self.mod, 'freeze_idle', return_value=(123, time.monotonic())), patch.object(self.mod, 'signal_pid') as signals, patch.object(self.mod, 'wait_exit'), patch.object(self.mod, 'restart'), patch.object(self.mod, 'wait_polls') as polls:
            result = self.mod.configure(SHA, SHA, self.payload, apply=True)
        self.assertEqual(result['status'], 'CONFIGURED_GPOINT_AUTHENTICATED_GSALES_PENDING')
        self.assertEqual([call.args[1] for call in signals.call_args_list], [self.mod.signal.SIGTERM, self.mod.signal.SIGCONT])
        self.assertEqual(polls.call_args.kwargs['require_gsales'], False)
        self.assertLess(result['frozenSeconds'], 5)
        backup = Path(result['protectedBackup'])
        self.assertEqual((backup / 'worker.env').read_text(), original)
        self.assertEqual((backup / 'worker.env').stat().st_mode & 0o777, 0o600)
        self.assertIn('AGATA_LAB_WORKER_ENABLED=true', self.mod.CONFIG.read_text())

    def test_immediate_restart_failure_restores_private_config_without_force_restart(self):
        original = self.mod.CONFIG.read_text()
        with patch.object(self.mod, 'verify_candidate', return_value={}), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', return_value={}), patch.object(self.mod, 'freeze_idle', return_value=(123, time.monotonic())), patch.object(self.mod, 'signal_pid'), patch.object(self.mod, 'wait_exit'), patch.object(self.mod, 'restart', side_effect=RuntimeError('fixture')) as restart:
            with self.assertRaises(RuntimeError):
                self.mod.configure(SHA, SHA, self.payload, apply=True)
        self.assertEqual(self.mod.CONFIG.read_text(), original)
        self.assertEqual(self.mod.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertEqual(restart.call_count, 1)
        manifest = next(self.mod.SUPPORT.glob(SHA + '-*/manifest.json'))
        self.assertEqual(json.loads(manifest.read_text())['status'], 'ROLLED_BACK_CONFIG')

    def test_recorded_inference_refuses_restart_without_inspecting_children(self):
        self.mod.INFERENCE.write_text('{}')
        with patch.object(self.mod.subprocess, 'run') as command:
            self.assertFalse(self.mod.frozen_idle(123))
        command.assert_not_called()

    def test_source_and_helper_parity_are_checked_against_candidate(self):
        self.mod.REPO = self.mod.SUPPORT / 'repo'
        self.mod.HOME = self.mod.SUPPORT / 'home'
        sources = self.mod.REPO / 'services/agata/worker'
        sources.mkdir(parents=True)
        for name in self.mod.ASSETS:
            (sources / name).write_text('candidate fixture')
            (self.mod.SUPPORT / name).write_text('candidate fixture')
        plist_path = self.mod.HOME / 'Library/LaunchAgents' / (self.mod.LABEL + '.plist')
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(plistlib.dumps({'Label': self.mod.LABEL, 'ProgramArguments': [str(self.mod.SUPPORT / 'run_codex_worker.sh'), 'watch'], 'WorkingDirectory': str(self.mod.SUPPORT), 'RunAtLoad': True, 'KeepAlive': True}))
        plist_path.chmod(0o600)
        with patch.object(self.mod, 'command', side_effect=[SHA, '']):
            self.assertEqual(len(self.mod.verify_candidate(SHA)), 4)
        (self.mod.SUPPORT / 'lab_queue.py').write_text('foreign source')
        with patch.object(self.mod, 'command', side_effect=[SHA, '']):
            with self.assertRaisesRegex(RuntimeError, 'installed_worker_candidate_hash_mismatch'):
                self.mod.verify_candidate(SHA)

    def test_startup_gpoint_poll_does_not_wait_for_gsales_route(self):
        now = datetime.now(timezone.utc).isoformat()
        current = {'targets': {'GPOINT': {'state': 'idle', 'last_poll_at': now}, 'GSALES': {'state': 'error'}}}
        with patch.object(self.mod, 'read_status', return_value=current), patch.object(self.mod.time, 'sleep') as sleep:
            self.mod.wait_polls({}, timeout=1, require_gsales=False)
        sleep.assert_not_called()

    def test_source_change_before_frozen_commit_blocks_config_mutation(self):
        helper = self.mod.SUPPORT / 'lab_queue.py'
        helper.write_text('approved')
        assets = {'lab_queue.py': hashlib.sha256(helper.read_bytes()).hexdigest()}
        original = self.mod.CONFIG.read_text()
        def freeze():
            helper.write_text('changed')
            return 123, time.monotonic()
        with patch.object(self.mod, 'verify_candidate', return_value=assets), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', return_value={}), patch.object(self.mod, 'freeze_idle', side_effect=freeze), patch.object(self.mod, 'signal_pid'), patch.object(self.mod, 'wait_exit'), patch.object(self.mod, 'restart'), patch.object(self.mod, 'wait_polls'):
            with self.assertRaisesRegex(RuntimeError, 'installed_worker_asset_changed'):
                self.mod.configure(SHA, SHA, self.payload, apply=True)
        self.assertEqual(self.mod.CONFIG.read_text(), original)

    def test_readiness_baseline_is_captured_while_worker_is_frozen(self):
        old = {'last_poll_at': 'before-idle-wait'}
        frozen = {'last_poll_at': 'final-before-restart'}
        with patch.object(self.mod, 'verify_candidate', return_value={}), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', side_effect=[old, frozen]), patch.object(self.mod, 'freeze_idle', return_value=(123, time.monotonic())), patch.object(self.mod, 'signal_pid'), patch.object(self.mod, 'wait_exit'), patch.object(self.mod, 'restart'), patch.object(self.mod, 'wait_polls') as polls:
            self.mod.configure(SHA, SHA, self.payload, apply=True)
        self.assertEqual(polls.call_args.args[0], frozen)

    def test_already_configured_retry_does_not_freeze_restart_or_write(self):
        rendered = self.mod.render_config(self.mod.CONFIG.read_text(), self.payload)
        self.mod.CONFIG.write_text(rendered)
        before = self.mod.CONFIG.stat()
        with patch.object(self.mod, 'verify_candidate', return_value={}), patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', return_value={}), patch.object(self.mod, 'wait_polls') as polls, patch.object(self.mod, 'freeze_idle', return_value=(123, time.monotonic())) as freeze, patch.object(self.mod, 'wait_exit'), patch.object(self.mod, 'restart') as restart, patch.object(self.mod, 'signal_pid') as signals:
            result = self.mod.configure(SHA, SHA, self.payload, apply=True)
        self.assertEqual(result['status'], 'ALREADY_CONFIGURED')
        freeze.assert_not_called()
        restart.assert_not_called()
        signals.assert_not_called()
        self.assertEqual(self.mod.CONFIG.stat().st_ino, before.st_ino)
        self.assertEqual(self.mod.CONFIG.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(polls.call_args.kwargs['require_gsales'], False)

    def test_apply_requires_private_release_lock_owned_by_direct_parent(self):
        for variant in ('missing', 'foreign', 'public', 'symlink'):
            lock = self.mod.RELEASE_LOCK
            lock.unlink(missing_ok=True)
            if variant != 'missing':
                lock.write_text(json.dumps({'releaseId': 'gpoint-gsales-worker-fixture', 'owner': 'fixture-owner', 'pid': self.mod.os.getppid() + (1 if variant == 'foreign' else 0), 'startedAt': datetime.now(timezone.utc).isoformat()}))
                lock.chmod(0o644 if variant == 'public' else 0o600)
                if variant == 'symlink':
                    actual = lock.with_suffix('.actual')
                    lock.rename(actual)
                    lock.symlink_to(actual)
            with patch.object(self.mod, 'verify_candidate', return_value={}) as candidate, patch.object(self.mod, 'verified_pid', return_value=123), patch.object(self.mod, 'read_status', return_value={}), patch.object(self.mod, 'freeze_idle', side_effect=RuntimeError('unsafe_standalone_apply_reached_freeze')), patch.object(self.mod, 'signal_pid') as signals:
                with self.assertRaisesRegex(RuntimeError, 'canonical_release_lock_required'):
                    self.mod.configure(SHA, SHA, self.payload, apply=True)
            candidate.assert_not_called()
            signals.assert_not_called()

    def test_launchagent_pid_verification_handles_canonical_support_path_spaces(self):
        self.mod.SUPPORT = self.mod.SUPPORT / 'Application Support'
        command_line = str(self.mod.SUPPORT / '.venv/bin/python3.12') + ' ' + str(self.mod.SUPPORT / 'gpoint_mac_worker.py') + ' watch'
        with patch.object(self.mod, 'command', side_effect=['pid = 123', command_line]):
            try:
                actual = self.mod.verified_pid()
            except RuntimeError:
                actual = None
        self.assertEqual(actual, 123)


if __name__ == '__main__':
    unittest.main()
