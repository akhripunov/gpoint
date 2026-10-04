import base64

import pytest

from app.config import Settings
from app.db.session import GateError
from app.telegram.vault import SessionVault


def test_vault_encrypts_and_rejects_wrong_key(tmp_path):
    vault = SessionVault(
        Settings(
            secret_dir=str(tmp_path), secret_master_key=base64.urlsafe_b64encode(b"a" * 32).decode()
        )
    )
    ref = vault.store(
        {"session": "test-private-session", "api_hash": "private-hash", "api_id": 123}
    )
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert b"test-private-session" not in files[0].read_bytes()
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert vault.load(ref)["session"] == "test-private-session"
    wrong = SessionVault(
        Settings(
            secret_dir=str(tmp_path), secret_master_key=base64.urlsafe_b64encode(b"b" * 32).decode()
        )
    )
    with pytest.raises(GateError):
        wrong.load(ref)


def test_vault_rejects_login_codes(tmp_path):
    vault = SessionVault(
        Settings(
            secret_dir=str(tmp_path), secret_master_key=base64.urlsafe_b64encode(b"a" * 32).decode()
        )
    )
    with pytest.raises(GateError):
        vault.store({"session": "test", "api_hash": "test", "api_id": 1, "login_code": "123"})
