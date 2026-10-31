import importlib.util
import os
from pathlib import Path
import re

import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/kamal_cutover_credentials.py'


def module():
    spec = importlib.util.spec_from_file_location('credentials', PATH)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def setup(tmp_path):
    stage = tmp_path / ('a' * 40 + '-12345-1')
    stage.mkdir(mode=0o700)
    ssh = tmp_path / 'ssh'
    ssh.mkdir(mode=0o700)
    auth = ssh / 'authorized_keys'
    auth.write_bytes(b'# unrelated owner key\nssh-ed25519 AAAAexisting owner\n')
    auth.chmod(0o600)
    public = tmp_path / 'host.pub'
    public.write_text('ssh-ed25519 AAAAhost test-host\n')
    return stage, auth, public


def test_temporary_key_is_loopback_restricted_expiring_and_cleanup_preserves_other_keys(tmp_path):
    stage, auth, public = setup(tmp_path)
    original = auth.read_bytes()
    m = module()
    m.install(stage, auth, public, owner_uid=os.getuid())
    added = (stage / 'localhost-key.line').read_text()
    assert 'from="127.0.0.1,::1"' in added
    assert 'restrict,expiry-time="' in added
    expiry = re.search(r'expiry-time="([^"]+)"', added)
    assert expiry and re.fullmatch(r"[0-9]{14}", expiry.group(1))
    assert 'gpoint-cutover-' + stage.name in added
    assert auth.read_bytes() == original + added.encode()
    key = stage / 'home/.ssh/id_ed25519'
    assert key.stat().st_mode & 0o777 == 0o600
    assert 'StrictHostKeyChecking yes' in (key.parent / 'config').read_text()
    assert 'AAAAhost' in (key.parent / 'known_hosts').read_text()
    # A concurrent unrelated append must survive removal of our exact line.
    with auth.open('ab') as f:
        f.write(b'# another owner key\n')
    m.remove(stage, auth, owner_uid=os.getuid())
    assert auth.read_bytes() == original + b'# another owner key\n'
    assert not key.exists()
    m.remove(stage, auth, owner_uid=os.getuid())  # idempotent terminal cleanup


def test_replay_and_symlink_authorized_keys_are_rejected_before_write(tmp_path):
    stage, auth, public = setup(tmp_path)
    m = module()
    original = auth.read_bytes()
    real = auth.with_name('real')
    auth.rename(real)
    auth.symlink_to(real)
    with pytest.raises(m.CredentialError):
        m.install(stage, auth, public, owner_uid=os.getuid())
    assert real.read_bytes() == original
    auth.unlink()
    real.rename(auth)
    m.install(stage, auth, public, owner_uid=os.getuid())
    with pytest.raises(m.CredentialError):
        m.install(stage, auth, public, owner_uid=os.getuid())
    m.remove(stage, auth, owner_uid=os.getuid())


def test_cleanup_restores_original_bytes_when_authorized_keys_lacks_final_newline(tmp_path):
    stage, auth, public = setup(tmp_path)
    original = b'ssh-ed25519 AAAAoriginal-key'
    auth.write_bytes(original)
    auth.chmod(0o600)
    m = module()
    m.install(stage, auth, public, owner_uid=os.getuid())
    with auth.open('ab') as stream:
        stream.write(b'# appended during the attempt\n')
    m.remove(stage, auth, owner_uid=os.getuid())
    assert auth.read_bytes() == original + b'# appended during the attempt\n'


def test_terminal_cleanup_removes_only_attempt_secrets_and_credentials(tmp_path):
    stage, auth, public = setup(tmp_path)
    m = module()
    m.install(stage, auth, public, owner_uid=os.getuid())
    environment = stage / 'environment.json'
    environment.write_text('{"TOKEN":"secret"}')
    environment.chmod(0o600)
    docker = stage / 'home/.docker'
    docker.mkdir(mode=0o700)
    config = docker / 'config.json'
    config.write_text('{"auths":{"registry":"secret"}}')
    config.chmod(0o600)
    unrelated = docker / 'context-data'
    unrelated.write_text('preserve me')
    unrelated.chmod(0o600)
    status = stage / 'owner-status.json'
    status.write_text('{"state":"COMPLETE"}')
    status.chmod(0o600)

    m.terminal_cleanup(stage, auth, owner_uid=os.getuid())

    assert b'gpoint-cutover-' not in auth.read_bytes()
    assert not environment.exists()
    assert not config.exists()
    assert unrelated.read_text() == 'preserve me'
    assert status.exists()
    assert not (stage / 'home/.ssh/id_ed25519').exists()


def test_terminal_cleanup_fails_closed_on_docker_secret_symlink(tmp_path):
    stage, auth, public = setup(tmp_path)
    m = module()
    m.install(stage, auth, public, owner_uid=os.getuid())
    secret = tmp_path / 'outside-config'
    secret.write_text('preserve secret')
    secret.chmod(0o600)
    docker = stage / 'home/.docker'
    docker.mkdir(mode=0o700)
    (docker / 'config.json').symlink_to(secret)
    original_auth = auth.read_bytes()

    with pytest.raises(m.CredentialError):
        m.terminal_cleanup(stage, auth, owner_uid=os.getuid())

    assert auth.read_bytes() == original_auth
    assert secret.read_text() == 'preserve secret'
    assert (stage / 'home/.ssh/id_ed25519').exists()
