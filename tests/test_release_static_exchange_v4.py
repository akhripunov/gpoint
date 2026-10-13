import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts/release_static_exchange_v4.py"
spec = importlib.util.spec_from_file_location("exchange", MODULE)
exchange = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exchange)
A = "a"*40
B = "b"*40


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        app = self.root / "app"
        app.mkdir()
        self.live = app / "miniapp"
        self.staged = app / ("static-v4-" + B)
        self.live.mkdir()
        self.staged.mkdir()
        (self.live / "index.html").write_text('<meta name="gpoint-build-sha" content="' + A + '">')
        (self.staged / "index.html").write_text('<meta name="gpoint-build-sha" content="' + B + '">')

    def test_atomic_directory_exchange_and_restore(self):
        outcome = exchange.exchange_site(self.root, A, B)
        self.assertEqual(outcome["mode"], "RENAME_EXCHANGE")
        self.assertEqual(exchange._entry_sha(self.live), B)
        self.assertEqual(exchange._entry_sha(self.staged), A)
        exchange.restore_site(self.root, B, A)
        self.assertEqual(exchange._entry_sha(self.live), A)

    def test_failed_precondition_leaves_original_intact(self):
        with self.assertRaises(ValueError):
            exchange.exchange_site(self.root, B, A)
        self.assertEqual(exchange._entry_sha(self.live), A)

    def test_symlink_rejected_without_mutation(self):
        (self.staged / "index.html").unlink()
        (self.staged / "index.html").symlink_to(self.live / "index.html")
        with self.assertRaises(ValueError):
            exchange.exchange_site(self.root, A, B)
        self.assertEqual(exchange._entry_sha(self.live), A)

    def test_rollback_must_match_current_candidate(self):
        with self.assertRaises(ValueError):
            exchange.restore_site(self.root, B, A)
        self.assertEqual(exchange._entry_sha(self.live), A)

if __name__ == "__main__":
    unittest.main()
