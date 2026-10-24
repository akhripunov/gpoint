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
        for name in ("eros-pilot-api", "eros", "eros-migrate", "postgres", "ollama-model-verify", "eros-pilot-embedding-relay"):
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
        self.assertEqual(self.services["eros"]["depends_on"]["ollama-model-verify"]["condition"],
                         "service_completed_successfully")

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
        self.assertEqual(self.services["eros-pilot-api"]["extra_hosts"], ["ollama:172.31.254.1"])
        self.assertIn("EROS_PRODUCTION_TEST_USER_IDS", self.services["eros-pilot-api"]["environment"])
        self.assertIn("EROS_PRODUCTION_TEST_CREATOR_IDS", self.services["eros-pilot-api"]["environment"])
        self.assertIn("EROS_PILOT_OLLAMA_MODEL_DIGEST", self.services["ollama-model-verify"]["environment"])
        self.assertEqual(self.services["eros-pilot-api"]["extra_hosts"], ["ollama:172.31.254.1"])
        self.assertIn("ollama-model-verify", self.services["eros"]["depends_on"])
        self.assertNotIn("ollama", self.services)
        self.assertNotIn("ollama-model-pull", self.services)
        self.assertNotIn("EROS_PILOT_OLLAMA_IMAGE", (COMPOSE.read_text()))
        self.assertEqual(self.services["eros-pilot-embedding-relay"]["network_mode"], "host")
        self.assertNotIn("ports", self.services["eros-pilot-embedding-relay"])
        self.assertEqual(self.services["eros-pilot-embedding-relay"]["environment"]["EROS_PILOT_RELAY_BIND"], "172.31.254.1:11434")
        self.assertIn("/healthz", self.services["eros-pilot-embedding-relay"]["healthcheck"]["test"][-1])

    def test_pilot_network_is_internal_and_only_api_bridges_to_gpoint(self):
        networks = self.compose["networks"]
        self.assertTrue(networks["eros_pilot_app"]["internal"])
        self.assertTrue(networks["eros_pilot_data"]["internal"])
        self.assertEqual(set(self.services["eros-pilot-api"]["networks"]), {"gpoint_app", "shared_internal", "eros_pilot_app", "eros_pilot_embeddings"})
        self.assertNotIn("gpoint_app", self.services["eros"]["networks"])
        self.assertNotIn("eros_pilot_fetch", networks)
        self.assertNotIn("gpoint_app", self.services["eros"].get("networks", []))
        self.assertEqual(networks["eros_pilot_embeddings"]["ipam"]["config"][0]["subnet"], "172.31.254.0/29")
        self.assertEqual(self.services["eros-pilot-api"]["networks"]["eros_pilot_embeddings"]["ipv4_address"], "172.31.254.2")
        self.assertEqual(self.services["ollama-model-verify"]["networks"]["eros_pilot_embeddings"]["ipv4_address"], "172.31.254.3")
        self.assertNotIn("eros_pilot_embeddings", self.services["eros"]["networks"])

    def test_model_runtime_and_weights_are_digest_pinned(self):
        lock = json.loads((ROOT / "services/eros-engine/ollama-model.lock.json").read_text())
        self.assertEqual(lock["model"], "qwen3-embedding:0.6b")
        self.assertRegex(lock["digest"], r"^[a-f0-9]{64}$")
        self.assertIn(lock["digest"], (ROOT / "infra/production/docker-compose.eros-pilot.yml").read_text())
        verifier = (ROOT / "services/eros-engine/verify-eros-pilot-model.mjs").read_text()
        self.assertIn("EROS_PILOT_OLLAMA_MODEL_DIGEST", verifier)
        self.assertIn("/api/tags", verifier)
        self.assertNotIn("console.log(JSON.stringify", verifier)
        relay = (ROOT / "services/eros-engine/eros-pilot-embedding-relay.mjs").read_text()
        self.assertIn('const UPSTREAM_HOST = "127.0.0.1"', relay)
        self.assertIn("const UPSTREAM_PORT = 11435", relay)
        self.assertIn("172.31.254.2", relay)
        self.assertIn("172.31.254.3", relay)
        self.assertIn(lock["digest"], relay)
        tunnel = (ROOT / "services/eros-engine/open-mac-ollama-tunnel.sh").read_text()
        self.assertIn("-R 127.0.0.1:11435:127.0.0.1:11434", tunnel)
        self.assertIn("GatewayPorts=no", tunnel)
        self.assertIn("StrictHostKeyChecking=yes", tunnel)
        self.assertNotIn("0.0.0.0", tunnel)

    def test_host_bind_mounts_are_bound_to_the_exact_v5_source_release(self):
        for name in ("postgres", "ollama-model-verify", "eros-pilot-embedding-relay", "eros"):
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
