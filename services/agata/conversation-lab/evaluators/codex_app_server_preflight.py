"""Fail-closed, no-inference preflight for the local Codex app-server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from typing import Mapping


_API_KEY_NAME = re.compile(r"(?:^|_)API_KEY$", re.IGNORECASE)
_API_KEY_CONFIG_KEYS = {"api_key", "apikey", "api_key_env", "env_key"}


class PreflightError(RuntimeError):
    """Sanitized fail-closed preflight error; never includes credential values."""


def _configured_api_key_path(config: Path) -> bool:
    if not config.is_file():
        return False
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError("Codex config cannot be safely checked") from exc

    def contains_key_path(node):
        if isinstance(node, Mapping):
            for key, child in node.items():
                if str(key).casefold() in _API_KEY_CONFIG_KEYS and child not in (None, "", False):
                    return True
                if contains_key_path(child):
                    return True
        elif isinstance(node, list):
            return any(contains_key_path(child) for child in node)
        return False

    return contains_key_path(value)


def _effective_environment(parent_env: Mapping[str, str]) -> dict[str, str]:
    key_names = sorted(
        name for name, value in parent_env.items()
        if value and (_API_KEY_NAME.search(name) or name in {"OPENAI_BASE_URL", "OPENAI_API_BASE"})
    )
    if key_names:
        raise PreflightError("API-key environment path is configured")

    home = parent_env.get("HOME")
    if not isinstance(home, str) or not home:
        raise PreflightError("HOME is unavailable for Codex subscription authentication")
    code_home = parent_env.get("CODEX_HOME")
    codex_dir = Path(code_home) if code_home else Path(home) / ".codex"
    config_path = codex_dir / "config.toml"
    if _configured_api_key_path(config_path):
        raise PreflightError("API-key Codex provider configuration is present")

    env = {
        "HOME": home,
        "PATH": parent_env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": parent_env.get("LANG", "C.UTF-8"),
    }
    for name in ("CODEX_HOME", "TMPDIR"):
        value = parent_env.get(name)
        if value:
            env[name] = value
    return env


def _check_subscription_login(codex_path: Path, *, cwd: Path, env: Mapping[str, str], timeout: float) -> None:
    try:
        result = subprocess.run(
            [str(codex_path), "login", "status"], cwd=cwd, env=dict(env),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("Codex subscription login status timed out") from exc
    except OSError as exc:
        raise PreflightError(f"Codex login status could not start ({exc.__class__.__name__})") from exc
    login_status = (result.stdout + "\n" + result.stderr).casefold()
    if result.returncode != 0 or "logged in using chatgpt" not in login_status:
        raise PreflightError("ChatGPT subscription login is not confirmed")


def _initialize_app_server(codex_path: Path, *, cwd: Path, env: Mapping[str, str], timeout: float) -> None:
    command = [str(codex_path), "app-server", "--listen", "stdio://"]
    process = None
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
        )
    except OSError as exc:
        raise PreflightError(f"Codex app-server could not start ({exc.__class__.__name__})") from exc

    try:
        assert process.stdin is not None and process.stdout is not None
        request = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "agata-conversation-lab-preflight", "version": "1"},
                "capabilities": {"experimentalApi": False},
            },
        }
        process.stdin.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        buffer = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise PreflightError("Codex app-server exited before initialization")
                events = selector.select(max(0.0, deadline - time.monotonic()))
                if not events:
                    raise PreflightError("Codex app-server initialization timed out")
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    raise PreflightError("Codex app-server closed before initialization")
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise PreflightError("Codex app-server emitted an invalid protocol frame") from exc
                    if isinstance(message, dict) and message.get("id") == 1:
                        if "error" in message:
                            raise PreflightError("Codex app-server rejected initialization")
                        if not isinstance(message.get("result"), dict):
                            raise PreflightError("Codex app-server returned an invalid initialization result")
                        process.stdin.write(b'{"method":"initialized","params":{}}\n')
                        process.stdin.flush()
                        return
            raise PreflightError("Codex app-server initialization timed out")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


def run_preflight(*, codex_path: str | Path, working_dir: str | Path,
                  parent_env: Mapping[str, str] | None = None,
                  timeout_seconds: float = 10.0) -> dict[str, object]:
    """Check subscription status and complete app-server initialize; never starts a model turn."""
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    executable = Path(codex_path).expanduser()
    cwd = Path(working_dir).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PreflightError("Codex CLI executable is unavailable")
    if not cwd.is_dir():
        raise PreflightError("isolated working directory is unavailable")
    parent = dict(os.environ if parent_env is None else parent_env)
    env = _effective_environment(parent)
    started = time.monotonic()
    _check_subscription_login(executable, cwd=cwd, env=env, timeout=min(timeout_seconds, 10.0))
    _initialize_app_server(executable, cwd=cwd, env=env, timeout=timeout_seconds)
    return {
        "status": "PASS",
        "auth": "chatgpt_subscription",
        "app_server_initialized": True,
        "model_calls": 0,
        "retries": 0,
        "fallback": 0,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, help="absolute path to the installed Codex CLI")
    parser.add_argument("--working-dir", required=True, help="isolated existing working directory")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--repeat", type=int, default=1, help="sequential no-inference preflights, 1-3")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 3:
        parser.error("--repeat must be between 1 and 3")
    results = []
    try:
        for index in range(args.repeat):
            try:
                result = run_preflight(codex_path=args.codex, working_dir=args.working_dir,
                                       timeout_seconds=args.timeout_seconds)
            except PreflightError as exc:
                results.append({"attempt": index + 1, "status": "FAIL", "reason": str(exc), "model_calls": 0})
                print(json.dumps({"status": "FAIL", "preflights": results}, sort_keys=True))
                return 1
            results.append({"attempt": index + 1, **result})
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "PASS", "preflights": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
