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
MINIMAL_ENV_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "CODEX_HOME", "PROMPTFOO_DISABLE_TELEMETRY", "PROMPTFOO_CONFIG_DIR", "GPOINT_AGATA_WORKER_SUPPORT", "AGATA_WORKER_ROOT"})
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
        "tests": [{}],
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
        "tests": [{}],
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
