import base64
from uuid import uuid4

import pytest

from app.messages.crypto import AeadCrypto, CryptoError, UnknownKeyVersionError
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    decrypt_text_from_storage,
    encrypt_json_for_storage,
    encrypt_text_for_storage,
    opaque_search_text,
)

KEY = base64.b64encode(b"a" * 32).decode("ascii")


def _aad(field: str = "value_text") -> bytes:
    return content_aad(
        entity="memories",
        tenant_id=uuid4(),
        user_id=uuid4(),
        character_id=uuid4(),
        scope="real_world",
        scenario_id=None,
        record_id=uuid4(),
        field=field,
    )


def test_text_and_json_are_encrypted_authenticated_and_idempotent() -> None:
    crypto = AeadCrypto(encoded_key=KEY, key_version="v1")
    aad = _aad()
    protected = encrypt_text_for_storage("private memory", aad, crypto=crypto)
    assert "private memory" not in protected
    assert decrypt_text_from_storage(protected, aad, crypto=crypto) == "private memory"
    assert encrypt_text_for_storage(protected, aad, crypto=crypto) == protected

    json_aad = _aad("value_json")
    payload = {"note": "private narrative", "score": 3}
    encrypted = encrypt_json_for_storage(payload, json_aad, crypto=crypto)
    assert "private narrative" not in str(encrypted)
    assert decrypt_json_from_storage(encrypted, json_aad, crypto=crypto) == payload
    assert encrypt_json_for_storage(encrypted, json_aad, crypto=crypto) == encrypted


def test_wrong_key_aad_unknown_version_and_tampering_fail_closed() -> None:
    aad = _aad()
    ciphertext = encrypt_text_for_storage("private memory", aad, crypto=AeadCrypto(encoded_key=KEY, key_version="v1"))
    wrong_key = AeadCrypto(encoded_key=base64.b64encode(b"b" * 32).decode(), key_version="v1")
    with pytest.raises(CryptoError):
        decrypt_text_from_storage(ciphertext, aad, crypto=wrong_key)
    with pytest.raises(CryptoError):
        decrypt_text_from_storage(ciphertext, _aad(), crypto=AeadCrypto(encoded_key=KEY, key_version="v1"))
    with pytest.raises(UnknownKeyVersionError, match="unknown_key_version"):
        decrypt_text_from_storage(ciphertext, aad, crypto=AeadCrypto(encoded_key=KEY, key_version="v2"))

    version, nonce, encrypted = ciphertext.split("$", 3)[1:]
    changed = encrypted[:-2] + ("AA" if encrypted[-2:] != "AA" else "BB")
    tampered = f"agata${version}${nonce}${changed}"
    with pytest.raises(CryptoError):
        decrypt_text_from_storage(tampered, aad, crypto=AeadCrypto(encoded_key=KEY, key_version="v1"))


def test_plaintext_is_never_accepted_as_an_encrypted_payload() -> None:
    with pytest.raises(CryptoError, match="plaintext_not_permitted"):
        decrypt_text_from_storage("legacy plaintext", _aad(), crypto=AeadCrypto(encoded_key=KEY, key_version="v1"))
    with pytest.raises(CryptoError, match="plaintext_not_permitted"):
        decrypt_json_from_storage({"memory": "legacy plaintext"}, _aad(), crypto=AeadCrypto(encoded_key=KEY, key_version="v1"))


def test_fts_representation_is_opaque_and_deterministic_without_persisting_words() -> None:
    key = base64.b64encode(b"search-key-material-32-bytes-long"[:32]).decode("ascii")
    indexed = opaque_search_text("Alina любит красные розы", key)
    assert "Alina" not in indexed and "розы" not in indexed
    assert indexed == opaque_search_text("ALINA любит красные розы", key)
    assert all(token.startswith("w") and len(token) == 65 for token in indexed.split())
