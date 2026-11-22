import unittest
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"


class NativeReleaseBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_python_release_boundary_suite_does_not_run_a_test_file_twice(self):
        step = next(step for step in self.workflow["jobs"]["tests"]["steps"]
                    if step.get("name") == "Run workflow, Native First and Kamal boundary tests")
        paths = [token for token in step["run"].split() if token.startswith("tests/")]
        self.assertEqual(len(paths), len(set(paths)), "duplicate test paths repeat work in the mandatory CI suite")

    def test_pipeline_has_release_gate_and_protected_migration_rehearsal(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"tests", "build", "deploy"})
        rollback = yaml.safe_load((ROOT / ".github/workflows/gpoint-release.yml").read_text())["jobs"]["deploy"]
        self.assertIn("inputs.mode == 'rollback'", "\n".join(step.get("if", "") for step in rollback["steps"]))
        self.assertEqual(rollback["environment"]["name"], "production")
        self.assertIn("steps.verify.outputs.environment_url", rollback["environment"]["url"])
        self.assertEqual(rollback["concurrency"]["group"], "gpoint-production-release")
        self.assertNotIn("LAYER7_API", rollback["if"])
        self.assertFalse(any(step.get("uses") == "./.github/actions/layer7-deploy" for step in rollback["steps"]))
        self.assertFalse(any("LAYER7_API" in step.get("if", "") for step in rollback["steps"]))
        rehearsal = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())["jobs"]["migration_rehearsal"]
        self.assertNotIn("needs", rehearsal)
        self.assertIn("github.ref == 'refs/heads/main'", rehearsal["if"])
        self.assertIn("inputs.migration_rehearsal == true", rehearsal["if"])
        self.assertEqual(jobs["build"]["needs"], ["tests"])
        self.assertNotIn("needs", jobs["deploy"])
        self.assertIn("outputs.mode == 'DEPLOY'", jobs["build"]["if"])
        self.assertIn("github.event_name == 'push'", jobs["build"]["if"])
        self.assertNotIn("pull_request", jobs["build"]["if"])
        self.assertNotIn("ARTIFACT_PREPARE", jobs["build"]["if"])

    def test_native_dispatch_has_no_cross_run_prebuilt_image_inputs(self):
        events = self.workflow.get("on", self.workflow.get(True))
        inputs = events["workflow_dispatch"]["inputs"]
        self.assertFalse(set(inputs) & {
            "use_prepared_images", "prepared_run_id", "prepared_app_digest",
            "prepared_agata_digest", "prepared_miniapp_digest", "prepared_eros_digest",
        })
        build = self.workflow["jobs"]["build"]
        self.assertNotIn("environment", build)
        self.assertNotEqual(build["permissions"].get("deployments"), "write")
        steps = build["steps"]
        self.assertFalse(any(step.get("id") in {"prepared", "prepared_run"} for step in steps))
        for image_step in (step for step in steps if step.get("uses", "").startswith("docker/build-push-action@")):
            self.assertNotIn("use_prepared_images", image_step.get("if", ""))
        readback = next(step for step in steps if step.get("id") == "image_readback")
        self.assertNotIn("REUSE_PREPARED_IMAGES", readback["env"])
        self.assertIn('docker pull --platform linux/amd64 "$image:$IMAGE_VERSION"', readback["run"])
        deploy = self.workflow["jobs"]["deploy"]
        self.assertIn("workflow_dispatch", deploy["if"])

    def test_prepare_and_deploy_are_separate_exact_sha_phases(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(jobs["tests"]["if"], "github.event_name != 'workflow_dispatch'")
        self.assertIn("github.event_name == 'push'", jobs["build"]["if"])
        self.assertIn("github.ref == 'refs/heads/main'", jobs["build"]["if"])
        self.assertNotIn("pull_request", jobs["build"]["if"])
        deploy = jobs["deploy"]
        self.assertNotIn("needs", deploy)
        self.assertIn("github.actor == github.repository_owner", deploy["if"])
        self.assertIn("inputs.approve_production == true", deploy["if"])
        self.assertEqual(deploy["environment"]["name"], "production")
        self.assertEqual(deploy["concurrency"]["group"], "gpoint-production-release")
        steps = deploy["steps"]
        ids = [step.get("id") for step in steps]
        self.assertLess(ids.index("prepared_run"), ids.index("prepared"))
        self.assertLess(ids.index("prepared"), next(i for i, step in enumerate(steps)
                                                      if step.get("name") == "Install verified Kamal SSH credentials"))
        self.assertLess(ids.index("eros_e2e_evidence"), next(i for i, step in enumerate(steps)
                                                             if step.get("name") == "Install verified Kamal SSH credentials"))
        lookup = next(step["run"] for step in steps if step.get("id") == "prepared_run")
        self.assertIn("head_sha == $sha", lookup)
        self.assertIn("conclusion == \"success\"", lookup)
        self.assertIn("artifact_name=\"gpoint-release-$run_id-$run_attempt\"", lookup)
        download = next(step for step in steps if step.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["digest-mismatch"], "error")
        self.assertEqual(download["with"]["run-id"], "${{ steps.prepared_run.outputs.run_id }}")
        manifest_upload = next(step for step in jobs["build"]["steps"]
                               if step.get("name") == "Store immutable release manifest for later Deploy and rollback")
        self.assertTrue(manifest_upload["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual(manifest_upload["with"]["retention-days"], 90)
        self.assertIn("requiredChecks", "\n".join(step.get("run", "") for step in jobs["build"]["steps"]))
        self.assertIn("approve_production == true", deploy["if"])
        self.assertNotIn("needs", deploy)

    def test_native_image_version_is_bound_to_its_current_build_run(self):
        build = self.workflow["jobs"]["build"]
        deploy = self.workflow["jobs"]["deploy"]
        version_step = next((step for step in build["steps"]
                             if step.get("id") == "image_version"), None)
        self.assertIsNotNone(version_step)
        self.assertEqual(
            version_step["env"]["CANDIDATE_SHA"],
            "${{ github.event_name == 'workflow_dispatch' && inputs.candidate_sha || github.sha }}",
        )
        self.assertNotIn("PREPARED", version_step["env"])
        self.assertNotIn("PREPARED", version_step["run"])
        self.assertEqual(build["outputs"]["image_version"], "${{ steps.image_version.outputs.image_version }}")
        self.assertIn("${CURRENT_RUN_ID}", version_step["run"])
        self.assertIn("${CURRENT_RUN_ATTEMPT}", version_step["run"])
        self.assertNotIn("IMAGE_VERSION", deploy["env"])
        self.assertIn("IMAGE_VERSION={version}", next(step["run"] for step in deploy["steps"] if step.get("id") == "prepared"))
        kamal = next(step for step in deploy["steps"]
                     if step.get("name") == "Deploy exact immutable images with stock Kamal")
        self.assertIn("--version \"$IMAGE_VERSION\"", kamal["run"])

    def test_every_admitted_build_selects_an_exact_image_version(self):
        build = self.workflow["jobs"]["build"]
        version = next(step for step in build["steps"] if step.get("id") == "image_version")
        self.assertNotIn("if", version)
        self.assertEqual(build["outputs"]["image_version"], "${{ steps.image_version.outputs.image_version }}")
        self.assertFalse(any(step.get("uses") == "./.github/actions/layer7-deploy"
                             for step in self.workflow["jobs"]["deploy"]["steps"]))

    def test_image_version_selection_step_uses_current_candidate_run_identity(self):
        version_step = next(step for step in self.workflow["jobs"]["build"]["steps"]
                            if step.get("id") == "image_version")
        candidate = "a" * 40

        def run_step():
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "github-output"
                env = os.environ.copy()
                env.update({
                    "CANDIDATE_SHA": candidate,
                    "CURRENT_RUN_ID": "9000",
                    "CURRENT_RUN_ATTEMPT": "3",
                    "GITHUB_OUTPUT": str(output),
                })
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", version_step["run"]],
                                        env=env, text=True, capture_output=True, check=False)
                return result, output.read_text() if output.exists() else ""

        result, output = run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"image_version={candidate}-9000-3\n")

    def test_legacy_release_marker_is_passed_to_the_fail_closed_intent_classifier(self):
        steps = self.workflow["jobs"]["tests"]["steps"]
        intent = next(step for step in steps if step.get("id") == "intent")
        self.assertEqual(intent["env"]["EVENT_COMMIT_MESSAGE"], "${{ github.event.head_commit.message || '' }}")
        self.assertIn("--commit-message", intent["run"])
        self.assertNotIn("ARTIFACT_PREPARE", self.workflow["jobs"]["build"]["if"])

    def test_required_checks_cover_release_tooling_and_eros(self):
        self.assertEqual(self.workflow["concurrency"], {
            "group": "gpoint-${{ github.event_name == 'pull_request' && format('pr-{0}', github.event.pull_request.number) || github.event_name == 'workflow_dispatch' && 'manual-production' || 'main-preparation' }}",
            "cancel-in-progress": False,
        })
        tests = self.workflow["jobs"]["tests"]
        self.assertEqual(tests["strategy"], {
            "fail-fast": False,
            "matrix": {"suite": ["workspace", "python"]},
        })
        self.assertEqual(tests["runs-on"], "ubuntu-24.04")
        # These are the exact required contexts enforced by public-main-integrity.
        self.assertEqual(tests["strategy"]["matrix"]["suite"], ["workspace", "python"])
        intent = next(step for step in tests["steps"] if step.get("id") == "intent")
        self.assertNotIn("if", intent)
        self.assertEqual(tests["outputs"], {
            "mode": "${{ steps.intent.outputs.mode }}",
        })
        eros_scope = next(step for step in tests["steps"] if step.get("id") == "eros_scope")
        self.assertEqual(eros_scope.get("if"), "matrix.suite == 'workspace'")
        required_steps = {
            "Validate GitHub Actions workflow": "workspace",
            "Validate pinned Kamal configs and shared proxy routing": "workspace",
            "Run stock Eros SQLx and synthetic conversation E2E": "workspace",
            "Build and typecheck workspace": "workspace",
            "Run Mini App end-to-end tests": "workspace",
            "Run workflow, Native First and Kamal boundary tests": "python",
            "Run isolated PostgreSQL 16 migration and integration suites": "python",
        }
        steps = {step.get("name"): step for step in tests["steps"]}
        for name, suite in required_steps.items():
            self.assertIn(f"matrix.suite == '{suite}'", steps[name]["if"])
        eros = next(step for step in tests["steps"] if step.get("id") == "eros_test_image")
        self.assertIn("matrix.suite == 'workspace'", eros["if"])
        self.assertIn("steps.eros_scope.outputs.required == 'true'", eros["if"])
        e2e = steps["Run stock Eros SQLx and synthetic conversation E2E"]
        self.assertEqual(eros["if"], e2e["if"])
        scope = next(step for step in tests["steps"] if step.get("id") == "eros_scope")
        self.assertIn("steps.filter.outputs.eros_e2e", scope["env"]["PR_EROS_FILTER"])
        self.assertIn("latest-successful-native-sha", scope["run"])
        self.assertIn("eros_e2e_required", scope["run"])
        self.assertNotIn("Run Eros Relay unit tests", steps)
        python = [steps[name] for name, suite in required_steps.items() if suite == "python"]
        for step in python:
            self.assertIn("unset DATABASE_URL SHADOW_DATABASE_URL", step["run"])
        self.assertIn("tests/test_simple_release_architecture.py", python[0]["run"])
        for step in tests["steps"]:
            condition = step.get("if", "")
            self.assertNotIn("matrix.suite == 'release-tooling'", condition)
            self.assertNotIn("matrix.suite == 'eros'", condition)

    def test_release_builds_all_images_without_a_build_scope_planner(self):
        tests = self.workflow["jobs"]["tests"]
        self.assertNotIn("infrastructure_baseline", [step.get("id") for step in tests["steps"]])
        self.assertNotIn("dispatch_build_scope", [step.get("id") for step in tests["steps"]])
        build = self.workflow["jobs"]["build"]
        self.assertNotIn("build_scope", [step.get("id") for step in build["steps"]])
        self.assertNotIn("build_components", build["outputs"])
        self.assertNotIn("deploy_scope", build["outputs"])
        image_steps = [step for step in build["steps"]
                       if step.get("uses", "").startswith("docker/build-push-action@")]
        self.assertEqual({step.get("id") for step in image_steps}, {"app", "miniapp", "eros"})
        self.assertTrue(all("if" not in step for step in image_steps))
        self.assertEqual(build["outputs"]["app_digest"], "${{ steps.app.outputs.digest }}")
        self.assertEqual(build["outputs"]["miniapp_digest"], "${{ steps.miniapp.outputs.digest }}")
        self.assertEqual(build["outputs"]["eros_digest"], "${{ steps.eros.outputs.digest }}")
        live_inventory = next(step for step in self.workflow["jobs"]["deploy"]["steps"]
                              if step.get("id") == "kamal_inventory")
        self.assertIn("verify-kamal-production-baseline.py", live_inventory["run"])

    def test_approved_deploy_dispatch_replaces_automatic_push_infrastructure_gate(self):
        steps = self.workflow["jobs"]["deploy"]["steps"]
        plan = next(step for step in steps if step.get("id") == "component_plan")
        self.assertNotIn("infrastructure_baseline", [step.get("id") for step in steps])
        self.assertNotIn("INFRASTRUCTURE_BASELINE_SHA", plan["env"])
        self.assertNotIn("is_infrastructure_change", plan["run"])
        self.assertIn("github.event_name == 'workflow_dispatch'", self.workflow["jobs"]["deploy"]["if"])
        self.assertIn("github.actor == github.repository_owner", self.workflow["jobs"]["deploy"]["if"])
        self.assertIn("inputs.approve_production == true", self.workflow["jobs"]["deploy"]["if"])

    def test_cumulative_engine_scope_runs_e2e_and_build_requires_same_run_tests_success(self):
        tests = self.workflow["jobs"]["tests"]
        steps = {step.get("id"): step for step in tests["steps"] if step.get("id")}
        self.assertNotIn("dispatch_build_scope", steps)
        self.assertNotIn("infrastructure_baseline", steps)
        self.assertIn("steps.eros_scope.outputs.required == 'true'", steps["eros_test_image"]["if"])
        self.assertEqual(steps["eros_test_image"]["if"], steps["eros_e2e"]["if"])
        self.assertNotIn("eros_e2e", tests["outputs"])
        build = self.workflow["jobs"]["build"]
        self.assertIn("tests", build["needs"])
        self.assertIn("needs.tests.result == 'success'", build["if"])
        self.assertIn("needs.tests.outputs.mode == 'DEPLOY'", build["if"])
        self.assertNotIn("needs.tests.outputs.deploy", build["if"])
        self.assertFalse(any(step.get("name") == "Require exact-candidate Eros test evidence"
                             for step in build["steps"]))

    def test_no_build_job_step_performs_duplicate_actions_run_lookup(self):
        steps = self.workflow["jobs"]["build"]["steps"]
        self.assertFalse(any("actions/runs/" in step.get("run", "") for step in steps))

    def test_main_push_runs_ci_build_and_kamal_production_jobs(self):
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        deploy = self.workflow["jobs"]["deploy"]
        self.assertIn("workflow_dispatch", deploy["if"])
        self.assertNotIn("needs", deploy)
        self.assertNotIn("push", deploy["if"])
        self.assertIn("github.actor == github.repository_owner", deploy["if"])
        self.assertIn("production", deploy["environment"]["name"])
        self.assertNotIn("paths", triggers["push"])
        approval = triggers["workflow_dispatch"]["inputs"]["approve_production"]
        self.assertTrue(approval["required"])
        self.assertEqual(approval["type"], "boolean")
        self.assertIn("--approve-production", "\n".join(
            step.get("run", "") for step in self.workflow["jobs"]["tests"]["steps"]
        ))
        dispatch = next(
            step for step in self.workflow["jobs"]["tests"]["steps"]
            if step.get("id") == "filter"
        )
        self.assertTrue(dispatch["uses"].startswith("dorny/paths-filter@"))

    def test_operational_only_push_and_migration_rehearsal_fail_closed(self):
        gate = self.workflow["jobs"]["tests"]
        self.assertEqual(gate["outputs"]["mode"], "${{ steps.intent.outputs.mode }}")
        self.assertNotIn("deploy", gate["outputs"])
        self.assertIn("scripts/native_release_intent.py", "\n".join(step.get("run", "") for step in gate["steps"]))
        gate_text = "\n".join(step.get("run", "") for step in gate["steps"])
        self.assertNotIn("--backup-maintenance", gate_text)
        self.assertIn('[[ "$MODE" == rollback ]] && args+=(--rollback)', gate_text)
        self.assertNotIn("backup_maintenance", yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())["jobs"])
        self.assertTrue((ROOT / "scripts/gpoint-backup-install.sh").is_file())
        intent = (ROOT / "scripts/native_release_intent.py").read_text()
        self.assertNotIn("PREP_ONLY_SCOPES", intent)
        self.assertNotIn('return "PREPARE_ONLY"', intent)
        self.assertIn('return "BLOCK"', intent)

        deploy = self.workflow["jobs"]["deploy"]
        self.assertIn("workflow_dispatch", deploy["if"])
        self.assertNotIn("needs", deploy)
        rehearsal = yaml.safe_load((ROOT / ".github/workflows/gpoint-operations.yml").read_text())["jobs"]["migration_rehearsal"]
        self.assertEqual(rehearsal["environment"], {"name": "production", "deployment": False})
        self.assertNotIn("deployments", rehearsal["permissions"])
        rehearsal_run = "\n".join(step.get("run", "") for step in rehearsal["steps"])
        self.assertNotIn("repos/$GITHUB_REPOSITORY/deployments", rehearsal_run)
        self.assertIn("--format '{{.Os}} {{.Architecture}}'", rehearsal_run)

    def test_python_job_keeps_gpoint_postgres_security_integration_only(self):
        job = self.workflow["jobs"]["tests"]
        run_steps = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("tests/integration/gpoint-chat-schema-docker.py", run_steps)
        self.assertNotIn("services/gsales/tests/", run_steps)
        self.assertNotIn("gsales-api", run_steps)
        checkout = next(step for step in job["steps"]
                        if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)

    def test_native_workflow_runs_actionlint_before_build(self):
        steps = self.workflow["jobs"]["tests"]["steps"]
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
        self.assertEqual(build["needs"], ["tests"])

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
        for role in ("miniapp",):
            image_step = next(step for step in steps if step.get("id") == role)
            self.assertIn("GPOINT_BUILD_SHA=${{ github.sha }}", image_step["with"]["build-args"])

    def test_production_images_build_independently_and_apply_uses_live_baseline(self):
        build = self.workflow["jobs"]["build"]
        self.assertNotIn("environment", build)
        self.assertEqual(build["needs"], ["tests"])
        steps = build["steps"]
        for step_id in ("app", "miniapp", "eros"):
            image = next(i for i, step in enumerate(steps) if step.get("id") == step_id)
            self.assertNotIn("if", steps[image])
        self.assertNotIn("deployments", build["permissions"])
        self.assertNotIn("actions", build["permissions"])
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
        self.assertIn("for service in api worker bot", steps[preflight]["run"])
        self.assertNotIn("agata-api", steps[preflight]["run"])
        self.assertIn("shared_postgres_1", steps[preflight]["run"])
        self.assertIn("compose.service=$service", steps[preflight]["run"])
        self.assertNotIn("agata-memory-worker", steps[preflight]["run"])

    def test_stock_kamal_version_crosscheck_precedes_authoritative_docker_inspection(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        names = [step.get("name", "") for step in steps]
        cli_index = names.index("Read live application version with Kamal")
        topology_index = names.index("Verify installed Kamal topology before ordinary release")
        self.assertLess(cli_index, topology_index)
        kamal_inventory = steps[cli_index]
        self.assertEqual(kamal_inventory.get("id"), "kamal_inventory")
        commands = kamal_inventory["run"]
        self.assertIn("kamal app version", commands)
        self.assertNotIn("kamal app details", commands)
        self.assertNotIn("kamal app containers", commands)
        self.assertIn("--env KAMAL_REGISTRY_PASSWORD", commands)
        self.assertIn("--env GPOINT_APP_SOURCE_SHA", commands)
        self.assertIn("--env GPOINT_COMMIT_SHA", commands)
        self.assertIn("--platform linux/amd64", commands)
        self.assertIn("--pull=never", commands)
        self.assertIn("Kamal app version command failed for", commands)
        self.assertIn("2>&1", commands)
        self.assertIn("[redacted]", commands)
        self.assertIn("Kamal inventory could not resolve one exact current version", commands)
        self.assertIn("message.splitlines()[-12:]", commands)
        self.assertNotIn("[:300]", commands)
        self.assertIn('>> "$GITHUB_OUTPUT"', commands)
        self.assertIn("config/deploy.yml", commands)
        self.assertNotIn("config/deploy.agata.yml", commands)
        self.assertIn("config/deploy.miniapp.yml", commands)
        topology = steps[topology_index]
        for component in ("core", "miniapp"):
            self.assertTrue(any(
                f"steps.kamal_inventory.outputs.{component}_version" in value
                for value in topology["env"].values()
            ))
        self.assertIn("KAMAL_CORE_VERSION", topology["run"])
        self.assertIn("LIVE_ROLE_INVENTORY", topology["run"])
        self.assertIn("verify-kamal-production-baseline.py", commands)
        self.assertIn("RepoDigests", (ROOT / "scripts/verify-kamal-production-baseline.py").read_text())

    def test_pinned_kamal_image_is_pulled_and_digest_platform_checked_before_safe_config_test(self):
        tests_steps = self.workflow["jobs"]["tests"]["steps"]
        validate = next(step for step in tests_steps
                        if step.get("name") == "Validate pinned Kamal configs and shared proxy routing")
        helper = (ROOT / "scripts/check-kamal-configs.sh").read_text()
        self.assertIn("docker pull --platform linux/amd64", helper)
        self.assertIn("docker image inspect", helper)
        self.assertIn("linux/amd64", helper)
        self.assertIn("ghcr.io/basecamp/kamal@sha256:7a3893b47dd328955f86fbf9ba887d08d612b74f96867f077750a53312537625", helper)
        self.assertIn("./scripts/check-kamal-configs.sh", validate["run"])
        self.assertIn("--pull=never", helper)
        self.assertIn("--network none", helper)
        deploy_steps = self.workflow["jobs"]["deploy"]["steps"]
        deploy_pull = next(i for i, step in enumerate(deploy_steps)
                           if step.get("name") == "Pull and verify pinned Kamal CLI image")
        inventory = next(i for i, step in enumerate(deploy_steps)
                         if step.get("id") == "kamal_inventory")
        self.assertLess(deploy_pull, inventory)
        self.assertIn("docker pull --platform linux/amd64", deploy_steps[deploy_pull]["run"])

    def test_ordinary_deployment_success_requires_verified_manifest(self):
        deploy = self.workflow["jobs"]["deploy"]
        steps = deploy["steps"]
        verify = next(i for i, step in enumerate(steps) if step.get("name") == "Verify public routes and exact running services")
        resolve = next(i for i, step in enumerate(steps) if step.get("name") == "Resolve exact GitHub environment Deployment ID")
        self.assertLess(resolve, verify)
        self.assertIn("manifest_sha", steps[verify]["run"])
        self.assertIn("environment_url=https://gpoint.one/#gpoint-manifest-sha256=$manifest_sha", steps[verify]["run"])
        self.assertIn("steps.verify.outputs.environment_url", deploy["environment"]["url"])
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
        self.assertNotIn("needs", deploy)
        self.assertTrue(all(name in self.workflow["jobs"]["build"]["outputs"]
                            for name in ("app_digest", "miniapp_digest", "eros_digest")))
        self.assertNotIn("gsales_digest", self.workflow["jobs"]["build"]["outputs"])
        deploy_text = yaml.safe_dump(deploy)
        self.assertIn("steps.prepared.outputs.core_digest", deploy_text)
        self.assertNotIn("needs.build.outputs.agata_digest", deploy_text)
        self.assertNotIn("needs.build.outputs.gsales_digest", deploy_text)
        self.assertIn("steps.prepared.outputs.miniapp_digest", deploy_text)
        self.assertIn("steps.prepared.outputs.eros_digest", deploy_text)
        self.assertNotRegex(script, r"docker\s+compose\s+(up|down|pull|restart)")
        self.assertIn("kamal redeploy --skip-push", script)
        self.assertNotIn("kamal deploy --skip-push", script)
        self.assertIn("GPOINT_KAMAL_VERIFY=PASS", script)

    def test_eros_uses_exact_stock_artifact_and_existing_selective_kamal_chain(self):
        workflow_text = WORKFLOW.read_text()
        jobs = self.workflow["jobs"]
        build = jobs["build"]
        eros_build = next(step for step in build["steps"] if step.get("id") == "eros")
        self.assertEqual(eros_build["with"]["context"],
                         "https://github.com/etherfunlab/eros-engine.git#${{ env.EROS_UPSTREAM_SHA }}")
        self.assertEqual(eros_build["with"]["platforms"], "linux/amd64")
        self.assertTrue(eros_build["with"]["push"])
        self.assertIn("sbom: true", yaml.safe_dump(eros_build["with"]))
        self.assertIn("provenance: mode=max", yaml.safe_dump(eros_build["with"]))
        self.assertIn("verify_native_eros_image.py", workflow_text)
        self.assertIn("services/eros-engine/upstream.sha", workflow_text)
        self.assertIn("config/deploy.eros.yml", workflow_text)
        self.assertIn("DEPLOY_EROS", workflow_text)
        self.assertIn("gpoint-eros-pilot-engine", workflow_text)
        self.assertNotIn("gpoint-eros-relay-relay", workflow_text)
        self.assertIn('image: akhripunov/gpoint-eros', (ROOT / "config/deploy.eros.yml").read_text())
        self.assertIn('--eros-digest "$eros_digest"', workflow_text)
        self.assertNotIn("docker compose", workflow_text)

    def test_no_mac_runner_or_release_controller_is_used(self):
        text = WORKFLOW.read_text()
        self.assertNotIn("[self-hosted, macOS, gpoint-production]", text)
        self.assertNotIn("gpoint-owner-mac", text)
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
        self.assertNotIn("gpoint-agata", deploy_script)
        hook = (ROOT / ".kamal/hooks/post-app-boot").read_text()
        self.assertTrue(hook.startswith("#!/bin/sh\n"))
        self.assertIn('case "$version" in "$candidate"-*)', hook)

    def test_workspace_ci_runs_real_kamal_config_and_proxy_integration_checks(self):
        steps = self.workflow["jobs"]["tests"]["steps"]
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
        for config in ("deploy.yml", "deploy.miniapp.yml", "deploy.eros.yml",
                       "deploy.ollama.yml", "deploy.eros-api.yml"):
            self.assertIn(f"config/{config}", config_check)
        self.assertIn("config-validation-placeholder", config_check)
        self.assertIn("Kamal config validation failed", config_check)

    def test_deploy_manifest_validator_accepts_exact_artifact_and_rejects_mismatches(self):
        deploy = self.workflow["jobs"]["deploy"]
        validator = next(step for step in deploy["steps"] if step.get("id") == "prepared")
        candidate = "a" * 40
        upstream = "b" * 40
        digests = {name: "sha256:" + char * 64 for name, char in
                   (("core", "1"), ("miniapp", "2"), ("eros", "3"))}

        def run_case(mutate=None, corrupt_checksum=False):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                prepared = root / "gpoint-prepared"
                (prepared / "services/eros-engine").mkdir(parents=True)
                (prepared / "services/eros-engine/upstream.sha").write_text(upstream + "\n")
                images = {
                    "core": {"repository": "ghcr.io/akhripunov/gpoint-runtime", "version": f"{candidate}-9000-2", "digest": digests["core"], "sourceRevision": candidate},
                    "miniapp": {"repository": "ghcr.io/akhripunov/gpoint-miniapp", "version": f"{candidate}-9000-2", "digest": digests["miniapp"], "sourceRevision": candidate},
                    "eros": {"repository": "ghcr.io/akhripunov/gpoint-eros", "version": f"{candidate}-9000-2", "digest": digests["eros"], "sourceRevision": candidate, "upstreamRevision": upstream},
                }
                manifest = {
                    "schema": "gpoint-release-preparation/v1", "repository": "akhripunov/gpoint", "sha": candidate,
                    "sourceRun": {"id": 9000, "attempt": 2, "event": "push", "branch": "main", "conclusion": "success"},
                    "requiredChecks": [{"name": name, "conclusion": "success"} for name in
                                       ("Tests (workspace)", "Tests (python)", "Build and publish immutable images")],
                    "images": images, "imageVersion": f"{candidate}-9000-2", "readBack": "success",
                }
                if mutate:
                    mutate(manifest)
                manifest_path = prepared / "gpoint-release-manifest.json"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
                digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                if corrupt_checksum:
                    digest = "0" * 64
                (prepared / "gpoint-release-manifest.sha256").write_text(
                    f"{digest}  gpoint-release-manifest.json\n")
                output, envfile = root / "output", root / "env"
                env = os.environ.copy()
                env.update({"RUNNER_TEMP": str(root), "CANDIDATE_SHA": candidate, "SOURCE_RUN_ID": "9000",
                            "SOURCE_RUN_ATTEMPT": "2", "SOURCE_ARTIFACT_DIGEST": "sha256:" + "4" * 64,
                            "REPOSITORY": "akhripunov/gpoint", "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(envfile)})
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", validator["run"]],
                                        env=env, capture_output=True, text=True, check=False)
                return result, output.read_text() if output.exists() else "", envfile.read_text() if envfile.exists() else ""

        result, outputs, envfile = run_case()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"core_digest={digests['core']}", outputs)
        self.assertIn(f"miniapp_digest={digests['miniapp']}", outputs)
        self.assertIn(f"eros_digest={digests['eros']}", outputs)
        self.assertIn(f"IMAGE_VERSION={candidate}-9000-2", envfile)

        failures = [
            (lambda manifest: manifest.update(sha="c" * 40), False),
            (lambda manifest: manifest["sourceRun"].update(attempt=3), False),
            (lambda manifest: manifest["requiredChecks"][0].update(conclusion="failure"), False),
            (lambda manifest: manifest["images"]["core"].update(digest="latest"), False),
            (lambda manifest: manifest["images"].pop("miniapp"), False),
            (lambda manifest: manifest["images"]["eros"].update(upstreamRevision="c" * 40), False),
            (None, True),
        ]
        for mutate, corrupt_checksum in failures:
            with self.subTest(mutate=mutate, corrupt_checksum=corrupt_checksum):
                rejected, _, _ = run_case(mutate, corrupt_checksum)
                self.assertNotEqual(rejected.returncode, 0)

    def test_deploy_only_eros_evidence_reads_exact_preparation_attempt(self):
        deploy = self.workflow["jobs"]["deploy"]
        evidence = next(step for step in deploy["steps"] if step.get("id") == "eros_e2e_evidence")
        command = evidence["run"]
        self.assertIn("steps.prepared_run.outputs.run_id", str(evidence["env"]))
        self.assertIn("steps.prepared_run.outputs.run_attempt", str(evidence["env"]))
        self.assertIn("$SOURCE_RUN_ID/attempts/$SOURCE_RUN_ATTEMPT/jobs", command)
        self.assertNotIn("$GITHUB_RUN_ID/jobs", command)
        self.assertIn('outcome not in {"success", "skipped"}', command)
        self.assertIn("outcome is None", command)
        self.assertIn('outcome in {"failure", "cancelled"}', command)

        def run_case(conclusion="success", *, include_workspace=True, include_step=True,
                     workspace_conclusion="success"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fake_gh = root / "gh"
                payload = {"jobs": []}
                if include_workspace:
                    job = {"name": "Tests (workspace)", "status": "completed",
                           "conclusion": workspace_conclusion, "steps": []}
                    if include_step:
                        job["steps"].append({"name": "Run stock Eros SQLx and synthetic conversation E2E",
                                             "conclusion": conclusion})
                    payload["jobs"].append(job)
                fake_gh.write_text("#!/bin/sh\ncase \"$*\" in *'/runs/9000/attempts/2/jobs?per_page=100'*) ;; *) exit 71 ;; esac\nprintf '%s\\n' '" + json.dumps(payload) + "'\n")
                fake_gh.chmod(0o700)
                output, summary = root / "output", root / "summary"
                env = os.environ.copy()
                env.update({"PATH": str(root) + os.pathsep + env.get("PATH", ""),
                            "GITHUB_REPOSITORY": "akhripunov/gpoint", "SOURCE_RUN_ID": "9000",
                            "SOURCE_RUN_ATTEMPT": "2", "GITHUB_OUTPUT": str(output),
                            "GITHUB_STEP_SUMMARY": str(summary)})
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", command], env=env,
                                        text=True, capture_output=True, check=False)
                return result, output.read_text() if output.exists() else ""

        result, output = run_case("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "outcome=success\n")
        result, output = run_case("skipped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "outcome=skipped\n")

        for args in (("failure",), ("cancelled",), (None,), ("missing",),
                     ("success", False, True, "failure")):
            with self.subTest(args=args):
                if len(args) == 4:
                    rejected, output = run_case(args[0], include_workspace=args[1], include_step=args[2],
                                                workspace_conclusion=args[3])
                else:
                    rejected, output = run_case(args[0], include_step=args[0] != "missing")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(output, "")

        rejected, output = run_case("success", include_workspace=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(output, "")

    def test_complete_workflow_routes_pr_push_and_deploy_dispatch(self):
        events = self.workflow.get("on", self.workflow.get(True))
        jobs = self.workflow["jobs"]
        self.assertEqual(events["pull_request"]["branches"], ["main"])
        self.assertEqual(events["push"]["branches"], ["main"])
        self.assertEqual(jobs["tests"]["if"], "github.event_name != 'workflow_dispatch'")
        self.assertEqual(set(jobs["tests"]["outputs"]), {"mode"})
        build_if = jobs["build"]["if"]
        self.assertIn("github.event_name == 'push'", build_if)
        self.assertIn("github.ref == 'refs/heads/main'", build_if)
        self.assertIn("needs.tests.result == 'success'", build_if)
        self.assertIn("needs.tests.outputs.mode == 'DEPLOY'", build_if)
        self.assertNotIn("needs.tests.outputs.deploy", build_if)
        self.assertNotIn("pull_request", build_if)
        deploy = jobs["deploy"]
        self.assertNotIn("needs", deploy)
        self.assertIn("workflow_dispatch", deploy["if"])
        self.assertIn("approve_production == true", deploy["if"])
        self.assertIn("github.actor == github.repository_owner", deploy["if"])
        deploy_yaml = yaml.safe_dump(deploy)
        self.assertNotIn("needs.tests", deploy_yaml)
        self.assertNotIn("needs.build", deploy_yaml)
        self.assertNotIn("GITHUB_RUN_ID/jobs", deploy_yaml)
        self.assertNotIn("github.event_name == 'push'", deploy_yaml)
        self.assertNotIn("infrastructure_baseline", deploy_yaml)
