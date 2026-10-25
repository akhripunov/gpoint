import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_native_image_readback.py'
SPEC = importlib.util.spec_from_file_location('verify_native_image_readback', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class NativeImageReadbackTests(unittest.TestCase):
    def test_normalizes_docker_image_inspect_object(self):
        item = {'Os': 'linux', 'Architecture': 'amd64'}
        self.assertEqual(MODULE.normalize_image_inspect(item), item)

    def test_normalizes_singleton_array(self):
        item = {'Os': 'linux', 'Architecture': 'amd64'}
        self.assertEqual(MODULE.normalize_image_inspect([item]), item)

    def test_rejects_ambiguous_or_invalid_inspect_output(self):
        for value in ([], [{}, {}], None, 'not-json-object'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.normalize_image_inspect(value)

    def test_verifies_repo_digest_platform_and_exact_source_identity(self):
        image = 'ghcr.io/akhripunov/gpoint-runtime'
        digest = 'sha256:' + 'a' * 64
        sha = 'b' * 40
        item = {
            'RepoDigests': [image + '@' + digest],
            'Os': 'linux',
            'Architecture': 'amd64',
            'Config': {'Labels': {
                'org.opencontainers.image.revision': sha,
                'org.opencontainers.image.source': 'https://github.com/akhripunov/gpoint',
            }},
        }
        MODULE.verify_image_identity(item, image, digest, sha)

    def test_rejects_repo_digest_or_source_mismatch(self):
        image = 'ghcr.io/akhripunov/gpoint-runtime'
        digest = 'sha256:' + 'a' * 64
        sha = 'b' * 40
        item = {
            'RepoDigests': [image + '@' + 'sha256:' + 'c' * 64],
            'Os': 'linux',
            'Architecture': 'amd64',
            'Config': {'Labels': {
                'org.opencontainers.image.revision': sha,
                'org.opencontainers.image.source': 'https://example.invalid/repo',
            }},
        }
        with self.assertRaises(ValueError):
            MODULE.verify_image_identity(item, image, digest, sha)


if __name__ == '__main__':
    unittest.main()
