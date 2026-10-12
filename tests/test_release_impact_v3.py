"""Fail-closed, network-free V3 impact selector regression tests."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("impact_v3", ROOT / "scripts/release_impact_v3.py")
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)
A, B = "a" * 40, "b" * 40


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


class ImpactV3Tests(unittest.TestCase):
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

    def test_exact_ui_only_avoids_backend_source_regressions(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "after")
        with patch.object(v3, "read_ledger_baseline", return_value=base):
            plan = v3.build_ci_plan(repo, sha, "token", "refs/heads/main")
        self.assertEqual(plan["classification"], "UI_ONLY")
        self.assertEqual(plan["productionBaselineSha"], base)
        self.assertEqual(plan["checks"], {k: False for k in v3.CHECKS})
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
        plan = v3.make_plan(sha, base, paths, reason="verified-production-ledger")
        self.assertEqual(plan["classification"], "FULL")
        self.assertTrue(all(plan["checks"].values()))

    def test_all_unknown_config_and_security_changes_are_full(self):
        for path in ("pnpm-lock.yaml", "AGENTS.md", ".github/workflows/gpoint-prepare.yml",
                     "apps/api/src/main.ts", "services/agata/worker/lab_queue.py",
                     "services/gsales/app/main.py", "apps/miniapp/vite.config.ts",
                     "packages/contracts/src/index.ts", "infra/production/Gpoint.Dockerfile",
                     "apps/miniapp/public/../unsafe", "docs/release/automation.md"):
            with self.subTest(path=path):
                result = v3.make_plan(B, A, [path], reason="verified-production-ledger")
                self.assertEqual(result["classification"], "FULL")

    def test_missing_ledger_token_bad_branch_and_dirty_repo_fall_back_full(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        self.assertEqual(v3.build_ci_plan(repo, sha, None, "refs/heads/main")["classification"], "FULL")
        self.assertEqual(v3.build_ci_plan(repo, sha, "token", "refs/heads/feature")["classification"], "FULL")
        (repo / "untracked").write_text("dirty")
        with patch.object(v3, "read_ledger_baseline", return_value=base):
            self.assertEqual(v3.build_ci_plan(repo, sha, "token", "refs/heads/main")["classification"], "FULL")

    def test_ui_plan_rejects_tampered_paths_candidate_and_broad_scope(self):
        repo, base = self.make_repo()
        sha = self.commit(repo, "apps/miniapp/src/ui.tsx", "new")
        plan = v3.make_plan(sha, base, ["apps/miniapp/src/ui.tsx"],
                            reason="verified-production-ledger")
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
        with patch.object(v3, "read_ledger_baseline", return_value=A):
            v3.verify_receipt_baseline(receipt, B, "token")
        with patch.object(v3, "read_ledger_baseline", return_value="c" * 40):
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                v3.verify_receipt_baseline(receipt, B, "token")
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            v3.verify_receipt_baseline(receipt, A, "token")
        receipt.write_text(json.dumps({
            "candidateSha": B, "status": "READY", "impactV3": {"classification": "FULL"},
            "gates": {"postgres": "SKIPPED", "gsalesPostgres": "PASS"},
        }))
        with self.assertRaisesRegex(ValueError, "unproved FULL"):
            v3.verify_receipt_baseline(receipt, B, "token")

    def test_canonical_ledger_requires_complete_safe_status(self):
        def opener(data):
            raw = json.dumps({"content": base64.b64encode(json.dumps(data).encode()).decode(),
                              "encoding": "base64"}).encode()
            class Response:
                def __enter__(self): return io.BytesIO(raw)
                def __exit__(self, *args): pass
            return lambda request, timeout: Response()
        valid = {"currentProductionSha": A, "lastReleaseStatus": "COMPLETE",
                 "serverDeployStatus": "COMPLETE", "releaseOrigin": "durable-production-release",
                 "recoveryRequired": False}
        self.assertEqual(v3.read_ledger_baseline("token", opener(valid)), A)
        for state in (
            {"currentProductionSha": A, "lastReleaseStatus": "FAILED"},
            {"currentProductionSha": A, "lastReleaseStatus": "COMPLETE", "recoveryRequired": True},
            {"currentProductionSha": "bad", "lastReleaseStatus": "COMPLETE"},
            {**valid, "serverDeployStatus": "FAILED"},
            {**valid, "releaseOrigin": "unverified"},
            {**valid, "recoveryRequired": True},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                v3.read_ledger_baseline("token", opener(state))


if __name__ == "__main__":
    unittest.main()
