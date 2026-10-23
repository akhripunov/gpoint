import unittest
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "infra/production/docker-compose.eros-pilot.yml"


class ErosVpsPilotComposeTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(COMPOSE.read_text())
        self.services = self.compose["services"]

    def test_pilot_services_are_opt_in_and_not_publicly_published(self):
        for name in ("eros-pilot-api", "eros", "eros-migrate", "postgres", "ollama", "ollama-model-pull", "ollama-model-verify"):
            with self.subTest(service=name):
                service = self.services[name]
                self.assertIn("eros-pilot", service.get("profiles", []))
                self.assertNotIn("ports", service)

    def test_eros_database_is_dedicated_to_pilot(self):
        postgres = self.services["postgres"]
        self.assertEqual(postgres["environment"]["POSTGRES_DB"], "eros_pilot")
        self.assertIn("eros_pilot_pgdata:/var/lib/postgresql/data", postgres["volumes"])
        self.assertEqual(postgres["networks"], ["eros_pilot_data"])
        self.assertNotIn("gpoint_app", postgres["networks"])
        self.assertEqual(postgres["mem_limit"], "256m")
        init_script = ROOT / "infra/production/eros-pilot-init.sh"
        init_text = init_script.read_text()
        self.assertTrue(init_script.stat().st_mode & 0o111)
        self.assertIn("GRANT CONNECT, CREATE ON DATABASE eros_pilot TO eros_migrator", init_text)
        self.assertNotIn("ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA public", init_text)
        self.assertIn("\\getenv eros_migrator_password EROS_PILOT_MIGRATOR_PASSWORD", init_text)
        self.assertNotIn("--set=eros_migrator_password", init_text)
        self.assertEqual(self.services["eros"]["depends_on"]["eros-migrate"]["condition"],
                         "service_completed_successfully")
        self.assertEqual(self.services["eros"]["depends_on"]["ollama"]["condition"],
                         "service_healthy")

    def test_postgres_keeps_only_capabilities_needed_by_upstream_entrypoint(self):
        postgres = self.services["postgres"]
        self.assertEqual(set(postgres["cap_drop"]), {"ALL"})
        self.assertEqual(
            set(postgres["cap_add"]),
            {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
        )

    def test_embeddings_and_chat_are_routed_through_the_existing_api(self):
        eros = self.services["eros"]
        model_config = (ROOT / "services/eros-engine/eros-pilot-model-config.toml").read_text()
        self.assertIn("http://eros-pilot-api:3000/eros-worker/v1/chat/completions", model_config)
        self.assertIn("http://eros-pilot-api:3000/eros-worker/v1/embeddings", model_config)
        self.assertEqual(self.services["eros-pilot-api"]["environment"]["EROS_OLLAMA_EMBEDDING_URL"], "http://ollama:11434/api/embed")
        self.assertIn("EROS_PRODUCTION_TEST_USER_IDS", self.services["eros-pilot-api"]["environment"])
        self.assertIn("EROS_PRODUCTION_TEST_CREATOR_IDS", self.services["eros-pilot-api"]["environment"])
        self.assertIn("EROS_PILOT_OLLAMA_MODEL_DIGEST", self.services["ollama-model-verify"]["environment"])
        self.assertEqual(self.services["ollama"]["mem_limit"], "1536m")
        self.assertIn("@sha256:352e045b937ac29d3d9550c22fb85525f60a89e064df34c26579bee5a93b3a16",
                      self.services["ollama"]["image"])

    def test_pilot_network_is_internal_and_only_api_bridges_to_gpoint(self):
        networks = self.compose["networks"]
        self.assertTrue(networks["eros_pilot_app"]["internal"])
        self.assertTrue(networks["eros_pilot_data"]["internal"])
        self.assertEqual(self.services["eros-pilot-api"]["networks"], ["gpoint_app", "shared_internal", "eros_pilot_app", "eros_pilot_embeddings"])
        self.assertNotIn("gpoint_app", self.services["eros"]["networks"])
        self.assertNotIn("eros_pilot_fetch", self.services["eros-pilot-api"]["networks"])
        self.assertNotIn("eros_pilot_fetch", self.services["ollama"]["networks"])
        self.assertEqual(self.services["ollama-model-pull"]["networks"], ["eros_pilot_fetch"])
        self.assertEqual(self.services["ollama-model-verify"]["networks"], ["eros_pilot_embeddings"])
        self.assertIsNone(self.services["ollama-model-pull"].get("depends_on"))
        self.assertEqual(self.services["ollama"].get("depends_on", {}).get("ollama-model-pull", {}).get("condition"),
                         "service_completed_successfully")

    def test_model_runtime_and_weights_are_digest_pinned(self):
        lock = json.loads((ROOT / "services/eros-engine/ollama-model.lock.json").read_text())
        self.assertEqual(lock["model"], "qwen3-embedding:0.6b")
        self.assertRegex(lock["digest"], r"^[a-f0-9]{64}$")
        self.assertIn(lock["digest"], (ROOT / "infra/production/docker-compose.eros-pilot.yml").read_text())
        puller = (ROOT / "infra/production/pull-eros-pilot-model.sh").read_text()
        verifier = (ROOT / "services/eros-engine/verify-eros-pilot-model.mjs").read_text()
        self.assertIn("ollama pull", puller)
        self.assertNotIn("curl", puller)
        self.assertIn("EROS_PILOT_OLLAMA_MODEL_DIGEST", verifier)
        self.assertIn("/api/tags", verifier)
        self.assertNotIn("console.log(JSON.stringify", verifier)

    def test_host_bind_mounts_are_bound_to_the_exact_v5_source_release(self):
        for name in ("postgres", "ollama-model-pull", "ollama-model-verify", "eros"):
            service = self.services[name]
            mounts = service.get("volumes", [])
            for mount in mounts:
                if isinstance(mount, str) and ":/" in mount and not mount.startswith("eros_pilot_"):
                    self.assertIn("/opt/gpoint/releases/${EROS_PILOT_SOURCE_SHA", mount)
                    self.assertIn("/source/", mount)

    def test_backup_restore_runbook_uses_only_isolated_pilot_resources(self):
        readme = (ROOT / "services/eros-engine/README.md").read_text()
        self.assertIn('pg_dump -U eros_pilot_admin -d eros_pilot -Fc', readme)
        self.assertIn('up -d --no-deps eros', readme)
        self.assertIn('EROS_PILOT_SOURCE_SHA', readme)
        self.assertIn('PILOT_BACKUP', readme)
        self.assertIn('sha256sum -c', readme)
        self.assertIn('backup_stamp=${PILOT_BACKUP##*/eros-pilot-}', readme)
        self.assertIn('test -n "$backup_stamp" && test "$backup_stamp" = "$env_stamp"', readme)
        self.assertIn('install -m 600 /opt/gpoint/env/eros-pilot.env', readme)
        self.assertIn('`down -v`', readme)
        self.assertIn('touch the GPoint database', readme)

    def test_public_caddy_route_rejects_the_embeddings_proxy(self):
        caddy = (ROOT / "infra/production/Caddyfile").read_text()
        self.assertIn("path /api/eros-worker/v1/embeddings*", caddy)
        denied = caddy.index("respond @eros_worker_embeddings 404")
        api_proxy = caddy.index("handle @api")
        self.assertLess(denied, api_proxy)


if __name__ == "__main__":
    unittest.main()
