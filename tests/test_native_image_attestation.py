import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_native_image_attestation.py'
SPEC = importlib.util.spec_from_file_location('verify_native_image_attestation', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class NativeImageAttestationTests(unittest.TestCase):
    def test_accepts_verified_remote_git_config_source_slsa_v1(self):
        sha = 'a' * 40
        provenance = {'SLSA': {'buildDefinition': {'externalParameters': {'configSource': {
            'uri': 'https://github.com/akhripunov/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }}}}}
        MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_accepts_verified_remote_git_config_source_slsa_v02(self):
        sha = 'b' * 40
        provenance = {'SLSA': {'invocation': {'configSource': {
            'uri': 'https://github.com/akhripunov/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }}}}
        MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_rejects_missing_or_mismatched_commit_digest(self):
        sha = 'c' * 40
        for digest in ({}, {'sha1': 'd' * 40}):
            provenance = {'SLSA': {'buildDefinition': {'externalParameters': {'configSource': {
                'uri': 'https://github.com/akhripunov/gpoint.git#refs/heads/main',
                'digest': digest,
            }}}}}
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_rejects_other_source_even_when_commit_digest_matches(self):
        sha = 'e' * 40
        provenance = {'SLSA': {'buildDefinition': {'externalParameters': {'configSource': {
            'uri': 'https://example.invalid/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }}}}}
        with self.assertRaises(ValueError):
            MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_rejects_empty_sbom(self):
        with self.assertRaises(ValueError):
            MODULE.verify_sbom({'SPDXID': 'SPDXRef-DOCUMENT', 'packages': []})

    def test_accepts_nonempty_sbom(self):
        MODULE.verify_sbom({'SPDXID': 'SPDXRef-DOCUMENT', 'packages': [{'name': 'gpoint'}]})


if __name__ == '__main__':
    unittest.main()
