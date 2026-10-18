"""Fail-closed, network-free V3 impact selector regression tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("impact_v3", ROOT / "scripts/release_impact_v3.py")
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)
A, B = "a" * 40, "b" * 40


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


class ImpactV3Tests(unittest.TestCase):
    def test_real_cli_entrypoint_builds_full_plan_for_exact_local_candidate(self):
        candidate = git(ROOT, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory(prefix="gpoint-impact-v3-cli-") as output_dir:
            output = Path(output_dir) / "impact.json"
            env = dict(os.environ, GITHUB_REF="refs/heads/main")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/release_impact_v3.py"),
                 "--candidate", candidate, "--output", str(output)],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            plan_text = output.read_text() if output.exists() else ""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("V3_IMPACT=FULL REASON=verified-live-baseline-unavailable", result.stdout)
        plan = json.loads(plan_text)
        self.assertEqual(plan["candidateSha"], candidate)
        self.assertEqual(plan["classification"], "FULL")
        self.assertIsNone(plan["productionBaselineSha"])
        self.assertIsNone(plan["baselineSource"])
        self.assertTrue(all(plan["checks"].values()))

    def make_repo(self):
        temp = tempfile.TemporaryDirectory(prefix="gpoint-impact-v3-test-")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "v3-fixture@example.test")
        git(repo, "config", "user.name", "V3 Fixture")
        (repo / "apps/miniapp/src").mkdir(parents=True)
        (repo / "apps/miniapp/src/ui.tsx").write_text("before")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "production")
        base = git(repo, "rev-parse", "HEAD")
        return repo, base

    def commit(self, repo, path, content):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "next")
        return git(repo, "rev-parse", "HEAD")

    def test_ui_change_still_requires_full_build_without_live_baseline(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "after")
        plan = v3.build_ci_plan(repo, sha, "token", "refs/heads/main")
        self.assertEqual(plan["classification"], "FULL")
        self.assertIsNone(plan["productionBaselineSha"])
        self.assertIsNone(plan["baselineSource"])
        self.assertTrue(all(plan["checks"].values()))
        self.assertEqual(plan["reason"], "verified-live-baseline-unavailable")
        file = repo.parent / (repo.name + "-plan.json")
        self.addCleanup(file.unlink, missing_ok=True)
        file.write_text(json.dumps(plan))
        self.assertEqual(v3.load_verified_plan(file, sha, repo), plan)
        self.assertIn("docker_images_exact_sha", plan["invariantChecks"])

    def test_mixed_unreleased_changes_require_full(self):
        repo, base = self.make_repo()
        self.commit(repo, "services/agata/migrations/versions/new.py", "migration")
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "label")
        paths = v3.changed_paths_between(repo, base, sha)
        plan = v3.make_plan(sha, base, paths, reason="verified-live-baseline-unavailable")
        self.assertEqual(plan["classification"], "FULL")
        self.assertTrue(all(plan["checks"].values()))

    def test_all_unknown_config_and_security_changes_are_full(self):
        for path in ("pnpm-lock.yaml", "AGENTS.md", ".github/workflows/gpoint-prepare.yml",
                     "apps/api/src/main.ts", "services/agata/worker/lab_queue.py",
                     "services/gsales/app/main.py", "apps/miniapp/vite.config.ts",
                     "packages/contracts/src/index.ts", "infra/production/Gpoint.Dockerfile",
                     "apps/miniapp/public/../unsafe", "docs/release/automation.md"):
            with self.subTest(path=path):
                result = v3.make_plan(B, A, [path], reason="verified-live-baseline-unavailable")
                self.assertEqual(result["classification"], "FULL")

    def test_branch_without_main_also_requires_full(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        self.assertEqual(v3.build_ci_plan(repo, sha, None, "refs/heads/main")["classification"], "FULL")
        self.assertEqual(v3.build_ci_plan(repo, sha, "token", "refs/heads/feature")["classification"], "FULL")

    def test_ui_plan_is_rejected_until_live_baseline_proof_is_supported(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        plan = v3.make_plan(sha, base, ["apps/miniapp/src/ui.tsx"],
                            reason="verified-live-baseline-unavailable")
        plan["classification"] = "UI_ONLY"
        plan["checks"] = {k: False for k in v3.CHECKS}
        plan["productionBaselineSha"] = base
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "plan.json"
        for edit in (
            {"candidateSha": A},
            {"changedPaths": ["apps/miniapp/src/other.tsx"]},
            {"checks": {"agata_postgres_rehearsal": False, "gsales_postgres_regression": True}},
            {"productionBaselineSha": A},
        ):
            bad = dict(plan, **edit)
            file.write_text(json.dumps(bad))
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                v3.load_verified_plan(file, sha, repo)

    def test_full_plan_rejects_injected_baseline_metadata(self):
        repo, _ = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        plan = v3.make_plan(sha, reason="verified-live-baseline-unavailable")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "plan.json"
        for edit in (
            {"productionBaselineSha": A},
            {"baselineSource": "github-deployment-vps-readback"},
            {"changedPaths": ["apps/miniapp/src/ui.tsx"]},
        ):
            file.write_text(json.dumps(dict(plan, **edit)))
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                v3.load_verified_plan(file, sha, repo)

    def test_removed_or_renamed_backend_path_never_becomes_ui_only(self):
        repo, base = self.make_repo()
        self.commit(repo, "services/gsales/app/api.py", "before")
        current = git(repo, "rev-parse", "HEAD")
        git(repo, "mv", "services/gsales/app/api.py", "apps/miniapp/src/api.tsx")
        git(repo, "commit", "-qm", "rename")
        sha = git(repo, "rev-parse", "HEAD")
        paths = v3.changed_paths_between(repo, current, sha)
        self.assertIn("services/gsales/app/api.py", paths)
        self.assertEqual(v3.make_plan(sha, current, paths, reason="verified-production-ledger")["classification"], "FULL")

    def test_diverged_prod_sha_rejected(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        with self.assertRaises((subprocess.CalledProcessError, ValueError)):
            v3.changed_paths_between(repo, B, sha)

    def test_prepublish_recheck_detects_production_drift(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        receipt = Path(tmp.name) / "receipt.json"
        receipt.write_text(json.dumps({
            "candidateSha": B, "status": "READY",
            "impactV3": {"classification": "UI_ONLY", "productionBaselineSha": A},
            "gates": {"postgres": "COVERED_UNCHANGED", "gsalesPostgres": "COVERED_UNCHANGED"},
        }))
        with self.assertRaisesRegex(ValueError, "unproved FULL"):
            v3.verify_receipt_baseline(receipt, B)
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            v3.verify_receipt_baseline(receipt, A, "token")
        receipt.write_text(json.dumps({
            "candidateSha": B, "status": "READY", "impactV3": {"classification": "FULL"},
            "gates": {"postgres": "SKIPPED", "gsalesPostgres": "PASS"},
        }))
        with self.assertRaisesRegex(ValueError, "unproved FULL"):
            v3.verify_receipt_baseline(receipt, B, "token")

if __name__ == "__main__":
    unittest.main()
