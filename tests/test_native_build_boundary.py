import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


class NativeReleaseBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_pipeline_has_only_the_four_native_release_jobs(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"workspace", "python", "build", "deploy"})
        self.assertEqual(set(jobs["build"]["needs"]), {"workspace", "python"})
        self.assertIn("build", jobs["deploy"]["needs"])

    def test_candidate_push_runs_nonproduction_jobs_only(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("push", triggers)
        self.assertEqual(triggers["push"]["branches"], ["codex/me52-native-first-reconstruction"])
        deploy = self.workflow["jobs"]["deploy"]
        self.assertEqual(deploy["if"], "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'")

    def test_python_job_runs_the_disposable_postgres_migration_harness(self):
        job = self.workflow["jobs"]["python"]
        run_steps = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("services/gsales/tests/run_postgres.py", run_steps)
        harness = (ROOT / "services/gsales/tests/run_postgres.py").read_text()
        self.assertIn("pgvector/pgvector:0.8.6-pg16", harness)

    def test_native_workflow_runs_actionlint_before_build(self):
        steps = self.workflow["jobs"]["workspace"]["steps"]
        lint = next(step for step in steps if step.get("name") == "Validate GitHub Actions workflow")
        build = self.workflow["jobs"]["build"]
        self.assertIn("actionlint@v1.7.7", lint["run"])
        self.assertEqual(set(build["needs"]), {"workspace", "python"})

    def test_build_uses_immutable_candidate_identity_and_attestation(self):
        build = self.workflow["jobs"]["build"]
        steps = build["steps"]
        uses = [step.get("uses", "") for step in steps]
        self.assertTrue(any(use.startswith("docker/setup-buildx-action@") for use in uses))
        self.assertTrue(any(use.startswith("docker/login-action@") for use in uses))
        publish = next(step for step in steps if step.get("name") == "Build and publish immutable runtime image")
        self.assertEqual(publish["with"]["push"], True)
        self.assertEqual(publish["with"]["sbom"], True)
        self.assertEqual(publish["with"]["provenance"], "mode=max")
        self.assertEqual(publish["with"]["context"],
                         "https://github.com/akhripunov/gpoint.git#${{ github.sha }}")
        self.assertIn("app_digest", build["outputs"])
        self.assertIn("steps.app.outputs.digest", build["outputs"]["app_digest"])
        readback = next(step for step in steps
                        if step.get("name") == "Read back exact published images and verify candidate identity")
        self.assertIn("docker pull --platform linux/amd64", readback["run"])
        self.assertIn("RepoDigests", readback["run"])
        self.assertIn("org.opencontainers.image.revision", readback["run"])
        self.assertIn("org.opencontainers.image.source", readback["run"])
        self.assertIn("imagetools inspect --format '{{json .SBOM.SPDX}}'", readback["run"])
        self.assertIn("imagetools inspect --format '{{json .Provenance}}'", readback["run"])
        self.assertIn("vcs.get('revision') != sha", readback["run"])
        for role in ("agata", "gsales"):
            image_step = next(step for step in steps if step.get("id") == role)
            self.assertIn("GPOINT_BUILD_SHA=${{ github.sha }}", image_step["with"]["build-args"])

    def test_deploy_is_exact_sha_read_only_and_does_not_start_gsales(self):
        deploy = self.workflow["jobs"]["deploy"]
        script = "\n".join(step.get("run", "") for step in deploy["steps"])
        self.assertIn("REQUESTED_SHA", script)
        self.assertIn("WORKFLOW_SHA", script)
        self.assertEqual(deploy["needs"], "build")
        self.assertTrue(all(name in self.workflow["jobs"]["build"]["outputs"]
                            for name in ("app_digest", "agata_digest", "gsales_digest")))
        deploy_text = yaml.safe_dump(deploy)
        self.assertIn("needs.build.outputs.app_digest", deploy_text)
        self.assertIn("needs.build.outputs.agata_digest", deploy_text)
        self.assertIn("needs.build.outputs.gsales_digest", deploy_text)
        self.assertNotRegex(script, r"docker\s+compose\s+(up|down|pull|restart)")
        for service in ("gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"):
            self.assertNotIn(service, script)

    def test_no_mac_runner_or_release_controller_is_used(self):
        text = WORKFLOW.read_text()
        self.assertNotIn("gpoint-production", text)
        self.assertNotIn("self-hosted", text)
        self.assertNotIn("release:gpoint", text)


if __name__ == "__main__":
    unittest.main()
