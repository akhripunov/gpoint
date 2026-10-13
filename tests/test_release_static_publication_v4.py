"""No network: V3 static supplement must be immutable and UI-only."""
import hashlib
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_assets import verified_optional_v4_static_asset
from release_ui_artifact_v4 import create_bundle

SHA = "a" * 40


class StaticPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text('<meta name="gpoint-build-sha" content="' + SHA + '">')
        (dist / "assets" / "x.js").write_text('ok')
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        static = self.bundle / "miniapp.zip"
        create_bundle(dist, SHA, static)
        self.receipt = {
            "candidateSha": SHA, "impactV3": {"classification": "UI_ONLY"},
            "staticArtifactSha256": hashlib.sha256(static.read_bytes()).hexdigest(),
            "staticArtifactName": "miniapp.zip",
            "staticArtifactRoute": "V4_ADVISORY_ONLY",
        }

    def test_valid_optional_static_supplement(self):
        self.assertEqual(verified_optional_v4_static_asset(self.receipt, self.bundle, SHA),
                         self.bundle / "miniapp.zip")

    def test_full_release_never_adds_v4_static(self):
        with self.assertRaises(RuntimeError):
            verified_optional_v4_static_asset(
                {**self.receipt, "impactV3": {"classification": "FULL"}},
                self.bundle, SHA)

    def test_wrong_digest(self):
        with self.assertRaises(RuntimeError):
            verified_optional_v4_static_asset(
                {**self.receipt, "staticArtifactSha256": "f" * 64},
                self.bundle, SHA)

    def test_wrong_candidate(self):
        with self.assertRaises(RuntimeError):
            verified_optional_v4_static_asset(
                {**self.receipt, "candidateSha": "b" * 40},
                self.bundle, SHA)

    def test_partial_identity(self):
        with self.assertRaises(RuntimeError):
            verified_optional_v4_static_asset(
                {"candidateSha": SHA, "staticArtifactName": "miniapp.zip"}, self.bundle, SHA)

    def test_missing_optional_asset_is_allowed(self):
        self.assertIsNone(verified_optional_v4_static_asset({"candidateSha": SHA}, self.bundle, SHA))

    def test_deleted_static_file(self):
        (self.bundle / "miniapp.zip").unlink()
        with self.assertRaises(RuntimeError):
            verified_optional_v4_static_asset(self.receipt, self.bundle, SHA)


if __name__ == '__main__':
    unittest.main()
