import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-deploy.yml"
ARTIFACT_TYPE = "application/vnd.gpoint.deploy.evidence.v1"
LAYER_TYPE = "application/vnd.gpoint.deploy.evidence.v1+json"


def publisher_script():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    step = next(step for step in steps
                if step.get("name") == "Publish deployment result as a GHCR OCI artifact")
    return step["run"]


def reconciliation_publisher_script():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["reconcile-authorization"]["steps"]
    step = next(step for step in steps
                if step.get("name") == "Publish reconciliation attestation as a GHCR OCI artifact")
    return step["run"]


def linear_production_publish_script():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    step = next(step for step in steps
                if step.get("name") == "Publish production result to GPoint Linear")
    return step["run"]


class DeployEvidenceWorkflowTests(unittest.TestCase):
    def test_native_deploy_job_requires_exact_sha_and_production_authorization_gates(self):
        native = yaml.safe_load((ROOT / '.github/workflows/gpoint-native-release.yml').read_text())
        triggers = native.get('on', native.get(True))
        self.assertIn('workflow_dispatch', triggers)
        self.assertIn('candidate_sha', triggers['workflow_dispatch']['inputs'])
        deploy = native['jobs']['deploy']
        self.assertEqual(deploy['if'],
                         "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'")
        script = '\n'.join(step.get('run', '') for step in deploy['steps'])
        self.assertIn('REQUESTED_SHA', script)
        owner_gate = next(step for step in deploy['steps']
                          if step.get('name') == 'Recheck owner authorization and current main for production mode')
        self.assertEqual(owner_gate['env']['APPROVE_PRODUCTION'], '${{ inputs.approve_production }}')
        self.assertIn('OWNER_APPROVAL_ACTOR', owner_gate['env'])
        self.assertIn('OWNER_TRIGGERING_ACTOR', owner_gate['env'])
        self.assertIn('OWNER_REPOSITORY', owner_gate['env'])
        self.assertEqual(deploy['environment'], 'production')
        self.assertEqual(deploy['concurrency']['group'], 'gpoint-production-release')
        self.assertIn('deployments', deploy.get('permissions', {}))
        self.assertIn('Create exact-SHA in-progress GitHub Deployment', [step.get('name') for step in deploy['steps']])
        names = [step.get('name') for step in deploy['steps']]
        self.assertLess(names.index('Verify public routes and exact running services'),
                        names.index('Record GitHub Deployment result against the same attempt'))

    def test_authorization_reconciliation_is_a_separate_owner_bound_metadata_only_job(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        dispatch = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertIn("reconcile-authorization", dispatch["mode"]["options"])
        self.assertIn("deployment_id", dispatch)
        validate = workflow["jobs"]["validate"]
        validate_names = {step.get("name"): step for step in validate["steps"]}
        owner_guard = validate_names["Require repository owner for authorization reconciliation"]
        self.assertEqual(validate["steps"][0], owner_guard)
        self.assertIn("OWNER_APPROVAL_ACTOR", owner_guard["run"])
        self.assertIn("OWNER_TRIGGERING_ACTOR", owner_guard["run"])
        self.assertIn("OWNER_REPOSITORY", owner_guard["run"])
        self.assertEqual(validate_names["Verify operator confirmation, current main and successful preparation"]["if"],
                         "inputs.mode != 'reconcile-authorization'")
        validation = validate_names["Validate existing Deployment authorization reconciliation"]
        self.assertIn("--validate-authorization", validation["run"])
        self.assertIn("--deployment-id", validation["run"])
        self.assertIn("OWNER_APPROVAL_ACTOR", validation["env"])
        self.assertIn("OWNER_TRIGGERING_ACTOR", validation["env"])
        self.assertIn("OWNER_REPOSITORY", validation["env"])
        self.assertIn("original_run_id", validate["outputs"]["original_run_id"])

        self.assertEqual(workflow["jobs"]["deploy"]["if"],
                         "inputs.mode != 'reconcile-authorization' && inputs.mode != 'eros-pilot' && "
                         "inputs.mode != 'eros-acl-plan' && inputs.mode != 'eros-acl-apply' && "
                         "inputs.mode != 'eros-acl-rollback' && inputs.mode != 'eros-acl-reopen'")
        reconcile = workflow["jobs"]["reconcile-authorization"]
        self.assertEqual(reconcile["if"], "inputs.mode == 'reconcile-authorization'")
        self.assertEqual(reconcile["runs-on"], ["self-hosted", "macOS", "gpoint-production"])
        self.assertEqual(reconcile["permissions"], {"contents": "write", "actions": "read",
                                                     "packages": "write", "deployments": "write"})
        reconcile_steps = {step.get("name"): step for step in reconcile["steps"]}
        finalize = reconcile_steps["Finalize authorization on the existing Deployment"]
        self.assertIn("--finalize-authorization", finalize["run"])
        self.assertIn("--deployment-id", finalize["run"])
        self.assertIn("GITHUB_TOKEN", finalize["env"])
        self.assertIn("GHCR_READ_TOKEN", finalize["env"])
        self.assertIn("OWNER_APPROVAL_ACTOR", finalize["env"])
        self.assertIn("OWNER_TRIGGERING_ACTOR", finalize["env"])
        readiness = reconcile_steps["Verify assigned runner before authorization reconciliation"]
        self.assertEqual(readiness["env"]["CANDIDATE_SHA"], "${{ github.sha }}")
        self.assertNotIn("automated-release.mjs", "\n".join(step.get("run", "") for step in reconcile["steps"]))
        self.assertNotIn("release:gpoint", "\n".join(step.get("run", "") for step in reconcile["steps"]))
        self.assertNotIn("--apply", finalize["run"])
        self.assertNotIn("--confirm-production-apply", finalize["run"])
        self.assertEqual(reconcile_steps["Set up ORAS for immutable reconciliation evidence"]["with"]["version"], "1.3.0")
        self.assertIn("Publish reconciliation attestation as a GHCR OCI artifact", reconcile_steps)

        publish = workflow["jobs"]["publish"]
        self.assertIn("reconcile-authorization", publish["needs"])
        publish_steps = {step.get("name"): step for step in publish["steps"]}
        self.assertEqual(publish_steps["Publish preflight result to GPoint Linear"]["if"],
                         "always() && inputs.mode != 'reconcile-authorization' && inputs.mode != 'eros-pilot' && "
                         "inputs.mode != 'eros-acl-plan' && inputs.mode != 'eros-acl-apply' && "
                         "inputs.mode != 'eros-acl-rollback' && inputs.mode != 'eros-acl-reopen'")
        self.assertIn("reconcile-authorization", publish_steps["Publish production result to GPoint Linear"]["if"])

    def test_oras_is_available_before_production_parent_oci_verification(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        steps = workflow["jobs"]["deploy"]["steps"]
        names = [step.get("name") for step in steps]
        installer = "Set up ORAS for immutable deployment evidence"
        verifier = "Verify receipt, canonical preflight, apply and persist evidence"
        publisher = "Publish deployment result as a GHCR OCI artifact"
        self.assertEqual(names.count(installer), 1)
        self.assertLess(names.index("Resolve and verify exact Prepare receipt from GHCR"),
                        names.index(installer))
        self.assertLess(names.index(installer), names.index(verifier))
        self.assertLess(names.index(installer), names.index(publisher))
        self.assertEqual(steps[names.index(installer)]["with"]["version"], "1.3.0")
        reconcile_steps = workflow["jobs"]["reconcile-authorization"]["steps"]
        self.assertEqual(next(step for step in reconcile_steps
                              if step.get("name") == "Set up ORAS for immutable reconciliation evidence")["with"]["version"], "1.3.0")

    def test_only_trusted_mac_production_jobs_have_contents_write_for_authorization_consumption(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        deploy = workflow["jobs"]["deploy"]
        self.assertEqual(deploy["runs-on"], ["self-hosted", "macOS", "gpoint-production"])
        self.assertEqual(deploy["permissions"].get("contents"), "write")
        self.assertEqual(workflow["jobs"]["reconcile-authorization"]["permissions"].get("contents"), "write")
        self.assertEqual(workflow["jobs"]["validate"].get("permissions", workflow["permissions"]).get("contents"), "read")
        self.assertEqual(workflow["jobs"]["publish"]["permissions"].get("contents", "read"), "read")
        for name, job in workflow["jobs"].items():
            if job.get("permissions", workflow["permissions"]).get("contents") == "write":
                self.assertEqual(job["runs-on"], ["self-hosted", "macOS", "gpoint-production"], name)
        self.assertIn("consumeTaskAuthorization", (ROOT / "scripts/automated-release.mjs").read_text())

    def test_linear_status_selector_preserves_evidenced_failures_and_gates_complete(self):
        script = linear_production_publish_script()
        with tempfile.TemporaryDirectory(prefix="gpoint-deploy-linear-status-") as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            python = fake_bin / "python3"
            python.write_text("#!/bin/sh\nprintf '%s' \"$RELEASE_STATUS\" > \"$STATUS_CAPTURE\"\n")
            python.chmod(0o755)
            capture = root / "status"
            valid_digest = "sha256:" + "a" * 64
            cases = [
                ("COMPLETE", "success", valid_digest, "COMPLETE"),
                ("COMPLETE", "failure", valid_digest, "INSTALLATION_UNCONFIRMED"),
                ("COMPLETE", "success", "", "INSTALLATION_UNCONFIRMED"),
                ("COMPLETE", "success", "sha256:bad", "INSTALLATION_UNCONFIRMED"),
                ("FAILED", "failure", valid_digest, "FAILED"),
                ("BLOCKED", "failure", valid_digest, "BLOCKED"),
                ("FAILED", "failure", "", "INSTALLATION_UNCONFIRMED"),
                ("UNCONFIRMED", "failure", valid_digest, "INSTALLATION_UNCONFIRMED"),
            ]
            for raw_status, outcome, digest, expected in cases:
                env = os.environ.copy()
                env.update({"DEPLOY_STATUS": raw_status, "DEPLOY_EXECUTE_OUTCOME": outcome,
                            "DEPLOY_EVIDENCE_DIGEST": digest, "STATUS_CAPTURE": str(capture),
                            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root,
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(capture.read_text(), expected, (raw_status, outcome, digest))

    def test_evidence_publisher_uses_and_cleans_private_oras_registry_config(self):
        script = publisher_script()
        with tempfile.TemporaryDirectory(prefix="gpoint-deploy-evidence-config-") as tmp:
            root = Path(tmp)
            release_dir = root / "evidence"
            release_dir.mkdir()
            filename = "gpoint-release-result-101-1.json"
            (release_dir / filename).write_text('{"schema":1}\n')
            fake_bin = root / "bin"
            fake_bin.mkdir()
            trace = root / "oras-trace.jsonl"
            fake_oras = fake_bin / "oras"
            fake_oras.write_text("""#!/usr/bin/env python3
import json, os, pathlib, stat, sys
args = sys.argv[1:]
command = args[0]
index = args.index('--registry-config')
config = pathlib.Path(args[index + 1])
assert config.is_file()
assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
assert stat.S_IMODE(config.stat().st_mode) == 0o600
body = json.loads(config.read_text())
assert 'credsStore' not in body and 'credHelpers' not in body
if command == 'login':
    assert 'ghcr.io' in args
    assert args[args.index('--username') + 1] == 'synthetic-test'
    sys.stdin.read()  # Consume but never record the synthetic password.
    config.write_text(json.dumps({'auths': {'ghcr.io': {'auth': 'synthetic'}}}))
elif command == 'push':
    assert body.get('auths', {}).get('ghcr.io', {}).get('auth') == 'synthetic'
    print(json.dumps({'digest': 'sha256:' + 'a' * 64}))
else:
    raise AssertionError(command)
with open(os.environ['ORAS_TRACE'], 'a') as trace:
    trace.write(json.dumps({'command': command, 'config': str(config), 'args': args}) + '\\n')
if os.environ.get('ORAS_FAIL_COMMAND') == command:
    print('synthetic ORAS failure', file=sys.stderr)
    raise SystemExit(17)
""")
            fake_oras.chmod(0o755)
            output = root / "github-output"
            env = os.environ.copy()
            env.update({
                "RELEASE_RESULT": str(release_dir / filename),
                "RUNNER_TEMP": str(release_dir),
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1",
                "CANDIDATE_SHA": "a" * 40,
                "GHCR_USERNAME": "synthetic-test",
                "GHCR_TOKEN": "synthetic-only-token",
                "GITHUB_OUTPUT": str(output),
                "ORAS_TRACE": str(trace),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            })
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([call['command'] for call in calls], ['login', 'push'])
            config_paths = {call['config'] for call in calls}
            self.assertEqual(len(config_paths), 1)
            self.assertFalse(Path(next(iter(config_paths))).exists())
            self.assertNotIn("synthetic-only-token", result.stdout + result.stderr)
            self.assertNotIn("synthetic-only-token", trace.read_text())

            for failed_command in ("login", "push"):
                env["ORAS_FAIL_COMMAND"] = failed_command
                failed = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root,
                                         env=env, capture_output=True, text=True)
                self.assertEqual(failed.returncode, 17, failed.stdout + failed.stderr)
                self.assertNotIn("synthetic-only-token", failed.stdout + failed.stderr)
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual(calls[-1]["command"], failed_command)
                self.assertFalse(Path(calls[-1]["config"]).exists())

    def test_reconciliation_publishes_attestation_under_new_run_attempt_tag(self):
        script = reconciliation_publisher_script()
        with tempfile.TemporaryDirectory(prefix="gpoint-reconcile-evidence-") as tmp:
            root = Path(tmp)
            release_dir = root / "evidence"
            release_dir.mkdir()
            filename = "gpoint-release-result-202-2.json"
            (release_dir / filename).write_text('{"schema":1,"reconciliation":true}\n')
            fake_bin = root / "bin"
            fake_bin.mkdir()
            trace = root / "oras.log"
            fake_oras = fake_bin / "oras"
            fake_oras.write_text("#!/bin/sh\nif [ \"$1\" = login ]; then cat >/dev/null; elif [ \"$1\" = push ]; then printf '{\\\"digest\\\":\\\"sha256:%064d\\\"}\\n' 0; else exit 9; fi\nprintf '%s\\n' \"$*\" >> \"$ORAS_TRACE\"\n")
            fake_oras.chmod(0o755)
            output = root / "github-output"
            env = os.environ.copy()
            env.update({"RELEASE_RESULT": str(release_dir / filename), "RUNNER_TEMP": str(release_dir),
                        "GITHUB_RUN_ID": "202", "GITHUB_RUN_ATTEMPT": "2", "CANDIDATE_SHA": "a" * 40,
                        "GHCR_USERNAME": "owner", "GHCR_TOKEN": "synthetic-secret", "GITHUB_OUTPUT": str(output),
                        "ORAS_TRACE": str(trace), "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            trace_text = trace.read_text()
            self.assertIn("deploy-" + "a" * 40 + "-202-2", trace_text)
            self.assertIn(filename + ":" + LAYER_TYPE, trace_text)
            self.assertNotIn("synthetic-secret", result.stdout + result.stderr + trace_text)
            self.assertEqual(output.read_text(), "digest=sha256:" + "0" * 64 + "\n")

    def test_real_workflow_push_uses_a_validated_relative_file_ref(self):
        script = publisher_script()
        self.assertIn('expected_result="$RUNNER_TEMP/gpoint-release-result-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.json"', script)
        self.assertIn('[ "$RELEASE_RESULT" != "$expected_result" ]', script)
        self.assertIn('[ -L "$RELEASE_RESULT" ]', script)
        self.assertIn('result_dir=$(dirname -- "$RELEASE_RESULT")', script)
        self.assertIn('result_file=$(basename -- "$RELEASE_RESULT")', script)
        self.assertIn('cd -- "$result_dir"', script)
        self.assertIn(f'"$result_file:{LAYER_TYPE}"', script)
        self.assertNotIn(f'"$RELEASE_RESULT:{LAYER_TYPE}"', script)
        self.assertIn(f'--artifact-type {ARTIFACT_TYPE}', script)
        self.assertNotIn("--disable-path-validation", script)
        self.assertIn("DEPLOY_EVIDENCE_DIGEST=$digest", script)

    def test_workflow_fails_closed_for_a_result_path_outside_the_run_temp_file(self):
        script = publisher_script()
        with tempfile.TemporaryDirectory(prefix="gpoint-deploy-evidence-path-") as tmp:
            root = Path(tmp)
            actual = root / "elsewhere.json"
            actual.write_text("synthetic evidence")
            env = os.environ.copy()
            env.update({
                "RELEASE_RESULT": str(actual),
                "RUNNER_TEMP": str(root),
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1",
                "GHCR_USERNAME": "synthetic-test",
                "GHCR_TOKEN": "must-not-be-printed",
                "GITHUB_OUTPUT": str(root / "github-output"),
            })
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root,
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe deployment evidence path", result.stdout)
            self.assertNotIn("must-not-be-printed", result.stdout + result.stderr)

    def test_extracted_workflow_push_round_trips_with_the_pinned_oras_binary(self):
        oras = os.environ.get("ORAS_1_3_BIN") or shutil.which("oras")
        if not oras:
            self.skipTest("ORAS is not installed in this execution context")
        version = subprocess.run([oras, "version"], check=True, capture_output=True, text=True).stdout
        expected_version = os.environ.get("ORAS_EXPECT_VERSION", "1.3.0")
        if not re.search(rf"Version:\s*{re.escape(expected_version)}\b", version):
            self.skipTest(f"the available ORAS binary is not v{expected_version}")

        script = publisher_script()
        with tempfile.TemporaryDirectory(prefix="gpoint-deploy-evidence-oras-") as tmp:
            root = Path(tmp)
            release_dir = root / "evidence"
            release_dir.mkdir()
            filename = "gpoint-release-result-101-1.json"
            sent = b'{"schema":1,"candidateSha":"' + b"a" * 40 + b'"}\n'
            (release_dir / filename).write_bytes(sent)
            layout = root / "layout"
            output_path = root / "github-output"

            # Execute the actual workflow shell block, swapping only GHCR login and
            # the remote destination for an isolated local OCI layout. Keep the
            # workflow's file reference, artifact type, media type and digest parsing.
            script, login_count = re.subn(
                r'printf \'%s\' "\$GHCR_TOKEN" \| oras login --registry-config "\$oras_config" ghcr.io --username "\$GHCR_USERNAME" --password-stdin',
                ": # local OCI layout test; no registry credentials",
                script,
            )
            self.assertEqual(login_count, 1, "expected exactly one isolated GHCR login command")
            script = re.sub(r'^ref="ghcr\.io/[^\n]+"$',
                            f'ref="{layout}:deploy-evidence-test"', script, flags=re.MULTILINE)
            script, push_count = re.subn(
                r'output=\$\(oras push --registry-config "\$oras_config" --artifact-type',
                'output=$(oras push --registry-config "$oras_config" --oci-layout --artifact-type',
                script,
            )
            self.assertEqual(push_count, 1, "expected exactly one registry-configured ORAS push command")
            self.assertIn('--registry-config "$oras_config"', script)
            self.assertIn("--oci-layout", script)
            self.assertNotIn("$RELEASE_RESULT:", script)

            env = os.environ.copy()
            env.update({
                "RELEASE_RESULT": str(release_dir / filename),
                "RUNNER_TEMP": str(release_dir),
                "CANDIDATE_SHA": "a" * 40,
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "1",
                "GHCR_USERNAME": "synthetic-test",
                "GHCR_TOKEN": "not-used",
                "GITHUB_OUTPUT": str(output_path),
                "PATH": f"{Path(oras).resolve().parent}{os.pathsep}{os.environ.get('PATH', '')}",
            })
            subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=root, env=env,
                           check=True, capture_output=True, text=True)
            digest = next(line.split("=", 1)[1] for line in output_path.read_text().splitlines()
                          if line.startswith("digest="))
            self.assertRegex(digest, r"^sha256:[a-f0-9]{64}$")

            pulled = root / "pulled"
            pulled.mkdir()
            subprocess.run([oras, "pull", "--oci-layout", f"{layout}@{digest}", "-o", str(pulled)],
                           cwd=root, check=True, capture_output=True, text=True)
            self.assertEqual(list(path.name for path in pulled.iterdir()), [filename])
            self.assertEqual((pulled / filename).read_bytes(), sent)

            manifest = subprocess.run([oras, "manifest", "fetch", "--oci-layout",
                                       f"{layout}@{digest}"], cwd=root, check=True,
                                      capture_output=True, text=True)
            manifest_bytes = manifest.stdout.encode()
            parsed = json.loads(manifest_bytes)
            self.assertEqual("sha256:" + hashlib.sha256(manifest_bytes).hexdigest(), digest)
            self.assertEqual(parsed["mediaType"], "application/vnd.oci.image.manifest.v1+json")
            self.assertEqual(parsed["artifactType"], ARTIFACT_TYPE)
            self.assertEqual(parsed["layers"][0]["mediaType"], LAYER_TYPE)
            self.assertEqual(parsed["layers"][0]["digest"],
                             "sha256:" + hashlib.sha256(sent).hexdigest())
            self.assertEqual(parsed["layers"][0]["size"], len(sent))


if __name__ == "__main__":
    unittest.main()
