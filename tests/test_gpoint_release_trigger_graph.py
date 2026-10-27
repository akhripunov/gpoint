from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def events(workflow):
    return workflow.get("on", workflow.get(True, {}))


def load_workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


class GPointReleaseTriggerGraphTests(unittest.TestCase):
    def test_legacy_prepare_and_deploy_are_manual_recovery_only(self):
        prepare = load_workflow("gpoint-prepare.yml")
        deploy = load_workflow("gpoint-deploy.yml")

        self.assertEqual(set(events(prepare)), {"workflow_dispatch"})
        self.assertEqual(set(events(deploy)), {"workflow_dispatch"})
        self.assertEqual(prepare["jobs"]["coalesce"]["if"], "github.ref == 'refs/heads/main'")
        self.assertIn("github.ref == 'refs/heads/main'", prepare["jobs"]["prepare"]["if"])
        deploy_source = (WORKFLOWS / "gpoint-deploy.yml").read_text()
        self.assertNotIn("trusted automatic controller", deploy_source)
        self.assertNotIn("Automatic release controller run", deploy_source)
        self.assertIn(
            "GPoint — Prepare",
            events(load_workflow("gpoint-linear-reconcile.yml"))["workflow_run"]["workflows"],
        )
        for name in (
            "gpoint-prepare.yml",
            "gpoint-native-release.yml",
            "gpoint-first-kamal-cutover.yml",
        ):
            source = (WORKFLOWS / name).read_text()
            self.assertTrue(
                "tests/test_gpoint_release_trigger_graph.py" in source
                or "tests.test_gpoint_release_trigger_graph" in source,
                f"{name} must run the release trigger graph regression test",
            )

    def test_main_push_cannot_reach_legacy_production_deploy(self):
        all_workflows = {
            path.name: yaml.safe_load(path.read_text())
            for path in WORKFLOWS.glob("*.yml")
        }
        self.assertNotIn("gpoint-auto-preflight.yml", all_workflows)

        native = all_workflows["gpoint-native-release.yml"]
        native_events = events(native)
        self.assertEqual(native_events["push"]["branches"], ["main"])
        self.assertIn("workflow_dispatch", native_events)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'",
            native["jobs"]["deploy"]["if"],
        )

        legacy_deploy = all_workflows["gpoint-deploy.yml"]
        self.assertEqual(set(events(legacy_deploy)), {"workflow_dispatch"})

        main_push_workflows = set()
        prepare_workflow_run_listeners = set()
        for name, workflow in all_workflows.items():
            trigger = events(workflow)
            if "push" in trigger:
                branches = trigger["push"].get("branches", [])
                if not branches or "main" in branches:
                    main_push_workflows.add(name)
                    source = (WORKFLOWS / name).read_text()
                    self.assertNotIn("gpoint-deploy.yml", source)
                    self.assertNotIn("scripts/release-orchestrator.mjs", source)
            if "workflow_run" not in trigger:
                continue
            listened_workflows = trigger["workflow_run"].get("workflows", [])
            if "GPoint — Prepare" not in listened_workflows:
                continue
            prepare_workflow_run_listeners.add(name)
            source = (WORKFLOWS / name).read_text()
            self.assertNotIn("scripts/release-orchestrator.mjs", source)
            self.assertNotIn("gh workflow run gpoint-deploy", source)
            self.assertNotIn("actions/workflows/gpoint-deploy.yml/dispatches", source)
        self.assertEqual(prepare_workflow_run_listeners, {"gpoint-linear-reconcile.yml"})
        self.assertIn("gpoint-native-release.yml", main_push_workflows)

    def test_legacy_prepare_reconciliation_is_read_only(self):
        reconcile = load_workflow("gpoint-linear-reconcile.yml")
        workflow_run = events(reconcile)["workflow_run"]
        self.assertIn("GPoint — Prepare", workflow_run["workflows"])
        self.assertEqual(workflow_run["types"], ["completed"])
        job = reconcile["jobs"]["reconcile"]
        self.assertEqual(job["steps"][-1]["env"]["STATE_SYNC_ONLY"], "true")
        source = (WORKFLOWS / "gpoint-linear-reconcile.yml").read_text()
        self.assertNotIn("actions: write", source)
        self.assertNotIn("packages: write", source)
        self.assertNotIn("scripts/release-orchestrator.mjs", source)


if __name__ == "__main__":
    unittest.main()
