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
                self.assertEqual(p["affectedBuildRoles"], [])
                self.assertEqual(p["requiredExactShaBuildRoles"], [])
                self.assertFalse(p["mayDeploySelectively"])
                self.assertFalse(p["maySkipV3Gates"])

    def test_agata_includes_gsales_consumer(self):
        p = v4.plan(B, A, [("M", "services/agata/app/responses/pipeline.py")],
                    baseline_verified=True)
        self.assertEqual(p["component"], "agata")
        self.assertEqual(p["affectedRuntimeServices"], [
            "agata-api", "agata-memory-worker",
            "gsales-agata-api", "gsales-agata-memory-worker",
        ])
        self.assertEqual(p["affectedBuildRoles"], ["agata"])
        self.assertEqual(p["requiredExactShaBuildRoles"], ["app", "agata", "gsales"])
        self.assertEqual(p["requiredHeavyWork"], [
            "agata_postgres_rehearsal", "gsales_postgres_regression",
            "exact_sha_docker_images",
        ])

    def test_gsales_runtime_source_is_identified_but_heavy_work_stays_required(self):
        p = v4.plan(B, A, [("M", "services/gsales/app/main.py")], baseline_verified=True)
        self.assertEqual(p["classification"], "COMPONENT_CANDIDATE")
        self.assertEqual(p["component"], "gsales")
        self.assertEqual(p["affectedRuntimeServices"], ["gsales-api"])
        self.assertEqual(p["affectedBuildRoles"], ["gsales"])
        self.assertEqual(p["requiredExactShaBuildRoles"], ["app", "agata", "gsales"])
        self.assertEqual(p["requiredHeavyWork"], [
            "agata_postgres_rehearsal", "gsales_postgres_regression",
            "exact_sha_docker_images",
        ])
        self.assertEqual(p["skippedHeavyWork"], [])
        self.assertFalse(p["maySkipV3Gates"])
        self.assertFalse(p["mayDeploySelectively"])

    def test_changed_test_files_force_full_instead_of_component_classification(self):
        for path in (
            "services/agata/tests/test_message_chronology.py",
            "services/agata/tests/test_internal_api_e2e.py",
            "services/gsales/tests/test_routes.py",
        ):
            with self.subTest(path=path):
                p = v4.plan(B, A, [("M", path)], baseline_verified=True)
                self.assertEqual(p["classification"], "FULL")
                self.assertEqual(p["component"], None)
                self.assertEqual(p["skippedHeavyWork"], [])

    def test_agata_and_gsales_source_mix_is_full(self):
        p = v4.plan(B, A, [
            ("M", "services/agata/app/responses/pipeline.py"),
            ("M", "services/gsales/app/main.py"),
        ], baseline_verified=True)
        self.assertEqual(p["classification"], "FULL")
        self.assertEqual(p["requiredHeavyWork"], [
            "agata_postgres_rehearsal", "gsales_postgres_regression",
            "exact_sha_docker_images",
        ])

    def test_component_advisory_requires_verifiable_source_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.invalid")
            f = repo / "services/gsales/app/main.py"
            f.parent.mkdir(parents=True)
            f.write_text("old")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "baseline")
            base = git(repo, "rev-parse", "HEAD")
            f.write_text("new")
            git(repo, "commit", "-qam", "update")
            candidate = git(repo, "rev-parse", "HEAD")
            p = v4.verified_plan(repo, base, candidate, baseline_verified=True)
            self.assertEqual(v4.verify_again(repo, base, candidate, p), p)
            forged = dict(p, affectedRuntimeServices=["gsales-api", "agata-api"])
            with self.assertRaises(ValueError):
                v4.verify_again(repo, base, candidate, forged)

    def test_prepare_advisory_binds_v3_ledger_baseline_and_exact_candidate_diff(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.invalid")
            f = repo / "services/gsales/app/main.py"
            f.parent.mkdir(parents=True)
            f.write_text("old")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "baseline")
            base = git(repo, "rev-parse", "HEAD")
            f.write_text("new")
            git(repo, "commit", "-qam", "update")
            candidate = git(repo, "rev-parse", "HEAD")
            v3 = {
                "candidateSha": candidate,
                "productionBaselineSha": base,
                "baselineSource": "gpoint-release-ledger",
                "reason": "impact-not-proven",
            }
            advisory = v4.verified_plan(repo, base, candidate, baseline_verified=True)
            self.assertEqual(
                v4.verify_prepare_advisory(repo, v3, candidate, advisory), advisory)
            self.assertEqual(advisory["skippedHeavyWork"], [])
            self.assertFalse(advisory["maySkipV3Gates"])
            self.assertFalse(advisory["mayDeploySelectively"])
            forged = dict(advisory, changedPaths=["services/gsales/app/other.py"])
            with self.assertRaises(ValueError):
                v4.verify_prepare_advisory(repo, v3, candidate, forged)
            forged_skip = dict(advisory, skippedHeavyWork=["gsales_postgres_regression"])
            with self.assertRaises(ValueError):
                v4.verify_prepare_advisory(repo, v3, candidate, forged_skip)
            with self.assertRaises(ValueError):
                v4.verify_prepare_advisory(
                    repo, dict(v3, baselineSource="unverified"), candidate, advisory)

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
