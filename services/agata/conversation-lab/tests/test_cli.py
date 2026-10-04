import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI=Path(__file__).resolve().parents[1]/"evaluators/cli.py"
class CLITests(unittest.TestCase):
    def test_cli_rejects_invalid_benchmark_with_nonzero_exit(self):
        self.assertTrue(CLI.exists(),"offline CLI missing")
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"empty.jsonl";p.write_text("")
            r=subprocess.run([sys.executable,str(CLI),"benchmark","--scenarios",str(p)],capture_output=True,text=True)
            self.assertEqual(r.returncode,2);self.assertEqual(json.loads(r.stdout)["status"],"FAIL")
    def test_help_has_no_network_runner(self):
        self.assertTrue(CLI.exists(),"offline CLI missing")
        r=subprocess.run([sys.executable,str(CLI),"--help"],capture_output=True,text=True)
        self.assertEqual(r.returncode,0);self.assertIn("leakage",r.stdout);self.assertNotIn("deploy",r.stdout)
