import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location('package', ROOT / 'scripts/package-kamal-cutover.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def environment(m):
    candidate = subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD^{commit}'], text=True
    ).strip()
    values = {key: 'proof-value' for key in m.REQUIRED_ENV}
    values.update(CANDIDATE_SHA=candidate, APP_SOURCE_SHA='b'*40,
                  GITHUB_RUN_ID='12345', GITHUB_RUN_ATTEMPT='1', GITHUB_ACTOR='owner',
                  IMAGE_VERSION=candidate+'-12345-1', KAMAL_REGISTRY_PASSWORD='private-test-token')
    return values


def test_bundle_binds_phases_config_and_private_env_to_exact_attempt(tmp_path):
    m = module()
    archive = tmp_path / 'bundle.tar'
    env = environment(m)
    digest = m.package(ROOT, archive, env)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert all(not name.startswith('/') and '..' not in Path(name).parts for name in names)
        raw = tar.extractfile('manifest.json').read()
        assert hashlib.sha256(raw).hexdigest() == digest
        manifest = json.loads(raw)
        assert manifest['candidateSha'] == env['CANDIDATE_SHA']
        for path, sha in manifest['files'].items():
            assert hashlib.sha256(tar.extractfile(path).read()).hexdigest() == sha
        packaged_env = json.load(tar.extractfile('environment.json'))
        assert packaged_env['GPOINT_PRODUCTION_HOST'] == '127.0.0.1'
        assert packaged_env['KAMAL_REGISTRY_PASSWORD'] == 'private-test-token'
        assert 'GH_TOKEN' not in packaged_env
        assert tar.getmember('environment.json').mode == 0o600
        assert 'private-test-token' not in raw.decode()
        for phase in ('requests', 'consumers'):
            text = tar.extractfile('phases/'+phase+'.sh').read().decode()
            assert '--network host' in text
            assert 'kamal deploy --skip-push' in text
            assert "rev-parse --verify 'HEAD^{commit}'" in text
            assert 'Kamal Git workspace HEAD does not match candidate' in text
        assert 'repo/.kamal/hooks/pre-app-boot' in names
        git_config = tar.extractfile('repo/.git/config').read().decode()
        assert '[core]' in git_config
        assert 'remote' not in git_config.lower() and 'credential' not in git_config.lower()
        git_objects = [name for name in names if name.startswith('repo/.git/objects/')]
        assert git_objects and all('/pack/pack-' in name for name in git_objects)
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


def test_candidate_sha_cannot_be_relabelled_away_from_checkout_head(tmp_path):
    m = module()
    env = environment(m)
    wrong_sha = subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD^'], text=True
    ).strip()
    env.update(CANDIDATE_SHA=wrong_sha, IMAGE_VERSION=f'{wrong_sha}-12345-1')
    with pytest.raises(ValueError, match='HEAD does not match'):
        m.package(ROOT, tmp_path/'wrong-head.tar', env)
    assert not (tmp_path/'wrong-head.tar').exists()


def test_unpacked_release_bundle_has_real_candidate_git_head_for_kamal(tmp_path):
    m = module()
    candidate = subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD^{commit}'], text=True
    ).strip()
    env = environment(m)
    env.update(CANDIDATE_SHA=candidate, IMAGE_VERSION=f'{candidate}-12345-1')
    archive = tmp_path / 'bundle.tar'
    m.package(ROOT, archive, env)

    unpacked = tmp_path / 'unpacked'
    unpacked.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(unpacked)

    workspace = unpacked / 'repo'
    actual_head = subprocess.check_output(
        ['git', '-C', str(workspace), 'rev-parse', '--verify', 'HEAD^{commit}'], text=True
    ).strip()
    assert actual_head == candidate
    assert (workspace / '.git' / 'HEAD').read_text().strip() == candidate
    git_config = (workspace / '.git' / 'config').read_text()
    assert 'credential' not in git_config.lower() and 'extraheader' not in git_config.lower()
    pack_index = next((workspace / '.git' / 'objects' / 'pack').glob('*.idx'))
    verify_pack = subprocess.check_output(
        ['git', '--git-dir', str(workspace / '.git'), 'verify-pack', '-v', str(pack_index)], text=True
    )
    object_types = [line.split()[1] for line in verify_pack.splitlines()
                    if len(line.split()) > 1 and line.split()[1] in {'commit', 'tree', 'blob', 'tag'}]
    assert 'commit' in object_types and set(object_types) <= {'commit', 'tree'}
