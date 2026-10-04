from app.workflow import approval_binding, content_hash


def test_approval_changes_with_every_bound_value():
    row = dict(
        telegram_user_id=123,
        telegram_username="alice",
        seller_id="seller",
        telegram_account_id="account",
        message_reference="draft",
        content_hash=content_hash("hello"),
        account_user_id=555,
        session_secret_ref="vault",
        agata_character_id="character",
    )
    expected = approval_binding(row)
    assert len(expected) == 64
    for key in row:
        assert approval_binding({**row, key: str(row[key]) + "changed"}) != expected
