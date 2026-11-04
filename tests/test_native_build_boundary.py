import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"
CUTOVER_WORKFLOW = ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml"


class NativeReleaseBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_pipeline_has_release_gate_and_one_isolated_rehearsal_job(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"workspace", "python", "build", "deploy", "rollback", "backup_maintenance", "migration_rehearsal"})
        rollback = jobs["rollback"]
        self.assertIn("outputs.mode == 'ROLLBACK'", rollback["if"])
        self.assertEqual(rollback["environment"], {
            "name": "production", "url": "${{ steps.verify.outputs.environment_url }}",
        })
        self.assertEqual(rollback["concurrency"], {"group": "gpoint-production-release", "cancel-in-progress": False})
        self.assertNotIn("build", rollback["needs"])
        self.assertEqual(jobs["migration_rehearsal"]["needs"], ["workspace", "python", "build"])
        self.assertIn("github.event_name == 'workflow_dispatch'", jobs["migration_rehearsal"]["if"])
        self.assertIn("github.ref == 'refs/heads/main'", jobs["migration_rehearsal"]["if"])
        self.assertIn("inputs.migration_rehearsal == true", jobs["migration_rehearsal"]["if"])
        self.assertIn("workspace", jobs["deploy"]["needs"])
        self.assertEqual(set(jobs["build"]["needs"]), {"workspace", "python"})
        self.assertIn("outputs.mode == 'DEPLOY'", jobs["build"]["if"])
        self.assertIn("outputs.mode == 'REHEARSAL'", jobs["build"]["if"])
        self.assertIn("build", jobs["deploy"]["needs"])

    def test_main_push_runs_ci_build_and_kamal_production_jobs(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        deploy = self.workflow["jobs"]["deploy"]
        self.assertEqual(deploy["if"], "needs.workspace.outputs.deploy == 'true' && github.ref == 'refs/heads/main' && (github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && inputs.migration_rehearsal != true && inputs.backup_maintenance != true))")
        self.assertIn("github.event_name == 'push'", deploy["if"])

    def test_operational_only_push_and_migration_rehearsal_fail_closed(self):
        gate = self.workflow["jobs"]["workspace"]
        self.assertEqual(gate["outputs"]["deploy"], "${{ steps.intent.outputs.deploy }}")
        self.assertIn("scripts/native_release_intent.py", "\n".join(step.get("run", "") for step in gate["steps"]))
        self.assertIn("--backup-maintenance", "\n".join(step.get("run", "") for step in gate["steps"]))
        intent = (ROOT / "scripts/native_release_intent.py").read_text()
        self.assertNotIn("PREP_ONLY_SCOPES", intent)
        self.assertNotIn('return "PREPARE_ONLY"', intent)
        self.assertIn('return "BLOCK"', intent)
        self.assertEqual(self.workflow["jobs"]["backup_maintenance"]["environment"],
                         {"name": "production", "deployment": False})
        deploy = self.workflow["jobs"]["deploy"]
        self.assertIn("needs.workspace.outputs.deploy == 'true'", deploy["if"])
        rehearsal = self.workflow["jobs"]["migration_rehearsal"]
        self.assertEqual(rehearsal["environment"], {"name": "production", "deployment": False})
        self.assertNotIn("deployments", rehearsal["permissions"])
        rehearsal_run = "\n".join(step.get("run", "") for step in rehearsal["steps"])
        self.assertNotIn("repos/$GITHUB_REPOSITORY/deployments", rehearsal_run)
        self.assertIn("--format '{{.Os}} {{.Architecture}}'", rehearsal_run)

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
        self.assertIn("actionlint@v1.7.12", lint["run"])
        go_setup = next(step for step in steps
                        if step.get("uses", "").startswith("actions/setup-go@"))
        self.assertEqual(go_setup["with"]["go-version"], "1.25.3")
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
        for role in ("agata", "miniapp"):
            image_step = next(step for step in steps if step.get("id") == role)
            self.assertIn("GPOINT_BUILD_SHA=${{ github.sha }}", image_step["with"]["build-args"])

    def test_runtime_images_build_only_after_verified_component_baseline(self):
        build = self.workflow["jobs"]["build"]
        self.assertNotIn("environment", build)
        self.assertEqual(set(build["needs"]), {"workspace", "python"})
        steps = build["steps"]
        resolver = next(i for i, step in enumerate(steps)
                        if step.get("id") == "build_scope")
        for component, step_id in (("CORE", "app"), ("AGATA", "agata"), ("MINIAPP", "miniapp")):
            image = next(i for i, step in enumerate(steps) if step.get("id") == step_id)
            self.assertGreater(image, resolver)
            self.assertIn(component, steps[image]["if"])
        resolver_run = steps[resolver]["run"]
        self.assertIn("scripts/resolve_native_build_scope.py", resolver_run)
        self.assertEqual(build["permissions"]["deployments"], "read")
        self.assertEqual(build["permissions"]["actions"], "read")
        workflow_runs = "\n".join(
            step.get("run", "")
            for job in self.workflow["jobs"].values()
            for step in job.get("steps", [])
        )
        self.assertIn("tests/test_release_pre_push_mode.py", workflow_runs)
        deploy = self.workflow["jobs"]["deploy"]
        deploy_names = [step.get("name", "") for step in deploy["steps"]]
        live_inventory = deploy_names.index("Verify installed Kamal topology before ordinary release")
        component_plan = deploy_names.index("Plan exact component-scoped Kamal update from live baseline")
        self.assertLess(live_inventory, component_plan)
        verify_images = deploy_names.index("Verify immutable candidate images selected for the live scope")
        self.assertGreater(verify_images, component_plan)

    def test_ordinary_release_does_not_contain_first_cutover_mutations(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        forbidden = (
            "Capture PostgreSQL and no-Eros cutover baseline",
            "Verify fresh PostgreSQL and media backup with isolated restore",
            "Pause legacy GPoint services after restore gates",
            "Apply candidate Caddyfile after request-serving roles are deployed",
        )
        names = {step.get("name") for step in steps}
        self.assertTrue(names.isdisjoint(forbidden))

    def test_ordinary_build_does_not_publish_gsales_image(self):
        build = self.workflow["jobs"]["build"]
        self.assertNotIn("gsales_digest", build["outputs"])
        build_text = yaml.safe_dump(build)
        self.assertNotIn("gpoint-g-sales-runtime", build_text)
        self.assertNotIn("id: gsales", build_text)

    def test_ordinary_release_rejects_compose_topology_before_deploy(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        names = [step.get("name", "") for step in steps]
        preflight = next(i for i, name in enumerate(names) if name == "Verify installed Kamal topology before ordinary release")
        deploy_roles = next(i for i, name in enumerate(names) if name == "Deploy exact immutable images with stock Kamal")
        self.assertLess(preflight, deploy_roles)
        self.assertIn("for service in api worker bot agata-api agata-memory-worker", steps[preflight]["run"])
        self.assertIn("shared_postgres_1", steps[preflight]["run"])
        self.assertIn("compose.service=$service", steps[preflight]["run"])
        self.assertIn("agata-memory-worker", steps[preflight]["run"])

    def test_stock_kamal_inventory_commands_precede_supplementary_docker_inspection(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        names = [step.get("name", "") for step in steps]
        cli_index = names.index("Read live application version and containers with Kamal")
        topology_index = names.index("Verify installed Kamal topology before ordinary release")
        self.assertLess(cli_index, topology_index)
        kamal_inventory = steps[cli_index]
        self.assertEqual(kamal_inventory.get("id"), "kamal_inventory")
        commands = kamal_inventory["run"]
        for command in ("kamal app version", "kamal app details", "kamal app containers"):
            self.assertIn(command, commands)
        self.assertIn("--env KAMAL_REGISTRY_PASSWORD", commands)
        self.assertIn("Kamal inventory could not resolve one exact current version", commands)
        self.assertIn("Kamal inventory details do not include expected role/version", commands)
        self.assertIn("Kamal inventory containers do not include expected role/version", commands)
        self.assertIn('>> "$GITHUB_OUTPUT"', commands)
        for role in ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot",
                     "gpoint-agata-api", "gpoint-agata-memory-worker", "gpoint-miniapp-web"):
            self.assertIn(role, commands)
        self.assertIn("config/deploy.yml", commands)
        self.assertIn("config/deploy.agata.yml", commands)
        self.assertIn("config/deploy.miniapp.yml", commands)
        topology = steps[topology_index]
        for component in ("core", "agata", "miniapp"):
            self.assertTrue(any(
                f"steps.kamal_inventory.outputs.{component}_version" in value
                for value in topology["env"].values()
            ))
        self.assertIn("KAMAL_CORE_VERSION", topology["run"])

    def test_ordinary_deployment_success_requires_verified_manifest(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        verify = next(i for i, step in enumerate(steps) if step.get("name") == "Verify public routes and exact running services")
        resolve = next(i for i, step in enumerate(steps) if step.get("name") == "Resolve exact GitHub environment Deployment ID")
        self.assertLess(resolve, verify)
        self.assertIn("manifest_sha", steps[verify]["run"])
        self.assertIn("environment_url=https://gpoint.one/#gpoint-manifest-sha256=$manifest_sha", steps[verify]["run"])
        self.assertEqual(deploy["environment"]["url"], "${{ steps.verify.outputs.environment_url }}")
        self.assertNotIn("deployments/$DEPLOYMENT_ID/statuses", "\n".join(step.get("run", "") for step in steps))
        verify_run = steps[verify]["run"]
        self.assertIn("scp -p scripts/write-kamal-production-manifest.py", verify_run)
        self.assertIn("--mode ordinary", verify_run)
        self.assertIn("--db-container-id", verify_run)
        self.assertIn("--public-routes-verified --migration-compatibility-verified", verify_run)
        self.assertIn("GPOINT_KAMAL_MANIFEST_SHA256", verify_run)
        self.assertIn("/media/assets/kamal-route-verification-missing", verify_run)
        self.assertIn('"message":"media not found"', verify_run)


    def test_deploy_is_exact_sha_owner_gated_kamal_and_keeps_gsales_stopped(self):
        deploy = self.workflow["jobs"]["deploy"]
        script = "\n".join(step.get("run", "") for step in deploy["steps"])
        self.assertIn("REQUESTED_SHA", script)
        self.assertIn("WORKFLOW_SHA", script)
        self.assertIn("APPROVE_PRODUCTION", script)
        self.assertIn("OWNER_APPROVAL_ACTOR", script)
        self.assertEqual(deploy["needs"], ["workspace", "python", "build"])
        self.assertTrue(all(name in self.workflow["jobs"]["build"]["outputs"]
                            for name in ("app_digest", "agata_digest", "miniapp_digest")))
        self.assertNotIn("gsales_digest", self.workflow["jobs"]["build"]["outputs"])
        deploy_text = yaml.safe_dump(deploy)
        self.assertIn("needs.build.outputs.app_digest", deploy_text)
        self.assertIn("needs.build.outputs.agata_digest", deploy_text)
        self.assertNotIn("needs.build.outputs.gsales_digest", deploy_text)
        self.assertIn("needs.build.outputs.miniapp_digest", deploy_text)
        self.assertNotRegex(script, r"docker\s+compose\s+(up|down|pull|restart)")
        phases = "\n".join((ROOT / "scripts/kamal-first-cutover" / (name+".sh")).read_text()
                           for name in ("requests", "consumers", "verify"))
        script += phases
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
        self.assertTrue(hook.startswith("#!/bin/sh\n"))
        self.assertIn('case "$version" in "$candidate"-*)', hook)

    def test_workspace_ci_runs_real_kamal_config_and_proxy_integration_checks(self):
        steps = self.workflow["jobs"]["workspace"]["steps"]
        runs = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("./scripts/check-kamal-configs.sh", runs)
        self.assertIn("./scripts/test-kamal-proxy-integration.sh", runs)
        self.assertLess(runs.index("./scripts/check-kamal-configs.sh"),
                        runs.index("./scripts/test-kamal-proxy-integration.sh"))
        self.assertTrue(any(
            step.get("env", {}).get("KAMAL_IMAGE") == "ghcr.io/basecamp/kamal:v2.12.0@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625"
            for step in steps
        ))

        config_check = (ROOT / "scripts/check-kamal-configs.sh").read_text()
        for config in ("deploy.yml", "deploy.agata.yml", "deploy.miniapp.yml"):
            self.assertIn(f"config/{config}", config_check)
        self.assertIn("config-validation-placeholder", config_check)
        self.assertIn("Kamal config validation failed", config_check)


class FirstCutoverWorkflowTests(unittest.TestCase):
    def load_cutover(self):
        self.assertTrue(CUTOVER_WORKFLOW.exists(), "first-cutover workflow must remain available")
        return yaml.safe_load(CUTOVER_WORKFLOW.read_text())

    def test_cutover_workflow_keeps_fresh_inventory_backup_and_restore_gates(self):
        workflow = self.load_cutover()
        names = {step.get("name") for step in workflow["jobs"]["deploy"]["steps"]}
        self.assertIn("Capture PostgreSQL and no-Eros cutover baseline", names)
        self.assertIn("Verify application source migrations match the live database", names)
        self.assertIn("Verify fresh PostgreSQL and media backup with isolated restore", names)
        self.assertIn("Run one server-owned Kamal cutover transaction", names)
        self.assertIn("scripts/retire-gpoint-caddy.sh", (ROOT / "scripts/kamal-first-cutover/retire.sh").read_text())

    def test_cutover_still_verifies_gsales_stopped_and_uses_stock_kamal(self):
        workflow = self.load_cutover()
        deploy = workflow["jobs"]["deploy"]
        script = "\n".join(step.get("run", "") for step in deploy["steps"])
        phases = "\n".join((ROOT / "scripts/kamal-first-cutover" / (name+".sh")).read_text()
                           for name in ("requests", "consumers", "verify"))
        script += phases
        self.assertIn("Expected stopped GSales service is missing", script)
        self.assertIn("kamal deploy --skip-push", script)
        self.assertIn("StrictHostKeyChecking=yes", script)


if __name__ == "__main__":
    unittest.main()
