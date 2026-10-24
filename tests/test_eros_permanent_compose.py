import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PermanentErosComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = yaml.safe_load((ROOT / "infra/production/docker-compose.yml").read_text())
        cls.overlay = yaml.safe_load((ROOT / "infra/production/docker-compose.eros.yml").read_text())

    def test_overlay_extends_the_existing_api_in_synthetic_only_mode(self):
        self.assertIn("api", self.overlay["services"])
        api = self.overlay["services"]["api"]
        self.assertNotIn("env_file", api)
        self.assertEqual(api["environment"]["EROS_ENABLED"], "true")
        self.assertEqual(api["environment"]["EROS_PRODUCTION_TEST_MODE"], "true")
        self.assertEqual(api["environment"]["EROS_DATA_MODE"], "synthetic")
        self.assertIn("EROS_PRODUCTION_TEST_USER_IDS", api["environment"])
        self.assertIn("EROS_PRODUCTION_TEST_CREATOR_IDS", api["environment"])
        self.assertIn("EROS_BRIDGE_API_KEY", api["environment"])
        self.assertIn("EROS_EMBEDDING_API_KEY", api["environment"])
        gateway = (ROOT / "apps/api/src/eros-worker-gateway.ts").read_text()
        self.assertIn("bearer(headers, 'EROS_EMBEDDING_API_KEY')", gateway)
        self.assertNotIn("bearer(headers, 'GPOINT_API_KEY')", gateway)
        self.assertNotIn("command", api)
        self.assertNotIn("image", api)

    def test_overlay_adds_only_the_stock_eros_runtime_and_one_shot_migration(self):
        self.assertNotIn("eros-pilot-api", self.overlay["services"])
        self.assertNotIn("postgres", self.overlay["services"])
        self.assertIn("eros", self.overlay["services"])
        eros = self.overlay["services"]["eros"]
        self.assertNotIn("env_file", eros)
        self.assertNotIn("EROS_MIGRATOR_DATABASE_URL", eros["environment"])
        self.assertNotIn("EROS_RUNTIME_DATABASE_URL", self.overlay["services"]["api"]["environment"])
        self.assertIn("EROS_BRIDGE_API_KEY", eros["environment"])
        self.assertNotIn("EROS_WORKER_SIGNING_SECRET", eros["environment"])
        self.assertEqual(eros["restart"], "unless-stopped")
        self.assertIn("healthcheck", eros)
        self.assertIn("EROS_RUNTIME_DATABASE_URL", eros["environment"]["DATABASE_URL"])
        self.assertIn("eros-migrate", self.overlay["services"])
        self.assertIn("EROS_MIGRATOR_DATABASE_URL", self.overlay["services"]["eros-migrate"]["environment"]["DATABASE_URL"])
        self.assertNotIn("build", eros)

    def test_eros_uses_private_data_and_embedding_networks_and_existing_postgres(self):
        eros = self.overlay["services"]["eros"]
        networks = eros["networks"]
        self.assertIn("eros_data", networks)
        self.assertNotIn("shared_internal", networks)
        self.assertTrue(self.overlay["networks"]["eros_data"]["external"])
        self.assertNotIn("ports", eros)
        self.assertNotIn("postgres", self.base["services"])

    def test_existing_api_joins_eros_network_without_replacing_its_runtime(self):
        api = self.overlay["services"]["api"]
        self.assertIn("eros_api", api["networks"])
        self.assertTrue(self.overlay["networks"]["eros_api"]["internal"])
        self.assertNotIn("ports", api)
        self.assertEqual(self.base["services"]["api"]["command"], api.get("command", self.base["services"]["api"]["command"]))


if __name__ == "__main__":
    unittest.main()
