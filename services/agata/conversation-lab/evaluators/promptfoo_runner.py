"""Fail-closed Promptfoo runner for the AGATA Worker LAB provider."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
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
from preinference_diagnostics import write_preinference_failure  # noqa: E402
FORBIDDEN_ENV_KEYS = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
MINIMAL_ENV_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "CODEX_HOME", "PROMPTFOO_PYTHON", "PROMPTFOO_DISABLE_TELEMETRY", "PROMPTFOO_CONFIG_DIR", "GPOINT_AGATA_WORKER_SUPPORT", "AGATA_WORKER_ROOT", "AGATA_LAB_APPROVAL_FILE"})
EVAL_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_GRACE_SECONDS = 5
MIN_PROMPTFOO_NODE_VERSION = (22, 22, 0)


def resolve_promptfoo_node(env: Mapping[str, str], *, node_executable: str | None = None) -> Path:
    """Resolve and verify the Node runtime needed by the pinned Promptfoo CLI."""
    candidate = node_executable or shutil.which("node", path=env.get("PATH", ""))
    if candidate is None:
        # The Lab caller may intentionally pass a minimal environment. Resolve
        # the already-installed runtime from this process without forwarding its
        # environment to Promptfoo.
        candidate = shutil.which("node", path=os.environ.get("PATH", ""))
    if candidate is None:
        raise PermissionError("Promptfoo requires an installed Node.js runtime")
    path = Path(candidate).expanduser().resolve()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise PermissionError("Promptfoo Node.js runtime is unavailable")
    check_env = {"PATH": str(path.parent)}
    for key in ("HOME", "TMPDIR", "LANG", "LC_ALL"):
        if env.get(key):
            check_env[key] = env[key]
    try:
        result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=True, text=True, env=check_env)
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
        if match is None or tuple(map(int, match.groups())) < MIN_PROMPTFOO_NODE_VERSION:
            raise ValueError("unsupported Node.js version")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PermissionError("Promptfoo requires verified Node.js 22.22.0 or newer") from exc
    return path


def ensure_promptfoo_node_path(env: Mapping[str, str], *, node_executable: str | None = None) -> str:
    """Add only the verified Node directory to the child-only Promptfoo PATH."""
    node = resolve_promptfoo_node(env, node_executable=node_executable)
    existing = env.get("PATH", "")
    entries = [item for item in existing.split(os.pathsep) if item]
    node_dir = str(node.parent)
    return os.pathsep.join([node_dir, *[entry for entry in entries if entry != node_dir]])


def _preflight_call(function, *, receipts: Path, invocation_id: str,
                    stage: str, reason_code: str, source_location: str):
    try:
        return function()
    except BaseException as exc:
        try:
            write_preinference_failure(
                receipts, invocation_id=invocation_id, stage=stage,
                reason_code=reason_code, exception=exc, source_location=source_location,
            )
        except (OSError, PermissionError, ValueError, FileExistsError):
            # Keep the original validation failure visible; receipt-write
            # failures must never replace its exception or trigger a retry.
            pass
        raise


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
    result = {key: env[key] for key in sorted(allowed) if key in env and env[key]}
    result["PATH"] = ensure_promptfoo_node_path(result)
    return result


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


def build_worker_v2_provider_request(
    request: Mapping[str, object], *, run_id: str, invocation_id: str, arm_id: str,
    configuration_id: str, scenario_id: str, turn_index: int,
    history_receipt_ids: list[str], python_executable: str,
) -> dict:
    """Build one Promptfoo eval bound to a single immutable V2 DEV turn."""
    import json
    import lab_queue
    manifest = lab_queue.read_v2_dev_manifest()
    if (scenario_id not in manifest["scenarios"] or configuration_id not in {"baseline", "naturalness-v1"}
            or type(turn_index) is not int or turn_index not in range(4)
            or len(history_receipt_ids) != turn_index):
        raise PermissionError("Promptfoo may execute only a committed V2 DEV turn")
    worker_client.build_v2_dev_job(
        request=dict(request), run_id=run_id, arm_id=arm_id, configuration_id=configuration_id,
        scenario_id=scenario_id, turn_index=turn_index,
        history_receipt_ids=history_receipt_ids, invocation_id=invocation_id,
    )
    provider_path = Path(__file__).with_name("promptfoo_worker_provider.py").resolve()
    provider = {"id": "file://" + str(provider_path), "config": {
        "job_type": "v2-dev-generator", "model": "gpt-6-luna", "reasoning_effort": "medium",
        "maxRetries": 0, "fallbacks": [], "run_id": run_id, "invocation_id": invocation_id,
        "arm_id": arm_id, "configuration_id": configuration_id, "scenario_id": scenario_id,
        "turn_index": turn_index, "history_receipt_ids": list(history_receipt_ids),
        "dataset_split": "DEV", "pythonExecutable": python_executable,
    }}
    config = {
        "description": "AGATA V2 DEV generator through the canonical Worker LAB lane",
        "prompts": ["AGATA_V2_WORKER_REQUEST\n" + json.dumps(
            dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))],
        "providers": [provider], "tests": [{"vars": {}}],
        "evaluateOptions": {"maxConcurrency": 1, "cache": False},
    }
    _check_config_for_api_auth(config)
    return config


def validate_promptfoo_cli(executable: str, *, command_runner=subprocess.run) -> str:
    """Verify the local, Lab-pinned Promptfoo CLI without invoking any provider."""
    path = Path(executable).expanduser().resolve()
    tooling = (_LAB / "tooling" / "promptfoo").resolve()
    try:
        path.relative_to(tooling)
    except ValueError as exc:
        raise PermissionError("Promptfoo must be the Lab-local pinned CLI") from exc
    package = json.loads((tooling / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((tooling / "package-lock.json").read_text(encoding="utf-8"))
    if (package.get("devDependencies", {}).get("promptfoo") != "0.123.1"
            or lock.get("packages", {}).get("node_modules/promptfoo", {}).get("version") != "0.123.1"
            or not path.is_file() or not os.access(path, os.X_OK)):
        raise PermissionError("Promptfoo package, lockfile, or local executable is not exact 0.123.1")
    version_env = build_cli_environment(os.environ)
    version_env["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    version_env["PROMPTFOO_DISABLE_UPDATE_CHECK"] = "1"
    result = command_runner([str(path), "--version"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=10, env=version_env)
    if result.returncode != 0 or result.stdout.strip() != "0.123.1":
        raise PermissionError("Lab-local Promptfoo CLI did not report pinned version 0.123.1")
    return result.stdout.strip()


def _promptfoo_result_output(path: Path) -> str:
    import json
    value = json.loads(path.read_text(encoding="utf-8"))
    result_root = value.get("results") if isinstance(value, dict) else None
    rows = result_root.get("results") if isinstance(result_root, dict) else result_root
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping) or rows[0].get("error"):
        raise RuntimeError("Promptfoo did not return exactly one successful V2 generator result")
    response = rows[0].get("response")
    output = response.get("output") if isinstance(response, Mapping) else None
    if isinstance(output, Mapping):
        output = output.get("output")
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError("Promptfoo result has no model output")
    return output


def run_promptfoo_v2_dev_eval(
    request: Mapping[str, object], *, run_id: str, invocation_id: str, arm_id: str,
    configuration_id: str, scenario_id: str, turn_index: int,
    history_receipt_ids: list[str], promptfoo_executable: str, worker_queue: Path,
    private_receipt_directory: Path, call_ledger, env: Mapping[str, str] | None = None,
    eval_runner=_run_eval_bounded,
) -> dict:
    """Run one actual Promptfoo CLI eval; provider can only submit one LAB job."""
    import hashlib
    import json
    from datetime import datetime, timezone
    from approval_phases import PhaseApprovalState
    import lab_queue
    import worker_client

    source_env = os.environ if env is None else env
    diagnostic_root = Path(private_receipt_directory)
    def check(function, stage, reason_code, location):
        return _preflight_call(function, receipts=diagnostic_root,
                              invocation_id=invocation_id, stage=stage,
                              reason_code=reason_code, source_location=location)

    limits = {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}
    def validate_ledger():
        if (getattr(call_ledger, "durable", False) is not True or not call_ledger.verify_persisted()
                or call_ledger.snapshot().get("limits") != limits
                or getattr(call_ledger, "journal_path", None) is None):
            raise PermissionError("V2 Promptfoo eval requires a verified durable call ledger")
    check(validate_ledger, "GENERATOR_RESERVATION", "DURABLE_LEDGER_INVALID",
          "promptfoo_runner.run_promptfoo_v2_dev_eval")
    config = check(lambda: build_worker_v2_provider_request(
        request, run_id=run_id, invocation_id=invocation_id, arm_id=arm_id,
        configuration_id=configuration_id, scenario_id=scenario_id,
        turn_index=turn_index, history_receipt_ids=history_receipt_ids,
        python_executable=source_env.get("PROMPTFOO_PYTHON") or sys.executable,
    ), "IMMUTABLE_V2_JOB_CONTRACT", "V2_JOB_CONTRACT_INVALID",
       "promptfoo_runner.run_promptfoo_v2_dev_eval")
    forbidden_keys = {key for key in source_env
                      if key in FORBIDDEN_ENV_KEYS or key.upper().endswith("_API_KEY")}
    if forbidden_keys:
        check(lambda: (_ for _ in ()).throw(PermissionError("API-key environment is forbidden")),
              "ENV_VALIDATION", "API_KEY_ENV_FORBIDDEN", "promptfoo_runner.validate_live_environment")
    check(lambda: _check_config_for_api_auth(config), "PROVIDER_CONFIG",
          "PROVIDER_AUTH_CONFIG_FORBIDDEN", "promptfoo_runner._check_config_for_api_auth")
    check(lambda: validate_live_environment(source_env, config), "ENV_VALIDATION",
          "WORKER_PROVIDER_ROUTE_INVALID", "promptfoo_runner.validate_live_environment")
    check(lambda: validate_promptfoo_python(
        config["providers"][0]["config"]["pythonExecutable"], source_env),
        "PYTHON_VALIDATION", "PYTHON_RUNTIME_INVALID", "promptfoo_runner.validate_promptfoo_python")
    check(lambda: resolve_promptfoo_node(build_cli_environment(source_env)),
          "PROMPTFOO_CLI_VALIDATION", "NODE_RUNTIME_INVALID",
          "promptfoo_runner.resolve_promptfoo_node")
    check(lambda: validate_promptfoo_cli(promptfoo_executable), "PROMPTFOO_CLI_VALIDATION",
          "PROMPTFOO_VERSION_OR_EXECUTABLE_INVALID", "promptfoo_runner.validate_promptfoo_cli")
    queue = check(lambda: worker_client.canonical_lab_queue(source_env), "LAB_QUEUE_VALIDATION",
                  "CANONICAL_LAB_QUEUE_INVALID", "promptfoo_runner.run_promptfoo_v2_dev_eval")
    requested_queue = Path(worker_queue).expanduser()
    if requested_queue.absolute() != queue.absolute() or requested_queue.resolve(strict=False) != queue.resolve(strict=False):
        check(lambda: (_ for _ in ()).throw(PermissionError("noncanonical queue")),
              "LAB_QUEUE_VALIDATION", "CANONICAL_LAB_QUEUE_MISMATCH",
              "promptfoo_runner.run_promptfoo_v2_dev_eval")
    if turn_index == 0:
        state_path = queue / "state" / "v2-approval-phase.json"
        if not state_path.is_file():
            check(lambda: (_ for _ in ()).throw(PermissionError("V2 phase state unavailable")),
                  "PHASE_STATE_VALIDATION", "PHASE_STATE_INACTIVE",
                  "promptfoo_runner.run_promptfoo_v2_dev_eval")
    child_env = check(lambda: build_cli_environment(source_env), "PROMPTFOO_CLI_VALIDATION",
                      "PROMPTFOO_CHILD_ENV_INVALID", "promptfoo_runner.build_cli_environment")
    child_env.update({
        "PROMPTFOO_DISABLE_TELEMETRY": "1",
        "AGATA_LAB_CALL_LEDGER": str(call_ledger.journal_path),
        "AGATA_LAB_QUEUE": str(queue),
        "AGATA_LAB_PRIVATE_RECEIPTS": str(Path(private_receipt_directory)),
    })
    with tempfile.TemporaryDirectory(prefix="agata-promptfoo-v2-generator-") as temp_dir:
        temp = Path(temp_dir)
        config_path, result_path = temp / "promptfooconfig.json", temp / "promptfoo-result.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        config_path.chmod(0o600)
        child_env["PROMPTFOO_CONFIG_DIR"] = temp_dir
        command = [promptfoo_executable, "eval", "--config", str(config_path), "--no-cache",
                   "--no-write", "--no-share", "--no-progress-bar", "--max-concurrency", "1",
                   "--output", str(result_path)]
        try:
            result = eval_runner(command, env=child_env, timeout_seconds=EVAL_TIMEOUT_SECONDS,
                                 on_started=lambda: None,
                                 on_ambiguous=lambda: _mark_v2_ambiguous(call_ledger, invocation_id))
        except BaseException:
            raise
        call_ledger.refresh()
        rows = call_ledger.snapshot()["generator_invocations"]["invocations"]
        ledger_row = next((row for row in rows if row.get("invocation_id") == invocation_id), None)
        if ledger_row is None:
            exc = RuntimeError("Promptfoo exited before durable generator reservation")
            try:
                write_preinference_failure(
                    diagnostic_root, invocation_id=invocation_id,
                    stage="PROMPTFOO_CLI_VALIDATION", reason_code="CLI_EXITED_BEFORE_RESERVATION",
                    exception=exc, source_location="promptfoo_runner.run_promptfoo_v2_dev_eval",
                )
            except (OSError, PermissionError, ValueError, FileExistsError):
                pass
            raise exc
        if result.returncode != 0:
            if ledger_row.get("outcome") is None:
                call_ledger.complete("generator", invocation_id, "ambiguous")
            raise RuntimeError("Promptfoo V2 eval failed; the reserved attempt cannot be retried")
        output = _promptfoo_result_output(result_path)
        receipt_path = Path(private_receipt_directory) / (invocation_id + ".worker-receipt.json")
        worker_evidence = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (worker_evidence.get("promptfoo_outcome") != "success"
                or worker_evidence.get("output_sha256") != hashlib.sha256(output.encode()).hexdigest()
                or ledger_row.get("outcome") != "success"):
            raise RuntimeError("Promptfoo result is not bound to one successful Worker terminal receipt")
        cli_path = Path(private_receipt_directory) / (invocation_id + ".promptfoo-receipt.json")
        cli_receipt = {
            "schema_version": 1, "run_id": run_id, "promptfoo_invocation_id": invocation_id,
            "worker_job_id": worker_evidence["worker_job_id"],
            "worker_receipt_sha256": worker_evidence["worker_receipt_sha256"],
            "promptfoo_success": True, "result_count": 1,
            "promptfoo_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        fd = os.open(cli_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                json.dump(cli_receipt, stream, sort_keys=True, separators=(",", ":"))
                stream.flush(); os.fsync(stream.fileno())
        finally:
            os.close(fd)
        return {"output": output, "worker_receipt": worker_evidence, "promptfoo_receipt": cli_receipt}


def _mark_v2_ambiguous(ledger, invocation_id: str) -> None:
    ledger.refresh()
    row = next((item for item in ledger.snapshot()["generator_invocations"]["invocations"]
                if item.get("invocation_id") == invocation_id), None)
    if row is not None and row.get("outcome") is None:
        ledger.complete("generator", invocation_id, "ambiguous")


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
