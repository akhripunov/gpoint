import sys
import unittest
import importlib.util
import os
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_ghcr_images import validate_eros_pilot_image_metadata  # noqa: E402

PREPARE_SPEC = importlib.util.spec_from_file_location("prepare_release", ROOT / "scripts" / "prepare-release.py")
PREPARE = importlib.util.module_from_spec(PREPARE_SPEC)
PREPARE_SPEC.loader.exec_module(PREPARE)
stock_eros_build_command = PREPARE.stock_eros_build_command


SHA = "a" * 40
UPSTREAM = "d1a140083321687212fb7b17c09cc0c3c9639f5e"
CONFIG = "sha256:" + "c" * 64
MANIFEST = "sha256:" + "d" * 64


class ErosPilotArtifactTests(unittest.TestCase):
    def test_stock_build_is_pinned_unmodified_and_candidate_bound(self):
        command = stock_eros_build_command(Path("/tmp/clean-eros"), SHA)
        rendered = " ".join(command)
        self.assertIn("--platform linux/amd64", rendered)
        self.assertIn(f"--file {(Path('/tmp/clean-eros') / 'docker/Dockerfile').resolve()}", rendered)
        self.assertIn("gpoint-eros-pilot:" + SHA, rendered)
        self.assertIn("org.opencontainers.image.revision=" + UPSTREAM, rendered)
        self.assertIn("org.opencontainers.image.source=https://github.com/etherfunlab/eros-engine", rendered)
        self.assertIn("com.gpoint.integration.revision=" + SHA, rendered)
        self.assertIn("com.gpoint.eros.patch-set.sha256=none", rendered)
        self.assertNotIn("patches/", rendered)

    def test_stock_build_rejects_non_exact_candidate(self):
        with self.assertRaisesRegex(ValueError, "exact"):
            stock_eros_build_command(Path("/tmp/clean-eros"), "not-a-sha")

    def test_build_cache_is_used_only_inside_github_actions(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=False):
            local = stock_eros_build_command(Path("/tmp/clean-eros"), SHA)
        self.assertNotIn("type=gha,scope=gpoint-eros-stock", local)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
            hosted = stock_eros_build_command(Path("/tmp/clean-eros"), SHA)
        self.assertIn("type=gha,scope=gpoint-eros-stock", hosted)

    def test_pilot_metadata_binds_registry_digest_candidate_and_upstream(self):
        metadata = {
            "Id": CONFIG,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Labels": {
                "org.opencontainers.image.revision": UPSTREAM,
                "org.opencontainers.image.source": "https://github.com/etherfunlab/eros-engine",
                "com.gpoint.integration.revision": SHA,
                "com.gpoint.eros.upstream-revision": UPSTREAM,
                "com.gpoint.eros.patch-set.sha256": "none",
            }},
        }
        record = validate_eros_pilot_image_metadata(MANIFEST, metadata, SHA)
        self.assertEqual(record, {
            "repository": "ghcr.io/akhripunov/gpoint-eros-pilot",
            "digest": MANIFEST,
            "configDigest": CONFIG,
            "sourceSha": SHA,
            "upstreamSha": UPSTREAM,
            "patchSetSha256": "none",
            "platform": "linux/amd64",
        })

    def test_pilot_metadata_fails_closed_on_mutable_or_untrusted_identity(self):
        metadata = {
            "Id": CONFIG,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Labels": {
                "org.opencontainers.image.revision": UPSTREAM,
                "org.opencontainers.image.source": "https://github.com/etherfunlab/eros-engine",
                "com.gpoint.integration.revision": SHA,
                "com.gpoint.eros.upstream-revision": UPSTREAM,
                "com.gpoint.eros.patch-set.sha256": "none",
            }},
        }
        for digest, changed in [
            ("latest", metadata),
            (MANIFEST, {**metadata, "Architecture": "arm64"}),
            (MANIFEST, {**metadata, "Config": {"Labels": {**metadata["Config"]["Labels"],
                                                              "com.gpoint.eros.patch-set.sha256": "modified"}}}),
        ]:
            with self.subTest(digest=digest, changed=changed):
                with self.assertRaises(ValueError):
                    validate_eros_pilot_image_metadata(digest, changed, SHA)


if __name__ == "__main__":
    unittest.main()
