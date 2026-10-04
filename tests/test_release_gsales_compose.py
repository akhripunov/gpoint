"""Ensure the optional GSales lane does not block GPoint-only releases."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "infra/production/docker-compose.yml"
GSALES = ROOT / "infra/production/docker-compose.gsales.yml"
GSales_KEYS = (
    "GSALES_AGATA_DATABASE_URL",
    "GSALES_AGATA_TENANT_ID",
    "GSALES_AGATA_INTERNAL_API_TOKEN",
    "GSALES_AGATA_LLM_PROVIDER",
    "GSALES_AGATA_MESSAGE_AEAD_KEY",
    "GSALES_AGATA_REQUEST_FINGERPRINT_HMAC_KEY",
    "GSALES_AGATA_RETRIEVAL_QUERY_HMAC_KEY",
    "GSALES_AGATA_SHADOW_CANDIDATE_HMAC_KEY",
    "GSALES_AGATA_RESPONSE_FINGERPRINT_HMAC_KEY",
)


class GSalesComposeProfileTests(unittest.TestCase):
    def compose(self, files, missing_key=None, production_mode=None):
        env = os.environ.copy()
        env["RELEASE_SHA"] = "a" * 40
        for key in GSales_KEYS:
            env.pop(key, None)
        if missing_key:
            # Compose-only fixtures: no container is started and no value is persisted.
            for key in GSales_KEYS:
                if key != missing_key:
                    env[key] = "compose-fixture-only"
        elif production_mode is not None:
            # Compose-only fixtures: no container is started and no value is persisted.
            for key in GSales_KEYS:
                env[key] = "compose-fixture-only"
        if production_mode is not None:
            env["GSALES_PRODUCTION_MODE"] = "true" if production_mode else "false"
        with tempfile.TemporaryDirectory(prefix="gpoint-compose-profile-") as directory:
            scratch = Path(directory)
            runtime_env = scratch / "production.env"
            runtime_env.write_text("")
            rendered_files = []
            for index, source in enumerate(files):
                target = scratch / f"compose-{index}.yml"
                content = source.read_text().replace("/opt/gpoint/env/production.env", str(runtime_env))
                target.write_text(content)
                rendered_files.append(target)
            profiles = ["--profile", "gsales"] if GSALES in files else []
            return subprocess.run(
                ["docker", "compose", "--env-file", "/dev/null", *profiles, *sum((["-f", str(path)] for path in rendered_files), []), "config", "--format", "json"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_gpoint_config_renders_without_gsales_lane_secrets(self):
        result = self.compose([BASE])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gsales_overlay_still_requires_owner_provisioned_settings(self):
        result = self.compose([BASE, GSALES], missing_key="GSALES_AGATA_DATABASE_URL")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GSALES_AGATA_DATABASE_URL", result.stderr)

    def test_gsales_overlay_requires_each_lane_setting(self):
        for missing in GSales_KEYS:
            with self.subTest(missing=missing):
                result = self.compose([BASE, GSALES], missing_key=missing)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)

    def test_production_mode_enables_core_drafts_and_provider_readiness(self):
        result = self.compose([BASE, GSALES], production_mode=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        services = json.loads(result.stdout)["services"]
        for name in ("gsales-agata-api", "gsales-agata-memory-worker"):
            self.assertEqual(services[name]["environment"]["AGATA_SALES_DRAFTS_ENABLED"], "true")
            self.assertEqual(services[name]["environment"]["AGATA_SALES_PRODUCTION_MODE"], "true")
        self.assertEqual(services["gsales-api"]["environment"]["GSALES_AGATA_API_TOKEN"], "compose-fixture-only")


if __name__ == "__main__":
    unittest.main()
