import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location('package', ROOT / 'scripts/package-kamal-cutover.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def environment(m):
    values = {key: 'proof-value' for key in m.REQUIRED_ENV}
    values.update(CANDIDATE_SHA='a'*40, APP_SOURCE_SHA='b'*40,
                  GITHUB_RUN_ID='12345', GITHUB_RUN_ATTEMPT='1', GITHUB_ACTOR='owner',
                  IMAGE_VERSION='a'*40+'-12345-1', KAMAL_REGISTRY_PASSWORD='private-test-token')
    return values


def test_bundle_binds_phases_config_and_private_env_to_exact_attempt(tmp_path):
    m = module()
    archive = tmp_path / 'bundle.tar'
    digest = m.package(ROOT, archive, environment(m))
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert all(not name.startswith('/') and '..' not in Path(name).parts for name in names)
        raw = tar.extractfile('manifest.json').read()
        assert hashlib.sha256(raw).hexdigest() == digest
        manifest = json.loads(raw)
        assert manifest['candidateSha'] == 'a'*40
        for path, sha in manifest['files'].items():
            assert hashlib.sha256(tar.extractfile(path).read()).hexdigest() == sha
        env = json.load(tar.extractfile('environment.json'))
        assert env['GPOINT_PRODUCTION_HOST'] == '127.0.0.1'
        assert env['KAMAL_REGISTRY_PASSWORD'] == 'private-test-token'
        assert 'GH_TOKEN' not in env
        assert tar.getmember('environment.json').mode == 0o600
        assert 'private-test-token' not in raw.decode()
        for phase in ('requests', 'consumers'):
            text = tar.extractfile('phases/'+phase+'.sh').read().decode()
            assert '--network host' in text
            assert 'kamal deploy --skip-push' in text
        assert 'repo/.kamal/hooks/pre-app-boot' in names
    assert archive.stat().st_mode & 0o777 == 0o600


def test_missing_secret_or_attempt_drift_refuses_packaging(tmp_path):
    m = module()
    env = environment(m)
    env.pop('KAMAL_REGISTRY_PASSWORD')
    with pytest.raises(ValueError):
        m.package(ROOT, tmp_path/'missing.tar', env)
    env = environment(m)
    env['IMAGE_VERSION'] = 'different-attempt'
    with pytest.raises(ValueError):
        m.package(ROOT, tmp_path/'wrong.tar', env)
