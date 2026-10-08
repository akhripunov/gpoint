"""Tests for the no-inference Codex Local app-server preflight."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).parents[1] / "evaluators" / "codex_app_server_preflight.py"
SPEC = importlib.util.spec_from_file_location("codex_app_server_preflight", MODULE_PATH)
preflight = None
if SPEC is not None and SPEC.loader is not None and MODULE_PATH.is_file():
    preflight = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(preflight)


class CodexAppServerPreflightTests(unittest.TestCase):
    def _require_preflight(self):
        self.assertIsNotNone(preflight, "Codex app-server preflight implementation is missing")

    def _fake_cli(self, root: Path, *, auth_line="Logged in using ChatGPT", init_response=True):
        calls = root / "calls.jsonl"
        cli = root / "codex-fake"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            f"calls = {str(calls)!r}\n"
            "args = sys.argv[1:]\n"
            "with open(calls, 'a', encoding='utf-8') as f: f.write(json.dumps({'args':args, 'api_key_env_present': any(k.endswith('_API_KEY') and os.environ.get(k) for k in os.environ)})+'\\n')\n"
            "if args == ['login', 'status']:\n"
            f"    print({auth_line!r}, file=sys.stderr); sys.exit(0)\n"
            "if args == ['app-server', '--listen', 'stdio://']:\n"
            "    request = json.loads(sys.stdin.readline())\n"
            + ("    print(json.dumps({'id':request['id'], 'result':{'userAgent':'codex'}}), flush=True)\n" if init_response else "    time.sleep(5)\n")
            + "    while True: time.sleep(0.02)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        cli.chmod(0o700)
        return cli, calls

    def test_initializes_codex_app_server_without_model_or_api_key_forwarding(self):
        self._require_preflight()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli, calls = self._fake_cli(root)
            result = preflight.run_preflight(
                codex_path=cli,
                working_dir=root,
                parent_env={"HOME": str(root), "PATH": "/usr/bin:/bin", "LANG": "C"},
                timeout_seconds=2,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["auth"], "chatgpt_subscription")
            self.assertEqual(result["model_calls"], 0)
            rows = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual([row["args"] for row in rows], [
                ["login", "status"], ["app-server", "--listen", "stdio://"]
            ])
            self.assertTrue(all(not row["api_key_env_present"] for row in rows))

    def test_fails_closed_when_api_key_environment_is_present(self):
        self._require_preflight()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli, calls = self._fake_cli(root)
            with self.assertRaisesRegex(preflight.PreflightError, "API-key environment path"):
                preflight.run_preflight(
                    codex_path=cli,
                    working_dir=root,
                    parent_env={"HOME": str(root), "PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "never-log"},
                    timeout_seconds=2,
                )
            self.assertFalse(calls.exists())

    def test_fails_closed_when_login_is_not_chatgpt_subscription(self):
        self._require_preflight()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli, calls = self._fake_cli(root, auth_line="Logged in using API key")
            with self.assertRaisesRegex(preflight.PreflightError, "ChatGPT subscription login"):
                preflight.run_preflight(
                    codex_path=cli,
                    working_dir=root,
                    parent_env={"HOME": str(root), "PATH": "/usr/bin:/bin"},
                    timeout_seconds=2,
                )
            rows = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["args"], ["login", "status"])

    def test_fails_closed_when_app_server_does_not_initialize(self):
        self._require_preflight()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli, _ = self._fake_cli(root, init_response=False)
            with self.assertRaisesRegex(preflight.PreflightError, "initialization timed out"):
                preflight.run_preflight(
                    codex_path=cli,
                    working_dir=root,
                    parent_env={"HOME": str(root), "PATH": "/usr/bin:/bin"},
                    timeout_seconds=1.0,
                )


if __name__ == "__main__":
    unittest.main()
