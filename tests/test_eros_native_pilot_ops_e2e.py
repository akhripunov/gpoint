import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "eros_native_pilot_ops_e2e", ROOT / "services/eros-engine/run-native-pilot-ops-e2e.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_failure_summary_reports_only_safe_stage_and_exception_type():
    line = MODULE.failure_summary("stock SQLx migration", RuntimeError("postgres password must stay hidden"))
    assert line == "EROS_NATIVE_POSTGRES_E2E=FAIL:stage=stock SQLx migration:type=RuntimeError"
    assert "postgres password" not in line


def test_failure_summary_includes_only_allowlisted_diagnostic_code():
    error = RuntimeError("postgres password must stay hidden")
    error.diagnostic_code = "postgres_schema_permission"
    line = MODULE.failure_summary("stock SQLx migration", error)
    assert line == (
        "EROS_NATIVE_POSTGRES_E2E=FAIL:stage=stock SQLx migration:"
        "type=RuntimeError:diagnostic=postgres_schema_permission"
    )
    assert "postgres password" not in line
    error.diagnostic_code = "postgresql://secret"
    assert "diagnostic=" not in MODULE.failure_summary("stock SQLx migration", error)
    error.diagnostic_code = "postgres_sqlstate"
    assert "diagnostic=postgres_sqlstate" in MODULE.failure_summary("stock SQLx migration", error)
