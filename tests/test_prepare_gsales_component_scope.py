import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PREPARE = load_script("prepare_release_scope", ROOT / "scripts" / "prepare-release.py")
GSALES = load_script("gsales_postgres_scope", ROOT / "services" / "gsales" / "tests" / "run_postgres.py")


class GsalesComponentScopeTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.baseline = "b" * 40
        self.v3 = {
            "candidateSha": self.sha,
            "productionBaselineSha": self.baseline,
            "checks": {
                "agata_postgres_rehearsal": True,
                "gsales_postgres_regression": True,
            },
        }
        self.v4 = {
            "schema": 1,
            "candidateSha": self.sha,
            "productionBaselineSha": self.baseline,
            "baselineProvenance": "immutable-COMPLETE",
            "classification": "COMPONENT_CANDIDATE",
            "component": "gsales",
            "changedPaths": ["services/gsales/app/api.py"],
            "unsafePaths": [],
            "maySkipV3Gates": False,
            "mayDeploySelectively": False,
            "skippedHeavyWork": [],
        }

    def test_only_exact_verified_gsales_component_requests_narrow_suite(self):
        command = PREPARE.gsales_regression_command(self.sha, self.v3, self.v4)
        self.assertEqual(command[-1], "--gsales-component-only")

    def test_unknown_or_shared_scope_keeps_full_regression(self):
        for component in ("agata", None):
            with self.subTest(component=component):
                v4 = {**self.v4, "component": component,
                      "classification": "FULL" if component is None else "COMPONENT_CANDIDATE"}
                command = PREPARE.gsales_regression_command(self.sha, self.v3, v4)
                self.assertNotIn("--gsales-component-only", command)

    def test_mismatched_candidate_baseline_or_missing_primary_agata_gate_keeps_full(self):
        cases = [
            (self.sha, {**self.v4, "candidateSha": "c" * 40}, self.v3),
            (self.sha, {**self.v4, "productionBaselineSha": "c" * 40}, self.v3),
            (self.sha, self.v4, {**self.v3, "productionBaselineSha": "invalid"}),
            (self.sha, self.v4, {**self.v3, "productionBaselineSha": self.sha}),
            (self.sha, self.v4, {**self.v3, "checks": {**self.v3["checks"], "agata_postgres_rehearsal": False}}),
        ]
        for sha, v4, v3 in cases:
            with self.subTest(sha=sha, v4=v4, v3=v3):
                self.assertNotIn("--gsales-component-only", PREPARE.gsales_regression_command(sha, v3, v4))

    def test_narrow_gsales_suite_retains_gsales_and_shared_agata_sales_tests(self):
        plan = GSALES.postgres_suite_plan(gsales_component_only=True)
        self.assertTrue(plan["gsales_tests"])
        self.assertTrue(plan["agata_sales_tenant_postgres"])
        self.assertFalse(plan["agata_core_postgres_regression"])

    def test_full_or_unknown_scope_retains_every_postgres_suite(self):
        for component_only in (False, None):
            with self.subTest(component_only=component_only):
                plan = GSALES.postgres_suite_plan(gsales_component_only=component_only)
                self.assertTrue(all(plan.values()))


if __name__ == "__main__":
    unittest.main()
