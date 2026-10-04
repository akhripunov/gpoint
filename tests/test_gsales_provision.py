import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('provision', Path(__file__).resolve().parents[1] / 'scripts/gsales-production-provision.py')


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.m)

    def test_rejects_unknown_existing_tenants_before_mutation(self):
        gpoint = '81a4ba28-6de5-4b03-8332-0661ba4d46f4'
        with self.assertRaisesRegex(RuntimeError, 'unmapped'):
            self.m.role_bindings([gpoint, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'], gpoint, None)

    def test_reuses_only_real_sales_identity(self):
        gp = '81a4ba28-6de5-4b03-8332-0661ba4d46f4'
        gs = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.assertEqual(self.m.role_bindings([gp, gs], gp, gs), {'gpoint': gp, 'gsales_runtime': gs, 'gsales_agata_runtime': gs})
        with self.assertRaisesRegex(RuntimeError, 'test|distinct'):
            self.m.role_bindings([gp], gp, 'c4d67889-877e-4d1b-a8d8-597c479d638f')

    def test_exact_authorization_and_baseline(self):
        self.m.validate_authorization('a' * 40, 'a' * 40, 'b' * 40, 'b' * 40)
        for candidate, approved, expected, live in [('main', 'main', 'b'*40, 'b'*40), ('a'*40, 'b'*40, 'c'*40, 'c'*40), ('a'*40, 'a'*40, 'b'*40, 'c'*40)]:
            with self.assertRaises(RuntimeError):
                self.m.validate_authorization(candidate, approved, expected, live)


if __name__ == '__main__':
    unittest.main()
