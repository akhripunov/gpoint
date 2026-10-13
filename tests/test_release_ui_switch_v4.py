import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_ui_artifact_v4 import create_bundle
from release_ui_switch_v4 import stage, activate, rollback

A, B = "a"*40, "b"*40


def make_archive(directory: Path, sha: str):
    dist = directory / ("dist-" + sha[:1])
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f'<meta name="gpoint-build-sha" content="{sha}">')
    (dist / "assets/app.js").write_text("window.v='" + sha + "'")
    bundle = directory / (sha + ".zip")
    create_bundle(dist, sha, bundle)
    return bundle


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.mkdir(exist_ok=True)
        self.app = self.root / "app"
        self.app.mkdir()

    def test_stage_switch_and_rollback(self):
        old = stage(self.root, make_archive(self.root, A), A)
        new = stage(self.root, make_archive(self.root, B), B)
        live = self.app / "miniapp"
        live.symlink_to(old, target_is_directory=True)
        previous = activate(self.root, live, new)
        self.assertEqual(previous, old)
        self.assertEqual(live.resolve(), new)
        self.assertIn(B, (live / "index.html").read_text())
        rollback(self.root, live, new, old)
        self.assertEqual(live.resolve(), old)
        self.assertIn(A, (live / "index.html").read_text())

    def test_refuse_replacement_of_real_live_directory(self):
        target = stage(self.root, make_archive(self.root, A), A)
        live = self.app / "miniapp"
        live.mkdir()
        with self.assertRaises(ValueError):
            activate(self.root, live, target)
        self.assertTrue(live.is_dir())

    def test_immutable_target_must_not_exist(self):
        bundle = make_archive(self.root, A)
        stage(self.root, bundle, A)
        with self.assertRaises(ValueError):
            stage(self.root, bundle, A)

    def test_reject_concurrent_runtime_switch(self):
        a = stage(self.root, make_archive(self.root, A), A)
        b = stage(self.root, make_archive(self.root, B), B)
        live = self.app / "miniapp"
        live.symlink_to(a, target_is_directory=True)
        with self.assertRaises(ValueError):
            rollback(self.root, live, b, a)

    def test_refuse_external_or_missing_target(self):
        a = stage(self.root, make_archive(self.root, A), A)
        live = self.app / "miniapp"
        live.symlink_to(a, target_is_directory=True)
        with self.assertRaises(ValueError):
            activate(self.root, live, self.root / "app")
        self.assertEqual(live.resolve(), a)

if __name__ == "__main__":
    unittest.main()
