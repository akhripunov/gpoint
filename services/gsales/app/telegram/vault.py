"""Encrypted session files; login codes and 2FA passwords are never persisted."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.db.session import GateError


class SessionVault:
    def __init__(self, settings: Settings):
        if not settings.secret_dir or not settings.secret_master_key:
            raise GateError("session_vault_not_configured")
        self.root = Path(settings.secret_dir).expanduser().resolve()
        repo = Path(__file__).resolve().parents[4]
        if self.root == repo or repo in self.root.parents:
            raise GateError("session_vault_must_be_outside_repository")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise GateError("session_directory_permissions_unsafe")
        try:
            self.crypto = Fernet(settings.secret_master_key.get_secret_value().encode())
        except ValueError:
            raise GateError("session_key_invalid") from None

    def store(self, credentials):
        if (
            set(credentials) != {"session", "api_id", "api_hash"}
            or not credentials["session"]
            or not credentials["api_hash"]
            or not isinstance(credentials["api_id"], int)
        ):
            raise GateError("session_payload_invalid")
        reference = uuid4()
        data = self.crypto.encrypt(json.dumps(credentials, separators=(",", ":")).encode())
        fd = os.open(
            self.root / (str(reference) + ".session.enc"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return reference

    def load(self, reference):
        path = self.root / (str(UUID(str(reference))) + ".session.enc")
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise GateError("session_file_permissions_unsafe")
        try:
            return json.loads(self.crypto.decrypt(path.read_bytes()))
        except (InvalidToken, ValueError):
            raise GateError("session_decryption_failed") from None
