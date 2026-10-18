"""Repository instruction contract for GPoint's DEVOPS GitHub access preflight.

This checks the repository-provided context chain, not the Codex model runtime.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STANDARD_URL = "https://linear.app/bestuzhev/document/github-dostup-7970682d1b80"


class GithubAccessPreflightInstructionTests(unittest.TestCase):
    def test_devops_context_requires_current_canonical_access_standard(self):
        agents = (ROOT / "AGENTS.md").read_text()
        bootstrap = (ROOT / "docs/codex/role-bootstrap.md").read_text()
        profile = (ROOT / ".codex/roles/devops.md").read_text()

        # The role-selection chain must make the canonical source available to
        # the selected DEVOPS profile before any GitHub/GHCR operation.
        self.assertIn("docs/codex/role-bootstrap.md", agents)
        self.assertIn("[DEVOPS](.codex/roles/devops.md)", agents)
        self.assertIn("ROLE=DEVOPS", bootstrap)
        self.assertIn("load `.codex/roles/devops.md`", bootstrap)
        self.assertIn(STANDARD_URL, agents)
        self.assertIn(STANDARD_URL, bootstrap)
        self.assertIn(STANDARD_URL, profile)
        self.assertIn("при любой ошибке github или ghcr", agents.lower())
        self.assertIn("обязательный preflight", bootstrap.lower())
        self.assertIn("before diagnosing any github or ghcr error, first read", profile.lower())

    def test_dns_failure_is_scoped_and_classified_before_blocking(self):
        agents = (ROOT / "AGENTS.md").read_text()
        bootstrap = (ROOT / "docs/codex/role-bootstrap.md").read_text()
        profile = (ROOT / ".codex/roles/devops.md").read_text()
        required_classes = ("NETWORK", "TOOLING", "AUTH", "PERMISSION", "OWNER_AUTHORIZATION")

        for source in (agents, bootstrap, profile):
            for failure_class in required_classes:
                with self.subTest(source=source[:60], failure_class=failure_class):
                    self.assertIn(failure_class, source)

        self.assertIn("dns внутри sandbox не доказывает глобальную недоступность", agents.lower())
        self.assertIn("именно проверенного execution context", bootstrap.lower())
        self.assertIn("never declare github/ghcr globally unavailable", profile.lower())
        self.assertIn("check штатные доступные способы подключения", profile.lower())


if __name__ == "__main__":
    unittest.main()
