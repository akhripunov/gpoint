from __future__ import annotations

import hashlib
import plistlib
import tempfile
import unittest
from pathlib import Path

from scripts.agata import lab_launch_agent


LABEL = "one.gpoint.agata-codex-worker"


class InMemoryLifecycleController:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def replace_plist_from_factory(self, **kwargs: object) -> None:
        self.calls += 1
        path = Path(kwargs["plist_path"])
        current = path.read_bytes()
        candidate = kwargs["candidate_factory"](current)
        before_replace = kwargs.get("before_replace")
        if before_replace:
            before_replace(current)
        if self.fail:
            raise RuntimeError("simulated lifecycle transition failure")
        path.write_bytes(candidate)
        after_replace = kwargs.get("after_replace")
        if after_replace:
            after_replace()


class PermanentLabLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plist_path = self.root / f"{LABEL}.plist"
        self.state_path = self.root / "lab-enabled-original.plist"
        self.original = plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": ["/trusted/worker", "watch"],
            "EnvironmentVariables": {"PATH": "/usr/bin"},
        }, sort_keys=True)
        self.plist_path.write_bytes(self.original)
        self.plist_path.chmod(0o600)
        self.bundle_dir = self.root / "bundle"
        self.bundle_dir.mkdir()
        self.bundle_files = []
        digest = hashlib.sha256()
        for name in ("gpoint_mac_worker.py", "lab_queue.py", "lab_fixture_manifest.json"):
            path = self.bundle_dir / name
            path.write_text(name, encoding="utf-8")
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            self.bundle_files.append((name, path))
        self.bundle_sha = digest.hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enable(self, controller: InMemoryLifecycleController) -> None:
        lab_launch_agent.enable_lab(
            controller=controller,
            plist_path=self.plist_path,
            state_path=self.state_path,
            domain="gui/501",
            label=LABEL,
            expected_bundle_sha256=self.bundle_sha,
            installed_bundle_files=tuple(self.bundle_files),
        )

    def test_enable_persists_lab_flag_without_an_approval_file(self) -> None:
        controller = InMemoryLifecycleController()
        self.enable(controller)

        enabled = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(enabled["EnvironmentVariables"]["AGATA_LAB_WORKER_ENABLED"], "true")
        self.assertEqual(enabled["ProgramArguments"], ["/trusted/worker", "watch"])
        self.assertEqual(self.state_path.read_bytes(), self.original)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(controller.calls, 1)

    def test_enable_refuses_worker_bundle_mismatch_before_mutation(self) -> None:
        controller = InMemoryLifecycleController()
        with self.assertRaisesRegex(RuntimeError, "lab_worker_bundle_mismatch"):
            lab_launch_agent.enable_lab(
                controller=controller,
                plist_path=self.plist_path,
                state_path=self.state_path,
                domain="gui/501",
                label=LABEL,
                expected_bundle_sha256="0" * 64,
                installed_bundle_files=tuple(self.bundle_files),
            )
        self.assertEqual(self.plist_path.read_bytes(), self.original)
        self.assertFalse(self.state_path.exists())

    def test_failed_transition_cleans_only_its_snapshot(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "simulated lifecycle transition failure"):
            self.enable(InMemoryLifecycleController(fail=True))
        self.assertEqual(self.plist_path.read_bytes(), self.original)
        self.assertFalse(self.state_path.exists())

    def test_disable_restores_original_plist(self) -> None:
        controller = InMemoryLifecycleController()
        self.enable(controller)
        lab_launch_agent.disable_lab(
            controller=controller,
            plist_path=self.plist_path,
            state_path=self.state_path,
            domain="gui/501",
            label=LABEL,
        )
        self.assertEqual(self.plist_path.read_bytes(), self.original)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(controller.calls, 2)

    def test_disable_refuses_to_overwrite_a_changed_plist(self) -> None:
        controller = InMemoryLifecycleController()
        self.enable(controller)
        changed = {
            "Label": LABEL,
            "ProgramArguments": ["/newer/v3-worker", "watch"],
            "EnvironmentVariables": {"PATH": "/usr/bin", "AGATA_LAB_WORKER_ENABLED": "true"},
        }
        changed_bytes = plistlib.dumps(changed, sort_keys=True)
        self.plist_path.write_bytes(changed_bytes)
        with self.assertRaisesRegex(RuntimeError, "lab_worker_plist_changed_since_enable"):
            lab_launch_agent.disable_lab(
                controller=controller,
                plist_path=self.plist_path,
                state_path=self.state_path,
                domain="gui/501",
                label=LABEL,
            )
        self.assertEqual(self.plist_path.read_bytes(), changed_bytes)
        self.assertTrue(self.state_path.exists())
        self.assertEqual(controller.calls, 2)


if __name__ == "__main__":
    unittest.main()
