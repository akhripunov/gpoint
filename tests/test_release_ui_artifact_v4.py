import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("static_v4", ROOT / "scripts/release_ui_artifact_v4.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
SHA = "a" * 40


class StaticV4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text(f'<meta name="gpoint-build-sha" content="{SHA}">')
        (self.dist / "assets" / "a.js").write_text("console.log(1)")
        self.archive = self.root / "site.zip"

    def test_valid_build_verifies(self):
        p = ui.create_bundle(self.dist, SHA, self.archive)
        self.assertEqual(ui.verify_bundle(self.archive, SHA), p)
        self.assertEqual(sorted(p["files"]), ["assets/a.js", "index.html"])

    def test_deterministic_archive(self):
        ui.create_bundle(self.dist, SHA, self.archive)
        duplicate = self.root / "duplicate.zip"
        ui.create_bundle(self.dist, SHA, duplicate)
        self.assertEqual(self.archive.read_bytes(), duplicate.read_bytes())

    def test_no_overwrite(self):
        ui.create_bundle(self.dist, SHA, self.archive)
        with self.assertRaises(ValueError):
            ui.create_bundle(self.dist, SHA, self.archive)

    def test_wrong_sha(self):
        with self.assertRaises(ValueError):
            ui.create_bundle(self.dist, "b"*40, self.archive)

    def test_symlink_rejected(self):
        (self.dist / "assets" / "outside").symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            ui.create_bundle(self.dist, SHA, self.archive)

    def test_tampered_member(self):
        ui.create_bundle(self.dist, SHA, self.archive)
        other = self.root / "bad.zip"
        with zipfile.ZipFile(self.archive) as old, zipfile.ZipFile(other, "w") as z:
            for name in old.namelist():
                data = old.read(name)
                z.writestr(name, b"bad" if name == "assets/a.js" else data)
        with self.assertRaises(ValueError):
            ui.verify_bundle(other, SHA)

    def test_extra_member(self):
        ui.create_bundle(self.dist, SHA, self.archive)
        with zipfile.ZipFile(self.archive, "a") as z:
            z.writestr("hidden.file", b"hidden")
        with self.assertRaises(ValueError):
            ui.verify_bundle(self.archive, SHA)


if __name__ == "__main__":
    unittest.main()
