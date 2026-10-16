import unittest
from copy import deepcopy

from scripts.publish_ghcr_images import parse_push_digest, validate_image_metadata

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
CONFIG = 'sha256:' + 'c' * 64


class GhcrImagePublishTests(unittest.TestCase):
    def test_accepts_exact_linux_amd64_image_and_registry_digest(self):
        record = validate_image_metadata('app', 'ghcr.io/akhripunov/gpoint-runtime', DIGEST,
            {'Id': CONFIG, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {
                'org.opencontainers.image.revision': SHA,
                'org.opencontainers.image.source': 'https://github.com/akhripunov/gpoint'}}}, SHA)
        self.assertEqual(record, {'repository': 'ghcr.io/akhripunov/gpoint-runtime',
            'digest': DIGEST, 'configDigest': CONFIG, 'sourceSha': SHA, 'platform': 'linux/amd64'})

    def test_parses_exact_manifest_digest_without_accepting_other_output(self):
        self.assertEqual(parse_push_digest(f'latest: digest: {DIGEST} size: 123'), DIGEST)
        with self.assertRaisesRegex(ValueError, 'manifest digest'):
            parse_push_digest('pushed successfully')
        with self.assertRaisesRegex(ValueError, 'manifest digest'):
            parse_push_digest(f'digest: sha256:{"g" * 64}')

    def test_rejects_source_architecture_and_config_identity_mismatch(self):
        valid = {'Id': CONFIG, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {
            'org.opencontainers.image.revision': SHA,
            'org.opencontainers.image.source': 'https://github.com/akhripunov/gpoint'}}}
        wrong_arch = deepcopy(valid)
        wrong_arch['Architecture'] = 'arm64'
        wrong_source = deepcopy(valid)
        wrong_source['Config']['Labels']['org.opencontainers.image.revision'] = 'd' * 40
        wrong_config = deepcopy(valid)
        wrong_config['Id'] = 'sha256:not-a-config-digest'
        for metadata in (wrong_arch, wrong_source, wrong_config):
            with self.assertRaises(ValueError):
                validate_image_metadata('app', 'ghcr.io/akhripunov/gpoint-runtime', DIGEST,
                    metadata, SHA)

    def test_rejects_wrong_registry_or_role(self):
        with self.assertRaisesRegex(ValueError, 'repository'):
            validate_image_metadata('app', 'ghcr.io/akhripunov/other', DIGEST,
                {'Id': CONFIG, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {
                    'org.opencontainers.image.revision': SHA,
                    'org.opencontainers.image.source': 'https://github.com/akhripunov/gpoint'}}}, SHA)
        with self.assertRaisesRegex(ValueError, 'role'):
            validate_image_metadata('unknown', 'ghcr.io/akhripunov/gpoint-runtime', DIGEST,
                {}, SHA)


if __name__ == '__main__':
    unittest.main()
