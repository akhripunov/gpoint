"""Isolated-restore readiness must wait for the final PostgreSQL server.

The disposable container runs the stock PostgreSQL entrypoint: initdb starts a
temporary server, then the real server is exec'd as PID 1. A single
``pg_isready`` can observe the temporary one, so readiness requires PID 1 to be
``postgres`` and a real ``SELECT 1`` to succeed.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-gpoint-scheduled-backup-restore.sh"


def function_source(name):
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name} is missing from {SCRIPT.name}"
    return match.group(0)


HARNESS = r"""
set -euo pipefail
switch_after="$1"
docker() {
  printf '%s | phase=%s\n' "$*" "$(cat "$COUNT" 2>/dev/null || echo 0)" >> "$LOG"
  if [[ "$1" == exec && "${4:-}" == "-ec" && "${5:-}" == "cat /proc/1/comm" ]]; then
    n=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 ))
    printf '%s' "$n" > "$COUNT"
    if (( n >= switch_after )); then
      printf 'postgres\n'
    else
      printf 'docker-entrypoint.sh\n'
    fi
    return 0
  fi
  if [[ "$1" == exec && "${3:-}" == "psql" ]]; then
    n=$(cat "$COUNT" 2>/dev/null || echo 0)
    if (( n >= switch_after )); then
      return 0
    fi
    printf 'psql: error: connection to server on socket failed: No such file or directory\n' >&2
    return 2
  fi
  return 0
}
<PLACEHOLDER>
"""


def run_readiness(tmp_path, switch_after, attempts):
    harness = tmp_path / "harness.sh"
    body = HARNESS.replace("<PLACEHOLDER>", function_source("await_restore_database"))
    body += "\nset +e\nawait_restore_database test-container " + str(attempts) + " 0\nrc=$?\nset -e\necho RC=$rc\n"
    harness.write_text(body, encoding="utf-8")
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(harness), str(switch_after)],
        capture_output=True, text=True, env={
            **__import__("os").environ, "LOG": str(log), "COUNT": str(tmp_path / "count"),
        })
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_readiness_ignores_the_temporary_initdb_server(tmp_path):
    # The temporary server answers the first probes; only probe 2 is the final one.
    result, log = run_readiness(tmp_path, switch_after=2, attempts=6)
    assert "RC=0" in result.stdout, result.stderr
    lines = log.splitlines()
    psql = [line for line in lines if " psql " in line]
    assert psql, "readiness never probed the database"
    assert "pg_isready" not in log, "readiness must not trust a bare pg_isready"
    # The temporary server is never queried: PID 1 must be postgres first, and
    # then a real SELECT 1 confirms the final server.
    assert all(line.endswith("phase=2") for line in psql), psql
    assert sum("cat /proc/1/comm" in line for line in lines) == 2


def test_readiness_fails_closed_when_the_main_server_never_starts(tmp_path):
    result, log = run_readiness(tmp_path, switch_after=99, attempts=3)
    assert "RC=1" in result.stdout, result.stderr
    # Bounded: exactly one PID 1 probe and one query attempt per iteration.
    assert sum("cat /proc/1/comm" in line for line in log.splitlines()) == 3
    # No query is attempted while PID 1 is still the entrypoint.
    assert not [line for line in log.splitlines() if " psql " in line]


def test_diagnostics_are_printed_before_cleanup_without_secrets(tmp_path):
    diagnostics = function_source("restore_diagnostics")
    harness = tmp_path / "diag.sh"
    harness.write_text(
        "set -euo pipefail\nrestore_container_id=abc123\n"
        + diagnostics
        + "\ndocker() {\n"
        + "  if [[ \"$1\" == inspect ]]; then echo 'running exit=2 oom=false'; return 0; fi\n"
        + "  if [[ \"$1\" == logs ]]; then echo 'postgres starting'; return 0; fi\n"
        + "  return 0\n}\n"
        + "restore_diagnostics\n",
        encoding="utf-8")
    result = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "GPOINT_RESTORE_DIAGNOSTIC_STATE=running exit=2 oom=false" in result.stderr
    assert "GPOINT_RESTORE_DIAGNOSTIC_LOGS_BEGIN" in result.stderr
    assert "postgres starting" in result.stderr
    assert "PASSWORD" not in result.stderr
    assert "GPOINT_RESTORE_DIAGNOSTIC_LOGS_END" in result.stderr
