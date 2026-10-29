import importlib.util
import json
import tempfile
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

    def test_accepts_git_plus_https_source_uri(self):
        sha = 'f' * 40
        provenance = {'SLSA': {'buildDefinition': {'externalParameters': {'configSource': {
            'uri': 'git+https://github.com/akhripunov/gpoint.git#refs/heads/main',
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

    def test_accepts_workflow_config_source_and_separately_pinned_git_material(self):
        workflow_sha = 'a' * 40
        app_sha = 'b' * 40
        provenance = {'SLSA': {'predicate': {'buildDefinition': {
            'externalParameters': {'configSource': {
                'uri': f'https://github.com/akhripunov/gpoint.git#{workflow_sha}',
                'digest': {'sha1': workflow_sha},
            }},
            'resolvedDependencies': [
                {'uri': f'https://github.com/akhripunov/gpoint.git#{app_sha}',
                 'digest': {'sha1': app_sha}},
            ],
        }}}}
        MODULE.verify_provenance_identity(
            provenance, workflow_sha, 'https://github.com/akhripunov/gpoint.git', app_sha
        )

    def test_requires_exact_app_source_git_material_for_dual_context_build(self):
        workflow_sha = 'c' * 40
        app_sha = 'd' * 40
        wrong_sha = 'e' * 40
        provenance = {'SLSA': {'predicate': {'buildDefinition': {
            'externalParameters': {'configSource': {
                'uri': f'https://github.com/akhripunov/gpoint.git#{workflow_sha}',
                'digest': {'sha1': workflow_sha},
            }},
            'resolvedDependencies': [
                {'uri': f'https://github.com/akhripunov/gpoint.git#{wrong_sha}',
                 'digest': {'sha1': wrong_sha}},
            ],
        }}}}
        with self.assertRaisesRegex(ValueError, 'required remote Git material'):
            MODULE.verify_provenance_identity(
                provenance, workflow_sha, 'https://github.com/akhripunov/gpoint.git', app_sha
            )

    def test_accepts_exact_source_material_in_slsa_v02_materials(self):
        workflow_sha = 'f' * 40
        app_sha = '1' * 40
        provenance = {'SLSA': {
            'invocation': {'configSource': {
                'uri': f'https://github.com/akhripunov/gpoint.git#{workflow_sha}',
                'digest': {'sha1': workflow_sha},
            }},
            'materials': [{
                'uri': f'git+https://github.com/akhripunov/gpoint.git#{app_sha}',
                'digest': {'sha1': app_sha},
            }],
        }}
        MODULE.verify_provenance_identity(
            provenance, workflow_sha, 'https://github.com/akhripunov/gpoint.git', app_sha
        )

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

    def test_rejects_nonstandard_port(self):
        sha = 'a' * 40
        provenance = {'SLSA': {'buildDefinition': {'externalParameters': {'configSource': {
            'uri': 'https://github.com:8443/akhripunov/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }}}}}
        with self.assertRaises(ValueError):
            MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_rejects_ambiguous_source_configurations(self):
        sha = 'b' * 40
        expected = {
            'uri': 'https://github.com/akhripunov/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }
        alternate = {
            'uri': 'https://github.com/attacker/gpoint.git#refs/heads/main',
            'digest': {'sha1': sha},
        }
        provenance = {'SLSA': {
            'buildDefinition': {'externalParameters': {'configSource': expected}},
            'invocation': {'configSource': alternate},
        }}
        with self.assertRaises(ValueError):
            MODULE.verify_provenance_identity(provenance, sha, 'https://github.com/akhripunov/gpoint.git')

    def test_rejects_empty_sbom(self):
        with self.assertRaises(ValueError):
            MODULE.verify_sbom({'SPDXID': 'SPDXRef-DOCUMENT', 'packages': []})

    def test_rejects_malformed_sbom_package(self):
        with self.assertRaises(ValueError):
            MODULE.verify_sbom({'SPDXID': 'SPDXRef-DOCUMENT', 'packages': [{}]})

    def test_accepts_nonempty_sbom(self):
        MODULE.verify_sbom({'SPDXID': 'SPDXRef-DOCUMENT', 'packages': [
            {'name': 'gpoint', 'SPDXID': 'SPDXRef-Package-gpoint'}
        ]})

    def test_requires_expected_build_arg_in_slsa_v1_provenance(self):
        digest = 'sha256:' + 'a' * 64
        provenance = {'SLSA': {'predicate': {'buildDefinition': {
            'externalParameters': {'request': {'args': {
                'build-arg:NGINX_BASE_DIGEST': digest,
            }}}
        }}}}
        MODULE.verify_build_args(provenance, {'NGINX_BASE_DIGEST': digest})
        with self.assertRaisesRegex(ValueError, 'NGINX_BASE_DIGEST'):
            MODULE.verify_build_args(provenance, {'NGINX_BASE_DIGEST': 'sha256:' + 'b' * 64})
        with self.assertRaisesRegex(ValueError, 'missing SLSA v1 build arguments'):
            MODULE.verify_build_args({'SLSA': {}}, {'NGINX_BASE_DIGEST': digest})

    def test_cli_checks_both_required_source_material_and_build_arg(self):
        workflow_sha = 'a' * 40
        app_sha = 'b' * 40
        digest = 'sha256:' + 'c' * 64
        sbom = {'SPDXID': 'SPDXRef-DOCUMENT', 'packages': [
            {'name': 'miniapp', 'SPDXID': 'SPDXRef-Package-miniapp'}
        ]}
        provenance = {'SLSA': {'predicate': {'buildDefinition': {
            'externalParameters': {
                'configSource': {
                    'uri': f'https://github.com/akhripunov/gpoint.git#{workflow_sha}',
                    'digest': {'sha1': workflow_sha},
                },
                'request': {'args': {'build-arg:NGINX_BASE_DIGEST': digest}},
            },
            'resolvedDependencies': [{
                'uri': f'https://github.com/akhripunov/gpoint.git#{app_sha}',
                'digest': {'sha1': app_sha},
            }],
        }}}}
        with tempfile.TemporaryDirectory() as temp:
            sbom_path = Path(temp) / 'sbom.json'
            provenance_path = Path(temp) / 'provenance.json'
            sbom_path.write_text(json.dumps(sbom), encoding='utf-8')
            provenance_path.write_text(json.dumps(provenance), encoding='utf-8')
            argv = [
                'verify_native_image_attestation.py', str(sbom_path), str(provenance_path),
                workflow_sha, 'https://github.com/akhripunov/gpoint.git',
                '--required-material-sha', app_sha,
                '--required-build-arg', f'NGINX_BASE_DIGEST={digest}',
            ]
            self.assertEqual(MODULE.main(argv), 0)
            provenance['SLSA']['predicate']['buildDefinition']['resolvedDependencies'][0]['digest']['sha1'] = 'd' * 40
            provenance_path.write_text(json.dumps(provenance), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'required remote Git material'):
                MODULE.main(argv)


if __name__ == '__main__':
    unittest.main()
