"""No network: source-change attribution is advisory, never a bypass."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v4", ROOT / "scripts/release_component_impact_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)
A, B = "a" * 40, "b" * 40


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


class TestImpactV4(unittest.TestCase):
    def test_ui_and_new_section(self):
        for name in ["apps/miniapp/src/Section.tsx", "apps/miniapp/public/a.svg", "apps/miniapp/index.html"]:
            with self.subTest(name=name):
                p = v4.plan(B, A, [("M", name)], baseline_verified=True)
                self.assertEqual(p["component"], "ui")
                self.assertEqual(p["affectedRuntimeServices"], ["miniapp_static"])
                self.assertFalse(p["mayDeploySelectively"])
                self.assertFalse(p["maySkipV3Gates"])

    def test_agata_includes_gsales_consumer(self):
        p = v4.plan(B, A, [("M", "services/agata/app/responses/pipeline.py"),
                            ("A", "services/agata/tests/test_message_chronology.py")],
                    baseline_verified=True)
        self.assertEqual(p["component"], "agata")
        self.assertIn("gsales-agata-api", p["affectedRuntimeServices"])

    def test_shared_and_security_are_full(self):
        for name in ["packages/contracts/src/index.ts", "services/agata/app/security.py",
                     "services/agata/migrations/versions/new.py",
                     "infra/production/Gpoint.Dockerfile", ".github/workflows/gpoint-deploy.yml",
                     "apps/api/src/main.ts", "pnpm-lock.yaml"]:
            with self.subTest(name=name):
                self.assertEqual(v4.plan(B, A, [("M", name)],
                                        baseline_verified=True)["classification"], "FULL")

    def test_mixed_is_full(self):
        p = v4.plan(B, A, [("M", "apps/miniapp/src/Main.tsx"),
                            ("M", "services/agata/app/responses/pipeline.py")],
                    baseline_verified=True)
        self.assertEqual(p["classification"], "FULL")

    def test_unsafe_path_and_status(self):
        for status, path in [("D", "apps/miniapp/src/X.tsx"),
                             ("R100", "apps/miniapp/src/X.tsx"),
                             ("M", "apps/miniapp/src/../X.tsx"),
                             ("M", "apps/miniapp/src/X\\Y.tsx")]:
            with self.subTest(status=status, path=path):
                self.assertEqual(v4.plan(B, A, [(status, path)],
                                        baseline_verified=True)["classification"], "FULL")

    def test_baseline_provenance_required(self):
        for base, proved in [(None, True), (A, False), (B, True)]:
            self.assertEqual(v4.plan(B, base, [("M", "apps/miniapp/src/X.tsx")],
                                     baseline_verified=proved)["classification"], "FULL")

    def test_git_diff_and_revalidation(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.invalid")
            f = repo / "apps/miniapp/src/X.tsx"
            f.parent.mkdir(parents=True)
            f.write_text("old")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "baseline")
            base = git(repo, "rev-parse", "HEAD")
            f.write_text("new")
            git(repo, "commit", "-qam", "update")
            candidate = git(repo, "rev-parse", "HEAD")
            p = v4.verified_plan(repo, base, candidate, baseline_verified=True)
            self.assertEqual(p["component"], "ui")
            self.assertEqual(v4.verify_again(repo, base, candidate, p), p)
            with self.assertRaises(ValueError):
                v4.verify_again(repo, base, candidate, dict(p, mayDeploySelectively=True))

    def test_symlink_denied(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.invalid")
            f = repo / "apps/miniapp/src/X.tsx"
            f.parent.mkdir(parents=True)
            f.write_text("old")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "before")
            base = git(repo, "rev-parse", "HEAD")
            f.unlink()
            f.symlink_to("/etc/passwd")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "symlink")
            candidate = git(repo, "rev-parse", "HEAD")
            p = v4.verified_plan(repo, base, candidate, baseline_verified=True)
            self.assertEqual(p["classification"], "FULL")

if __name__ == "__main__":
    unittest.main()
