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

    def test_main_push_runs_nonproduction_jobs_only(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("push", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        deploy = self.workflow["jobs"]["deploy"]
        self.assertEqual(deploy["if"], "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'")

    def test_python_job_runs_the_disposable_postgres_migration_harness(self):
        job = self.workflow["jobs"]["python"]
        run_steps = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("services/gsales/tests/run_postgres.py", run_steps)
        checkout = next(step for step in job["steps"]
                        if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        harness = (ROOT / "services/gsales/tests/run_postgres.py").read_text()
        self.assertIn("pgvector/pgvector:0.8.6-pg16", harness)

    def test_native_workflow_runs_actionlint_before_build(self):
        steps = self.workflow["jobs"]["workspace"]["steps"]
        lint = next(step for step in steps if step.get("name") == "Validate GitHub Actions workflow")
        pnpm_setup = next(i for i, step in enumerate(steps)
                          if step.get("uses", "").startswith("pnpm/action-setup@"))
        node_setup = next(i for i, step in enumerate(steps)
                          if step.get("uses", "").startswith("actions/setup-node@"))
        build = self.workflow["jobs"]["build"]
        self.assertIn("actionlint@v1.7.7", lint["run"])
        self.assertLess(pnpm_setup, node_setup)
        self.assertEqual(steps[pnpm_setup]["with"]["version"], "9.15.0")
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
        self.assertIn("miniapp_digest", build["outputs"])
        self.assertIn("steps.app.outputs.digest", build["outputs"]["app_digest"])
        readback = next(step for step in steps
                        if step.get("name") == "Read back exact published images and verify candidate identity")
        self.assertIn("docker pull --platform linux/amd64", readback["run"])
        self.assertIn("docker image inspect --format '{{json .}}'", readback["run"])
        self.assertIn("scripts/verify_native_image_readback.py", readback["run"])
        verifier = (ROOT / "scripts/verify_native_image_readback.py").read_text()
        self.assertIn("org.opencontainers.image.revision", verifier)
        self.assertIn("org.opencontainers.image.source", verifier)
        self.assertIn("imagetools inspect --format '{{json .SBOM.SPDX}}'", readback["run"])
        self.assertIn("imagetools inspect --format '{{json .Provenance}}'", readback["run"])
        attestation_verifier = (ROOT / "scripts/verify_native_image_attestation.py").read_text()
        self.assertIn("verify_provenance_identity", attestation_verifier)
        miniapp = next(step for step in steps if step.get("id") == "miniapp")
        self.assertEqual(miniapp["with"]["target"], "miniapp-runtime")
        self.assertEqual(miniapp["with"]["sbom"], True)
        self.assertEqual(miniapp["with"]["provenance"], "mode=max")
        for role in ("agata", "gsales", "miniapp"):
            image_step = next(step for step in steps if step.get("id") == role)
            self.assertIn("GPOINT_BUILD_SHA=${{ github.sha }}", image_step["with"]["build-args"])

    def test_deploy_is_exact_sha_owner_gated_kamal_and_keeps_gsales_stopped(self):
        deploy = self.workflow["jobs"]["deploy"]
        script = "\n".join(step.get("run", "") for step in deploy["steps"])
        self.assertIn("REQUESTED_SHA", script)
        self.assertIn("WORKFLOW_SHA", script)
        self.assertIn("APPROVE_PRODUCTION", script)
        self.assertIn("OWNER_APPROVAL_ACTOR", script)
        self.assertEqual(deploy["needs"], "build")
        self.assertTrue(all(name in self.workflow["jobs"]["build"]["outputs"]
                            for name in ("app_digest", "agata_digest", "gsales_digest")))
        deploy_text = yaml.safe_dump(deploy)
        self.assertIn("needs.build.outputs.app_digest", deploy_text)
        self.assertIn("needs.build.outputs.agata_digest", deploy_text)
        self.assertIn("needs.build.outputs.gsales_digest", deploy_text)
        self.assertIn("needs.build.outputs.miniapp_digest", deploy_text)
        self.assertNotRegex(script, r"docker\s+compose\s+(up|down|pull|restart)")
        self.assertIn("Expected stopped GSales service is missing", script)
        self.assertIn("test \"$(docker inspect --format '{{.State.Running}}' \"$id\")\" = false", script)
        self.assertIn("kamal deploy --skip-push", script)
        self.assertIn("GPOINT_KAMAL_VERIFY=PASS", script)

    def test_no_mac_runner_or_release_controller_is_used(self):
        text = WORKFLOW.read_text()
        self.assertNotIn("[self-hosted, macOS, gpoint-production]", text)
        self.assertNotIn("self-hosted", text)
        self.assertNotIn("release:gpoint", text)

    def test_native_kamal_uses_attempt_scoped_image_version_and_hook_context(self):
        workflow = self.workflow
        deploy = workflow["jobs"]["deploy"]
        combined = WORKFLOW.read_text()
        deploy_script = "\n".join(step.get("run", "") for step in deploy["steps"])
        self.assertIn("${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}", combined)
        self.assertIn("--env GPOINT_KAMAL_SERVICE", deploy_script)
        self.assertIn("--env KAMAL_ROLES", deploy_script)
        self.assertIn("GPOINT_KAMAL_SERVICE=gpoint-core KAMAL_ROLES=api", deploy_script)
        self.assertIn("GPOINT_KAMAL_SERVICE=gpoint-agata KAMAL_ROLES=api", deploy_script)
        hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
        self.assertIn('[[ "$version" == "$candidate-"* && "$version" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]', hook)

    def test_workspace_ci_runs_real_kamal_config_and_proxy_integration_checks(self):
        steps = self.workflow["jobs"]["workspace"]["steps"]
        runs = "\n".join(step.get("run", "") for step in steps)
        self.assertIn('config --config-file config/deploy.yml', runs)
        self.assertIn('config --config-file config/deploy.agata.yml', runs)
        self.assertIn('config --config-file config/deploy.miniapp.yml', runs)
        self.assertIn("./scripts/test-kamal-proxy-integration.sh", runs)
        self.assertTrue(any(
            step.get("env", {}).get("KAMAL_IMAGE") == "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625"
            for step in steps
        ))


if __name__ == "__main__":
    unittest.main()
