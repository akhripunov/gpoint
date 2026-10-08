"""Fail-closed Promptfoo runner for the AGATA Worker LAB provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import Mapping


_LAB = Path(__file__).resolve().parents[1]
_WORKER_DIR = _LAB.parents[0] / "worker"
_EVALUATORS_DIR = _LAB / "evaluators"
if str(_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKER_DIR))
if str(_EVALUATORS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALUATORS_DIR))
import worker_client  # noqa: E402
FORBIDDEN_ENV_KEYS = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
MINIMAL_ENV_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "CODEX_HOME", "PROMPTFOO_PYTHON", "PROMPTFOO_DISABLE_TELEMETRY", "PROMPTFOO_CONFIG_DIR", "GPOINT_AGATA_WORKER_SUPPORT", "AGATA_WORKER_ROOT", "AGATA_LAB_APPROVAL_FILE"})
EVAL_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_GRACE_SECONDS = 5


def _check_config_for_api_auth(value: object, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").casefold()
            if normalized in {"apikey", "api_key", "authorization", "access_token"} or normalized.endswith("_api_key"):
                raise PermissionError("API-key authentication is forbidden in Conversation Lab configuration")
            if normalized == "base_url":
                raise PermissionError("custom API endpoints are forbidden in Conversation Lab configuration")
            _check_config_for_api_auth(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_config_for_api_auth(child, f"{path}[{index}]")


def validate_live_environment(env: Mapping[str, str], provider_config: Mapping[str, object]) -> None:
    """Reject API-key auth and any provider route except the AGATA Worker LAB adapter."""
    present = {key for key in env if key in FORBIDDEN_ENV_KEYS or key.upper().endswith("_API_KEY")}
    if present:
        names = ", ".join(sorted(present))
        raise PermissionError(f"unset API-key environment variable(s) before Lab invocation: {names}")
    _check_config_for_api_auth(provider_config)
    providers = provider_config.get("providers")
    if providers is not None:
        if not isinstance(providers, list) or not providers:
            raise PermissionError("Conversation Lab requires the AGATA Worker LAB provider")
        expected_path = str(Path(__file__).with_name("promptfoo_worker_provider.py").resolve())
        for provider in providers:
            if not isinstance(provider, Mapping) or provider.get("id") != "file://" + expected_path:
                raise PermissionError("only the AGATA Worker LAB Python provider is permitted")


def build_cli_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Return only shell essentials needed by Promptfoo/Codex; never forward secrets."""
    allowed = set(MINIMAL_ENV_KEYS) | {"AGATA_LAB_CALL_LEDGER", "AGATA_LAB_QUEUE", "AGATA_LAB_PRIVATE_RECEIPTS"}
    return {key: env[key] for key in sorted(allowed) if key in env and env[key]}


def validate_promptfoo_python(executable: str, env: Mapping[str, str]) -> Path:
    """Promptfoo's Python provider must use the Worker-compatible Python 3.11+."""
    path = Path(executable).expanduser()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise PermissionError("Promptfoo Python provider runtime is missing or not executable")
    try:
        check = subprocess.run(
            [str(path), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=True, text=True,
            env={key: env[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if env.get(key)},
        )
        major, minor = (int(part) for part in check.stdout.strip().split(".", maxsplit=1))
        if (major, minor) < (3, 11):
            raise ValueError("unsupported Python version")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise PermissionError("Promptfoo requires a verified Python 3.11+ runtime for AGATA Worker imports") from exc
    return path


def build_provider_request(
    prompt: str, *, working_dir: str, model: str = "gpt-6-luna", reasoning: str = "medium",
) -> dict:
    """Return an offline Promptfoo provider preview; live runs require immutable IDs."""
    if model != "gpt-6-luna" or reasoning != "medium":
        raise PermissionError("Conversation Lab pins gpt-6-luna / medium")
    path = Path(working_dir)
    if not path.is_absolute() or not path.is_dir() or next(path.iterdir(), None) is not None:
        raise PermissionError("Promptfoo preview requires an existing empty working directory")
    provider = {
        "id": "file://" + str(Path(__file__).with_name("promptfoo_worker_provider.py").resolve()),
        "config": {
            "model": "gpt-6-luna",
            "reasoning_effort": "medium",
            "maxRetries": 0,
            "run_id": "offline-preview",
            "invocation_id": "offline-preview",
            "scenario_id": "promptfoo-compat-001",
        },
    }
    config = {
        "description": "Offline-only Promptfoo provider preview; not authorized for live execution",
        "prompts": [prompt],
        "providers": [provider],
        "tests": [{"vars": {}}],
        "evaluateOptions": {"maxConcurrency": 1, "cache": False},
    }
    validate_live_environment({}, config)
    return config


def _terminate_process_group(process) -> None:
    """Stop Promptfoo and any Codex CLI descendants without leaving an inference running."""
    if os.name != "posix":
        process.terminate()
        try:
            process.communicate(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit while a descendant remains in the new session and
    # closes its inherited pipes. Check the group itself before considering
    # cancellation complete; communicate() only reaps the direct child.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def _run_eval_bounded(args, *, env, timeout_seconds: int, on_started=lambda: None, on_ambiguous=lambda: None):
    """Run one eval in a new process group and always reap it after timeout/cancel."""
    if os.name != "posix":
        raise RuntimeError("bounded Codex process-tree cancellation requires POSIX")
    process = subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        on_started()
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            on_ambiguous()
        finally:
            _terminate_process_group(process)
        raise
    except KeyboardInterrupt:
        try:
            on_ambiguous()
        finally:
            _terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def run_promptfoo_eval(
    prompt: str,
    *,
    working_dir: str,
    promptfoo_executable: str,
    env: Mapping[str, str] | None = None,
    command_runner=subprocess.run,
    eval_runner=_run_eval_bounded,
    call_ledger,
    stage: str,
    invocation_id: str,
    invocation_metadata: Mapping[str, object],
):
    """The retired native Codex SDK path must never be invoked again."""
    raise RuntimeError("NATIVE_PROVIDER_UNAVAILABLE_CURRENT_ENV: use Promptfoo → AGATA Worker LAB")


def build_worker_provider_request(
    prompt: str, *, run_id: str, invocation_id: str, scenario_id: str,
) -> dict:
    """Build one Promptfoo eval config whose sole provider is the LAB adapter."""
    import sys
    worker_dir = _LAB.parents[0] / "worker"
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    import lab_queue

    fixture = lab_queue.read_promptfoo_compat_fixture()
    if (prompt != fixture["user_message"] or scenario_id != fixture["scenario_id"]
            or not run_id or not invocation_id):
        raise PermissionError("Promptfoo input is outside the frozen Worker compatibility fixture")
    provider_path = Path(__file__).with_name("promptfoo_worker_provider.py").resolve()
    provider = {
        "id": "file://" + str(provider_path),
        "config": {
            "model": "gpt-6-luna",
            "reasoning_effort": "medium",
            "pythonExecutable": sys.executable,
            "maxRetries": 0,
            "run_id": run_id,
            "invocation_id": invocation_id,
            "scenario_id": scenario_id,
        },
    }
    config = {
        "description": "AGATA Conversation Lab compatibility invocation via Worker LAB",
        "prompts": [prompt],
        "providers": [provider],
        "tests": [{"vars": {}}],
        "evaluateOptions": {"maxConcurrency": 1, "cache": False},
    }
    _check_config_for_api_auth(config)
    return config


def run_promptfoo_worker_eval(
    prompt: str, *, run_id: str, invocation_id: str, scenario_id: str,
    promptfoo_executable: str, worker_queue: Path, private_receipt_directory: Path,
    call_ledger, env: Mapping[str, str] | None = None, eval_runner=_run_eval_bounded,
):
    """Run Promptfoo once through the existing LAB queue; never starts Codex here."""
    approved_limits = {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}
    if (call_ledger is None or getattr(call_ledger, "durable", False) is not True
            or not callable(getattr(call_ledger, "verify_persisted", None))
            or not call_ledger.verify_persisted()
            or call_ledger.snapshot().get("limits") != approved_limits
            or getattr(call_ledger, "journal_path", None) is None):
        raise PermissionError("Promptfoo Worker LAB invocation requires a verified durable call ledger")
    source_env = os.environ if env is None else env
    config = build_worker_provider_request(
        prompt, run_id=run_id, invocation_id=invocation_id, scenario_id=scenario_id,
    )
    validate_live_environment(source_env, config)
    validate_promptfoo_python(config["providers"][0]["config"]["pythonExecutable"], source_env)
    canonical_queue = worker_client.canonical_lab_queue(source_env)
    requested_queue = Path(worker_queue).expanduser()
    if (requested_queue.absolute() != canonical_queue.absolute()
            or requested_queue.resolve(strict=False) != canonical_queue.resolve(strict=False)):
        raise PermissionError("Promptfoo may submit only to the canonical LAB queue")
    child_env = build_cli_environment(source_env)
    child_env["AGATA_LAB_CALL_LEDGER"] = str(call_ledger.journal_path)
    child_env["AGATA_LAB_QUEUE"] = str(canonical_queue)
    child_env["AGATA_LAB_PRIVATE_RECEIPTS"] = str(Path(private_receipt_directory))

    def current_snapshot():
        # Promptfoo's Python provider runs in a child process and appends to the
        # same durable journal. Reopen it before observing provider completion.
        refreshed = call_ledger.__class__(journal_path=Path(call_ledger.journal_path))
        return refreshed.snapshot()

    def mark_ambiguous_if_reserved():
        snapshot = current_snapshot().get("generator_invocations", {})
        entry = next((item for item in snapshot.get("invocations", [])
                      if item.get("invocation_id") == invocation_id), None)
        if entry is not None and entry.get("outcome") is None:
            try:
                call_ledger.complete("generator", invocation_id, "ambiguous")
            except ValueError:
                pass

    with tempfile.TemporaryDirectory(prefix="agata-promptfoo-worker-lab-") as temp_dir:
        config_path = Path(temp_dir) / "promptfooconfig.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        child_env["PROMPTFOO_CONFIG_DIR"] = temp_dir
        result = eval_runner(
            [promptfoo_executable, "eval", "--config", str(config_path), "--no-cache",
             "--max-concurrency", "1"],
            env=child_env,
            timeout_seconds=EVAL_TIMEOUT_SECONDS,
            on_started=lambda: None,
            on_ambiguous=mark_ambiguous_if_reserved,
        )
    snapshot = current_snapshot().get("generator_invocations", {})
    entry = next((item for item in snapshot.get("invocations", [])
                  if item.get("invocation_id") == invocation_id), None)
    if entry is None:
        if result.returncode != 0:
            raise RuntimeError("Promptfoo failed before Worker LAB reserved a generator attempt")
        raise RuntimeError("Promptfoo returned without invoking the AGATA Worker LAB provider")
    if result.returncode != 0 and entry.get("outcome") is None:
        mark_ambiguous_if_reserved()
    return result


def build_worker_judge_provider_request(prompt: str, *, run_id: str, batch_id: str,
                                        invocation_id: str, scenario_id: str,
                                        dataset_split: str, judge_slot: int,
                                        presented_labels: list[str],
                                        python_executable: str | None = None) -> dict:
    """Build exactly one Promptfoo eval target for one independent LAB judge."""
    worker_dir = _LAB.parents[0] / "worker"
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    import worker_client
    job = worker_client.build_judge_job(
        prompt, batch_id=batch_id, scenario_id=scenario_id, dataset_split=dataset_split,
        judge_slot=judge_slot, presented_labels=presented_labels,
    )
    if job["promptfoo_invocation_id"] != invocation_id or not isinstance(run_id, str) or not run_id:
        raise PermissionError("judge Promptfoo invocation identity does not match the immutable batch")
    provider_path = Path(__file__).with_name("promptfoo_judge_provider.py").resolve()
    python_path = Path(python_executable or sys.executable).expanduser()
    if not python_path.is_absolute() or not python_path.is_file() or not os.access(python_path, os.X_OK):
        raise PermissionError("Promptfoo judge requires an explicit executable Python runtime")
    provider = {"id": "file://" + str(provider_path), "config": {
        "model": "gpt-6-luna", "reasoning_effort": "medium", "maxRetries": 0,
        "fallbacks": [], "run_id": run_id, "batch_id": batch_id,
        "invocation_id": invocation_id, "scenario_id": scenario_id,
        "dataset_split": dataset_split, "judge_slot": judge_slot,
        "presented_labels": list(presented_labels), "pythonExecutable": str(python_path),
    }}
    return {
        "description": "AGATA Lab isolated judge via the canonical Worker LAB slot",
        "prompts": [prompt], "providers": [provider], "tests": [{"vars": {}}],
        "evaluateOptions": {"maxConcurrency": 1, "cache": False},
    }


def validate_judge_environment(env: Mapping[str, str], config: Mapping[str, object]) -> None:
    """Permit only the exact LAB judge Python provider; reject API-key routes."""
    present = {key for key in env if key in FORBIDDEN_ENV_KEYS or key.upper().endswith("_API_KEY")}
    if present:
        raise PermissionError("API-key authentication is forbidden for LAB judge invocations")
    _check_config_for_api_auth(config)
    providers = config.get("providers")
    expected = "file://" + str(Path(__file__).with_name("promptfoo_judge_provider.py").resolve())
    if (not isinstance(providers, list) or len(providers) != 1
            or not isinstance(providers[0], Mapping) or providers[0].get("id") != expected):
        raise PermissionError("only the AGATA LAB judge provider is permitted")
    provider_config = providers[0].get("config")
    if (not isinstance(provider_config, Mapping) or provider_config.get("model") != "gpt-6-luna"
            or provider_config.get("reasoning_effort") != "medium"
            or provider_config.get("maxRetries") != 0 or provider_config.get("fallbacks") != []
            or config.get("evaluateOptions") != {"maxConcurrency": 1, "cache": False}
            or "prompts" not in config or "tests" not in config or "defaultTest" in config
            or "grader" in config or "assertions" in config):
        raise PermissionError("LAB judge Promptfoo config violates the fixed execution contract")
    validate_promptfoo_python(str(provider_config.get("pythonExecutable", "")), env)


def run_promptfoo_judge_eval(prompt: str, *, run_id: str, batch_id: str,
                             invocation_id: str, scenario_id: str, dataset_split: str,
                             judge_slot: int, presented_labels: list[str],
                             promptfoo_executable: str, worker_queue: Path,
                             private_receipt_directory: Path, call_ledger,
                             env: Mapping[str, str] | None = None, eval_runner=_run_eval_bounded):
    """Run one pinned real Promptfoo CLI evaluation with its sole target in Worker LAB."""
    worker_dir = _LAB.parents[0] / "worker"
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    import worker_client
    approved_limits = {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}
    if (call_ledger is None or getattr(call_ledger, "durable", False) is not True
            or not callable(getattr(call_ledger, "verify_persisted", None))
            or not call_ledger.verify_persisted()
            or call_ledger.snapshot().get("limits") != approved_limits
            or getattr(call_ledger, "journal_path", None) is None):
        raise PermissionError("Promptfoo judge execution requires the verified durable call ledger")
    source_env = os.environ if env is None else env
    config = build_worker_judge_provider_request(
        prompt, run_id=run_id, batch_id=batch_id, invocation_id=invocation_id,
        scenario_id=scenario_id, dataset_split=dataset_split, judge_slot=judge_slot,
        presented_labels=presented_labels,
        python_executable=(source_env.get("PROMPTFOO_PYTHON") or sys.executable),
    )
    validate_judge_environment(source_env, config)
    canonical_queue = worker_client.canonical_lab_queue(source_env)
    requested_queue = Path(worker_queue).expanduser()
    if (requested_queue.absolute() != canonical_queue.absolute()
            or requested_queue.resolve(strict=False) != canonical_queue.resolve(strict=False)):
        raise PermissionError("Promptfoo judge may submit only to the canonical LAB queue")
    child_env = build_cli_environment(source_env)
    child_env["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    child_env["AGATA_LAB_CALL_LEDGER"] = str(call_ledger.journal_path)
    child_env["AGATA_LAB_QUEUE"] = str(canonical_queue)
    child_env["AGATA_LAB_PRIVATE_RECEIPTS"] = str(Path(private_receipt_directory))

    def current_ledger():
        return call_ledger.__class__(journal_path=Path(call_ledger.journal_path))

    def mark_ambiguous_if_reserved():
        snapshot = current_ledger().snapshot().get("judge_invocations", {})
        entry = next((row for row in snapshot.get("invocations", [])
                      if row.get("invocation_id") == invocation_id), None)
        if entry is not None and entry.get("outcome") is None:
            try:
                call_ledger.complete("judge", invocation_id, "ambiguous")
            except ValueError:
                pass

    with tempfile.TemporaryDirectory(prefix="agata-promptfoo-judge-") as temp_dir:
        config_path = Path(temp_dir) / "promptfooconfig.json"
        output_path = Path(temp_dir) / "promptfoo-result.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        config_path.chmod(0o600)
        child_env["PROMPTFOO_CONFIG_DIR"] = temp_dir
        command = [promptfoo_executable, "eval", "--config", str(config_path),
                   "--no-cache", "--no-write", "--no-share", "--no-progress-bar",
                   "--max-concurrency", "1", "--output", str(output_path)]
        result = eval_runner(command, env=child_env, timeout_seconds=EVAL_TIMEOUT_SECONDS,
                             on_started=lambda: None, on_ambiguous=mark_ambiguous_if_reserved)
        if result.returncode == 0:
            try:
                output = json.loads(output_path.read_text(encoding="utf-8"))
                results = output.get("results") if isinstance(output, dict) else None
                rows = results.get("results") if isinstance(results, dict) else results
                if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("error"):
                    return subprocess.CompletedProcess(command, 1, result.stdout, "Promptfoo result was not one successful judge evaluation")
                worker_receipt = json.loads((Path(private_receipt_directory) /
                    (invocation_id + ".judge-receipt.json")).read_text(encoding="utf-8"))
                if (not isinstance(worker_receipt, dict) or worker_receipt.get("outcome") != "success"
                        or worker_receipt.get("worker_terminal_status") != "completed"):
                    return subprocess.CompletedProcess(command, 1, result.stdout, "Promptfoo completed without a successful Worker judge receipt")
                cli_receipt = {
                    "schema_version": 1, "run_id": run_id, "batch_id": batch_id,
                    "promptfoo_invocation_id": invocation_id,
                    "worker_job_id": worker_receipt.get("worker_job_id"),
                    "worker_receipt_sha256": worker_receipt.get("worker_receipt_sha256"),
                    "result_count": 1, "promptfoo_success": True,
                    "promptfoo_result_sha256": __import__("hashlib").sha256(
                        output_path.read_bytes()).hexdigest(),
                }
                receipt_path = Path(private_receipt_directory) / (invocation_id + ".promptfoo-receipt.json")
                fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                        json.dump(cli_receipt, stream, sort_keys=True, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(fd)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return subprocess.CompletedProcess(command, 1, result.stdout, "Promptfoo result artifact was missing or invalid")
    return result
