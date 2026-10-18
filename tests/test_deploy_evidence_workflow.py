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


class DeployEvidenceWorkflowTests(unittest.TestCase):
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
            script = script.replace(
                'printf \'%s\' "$GHCR_TOKEN" | oras login ghcr.io --username "$GHCR_USERNAME" --password-stdin',
                ": # local OCI layout test; no registry credentials",
            )
            script = re.sub(r'^ref="ghcr\.io/[^\n]+"$',
                            f'ref="{layout}:deploy-evidence-test"', script, flags=re.MULTILINE)
            script = script.replace("output=$(oras push --artifact-type", "output=$(oras push --oci-layout --artifact-type")
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
