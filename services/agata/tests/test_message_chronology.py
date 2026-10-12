from datetime import UTC, datetime, timedelta

from app.responses.pipeline import assistant_message_timestamp


def test_assistant_timestamp_is_after_user_when_request_timestamp_is_shared():
    as_of = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)

    assert assistant_message_timestamp(as_of, now=as_of) == as_of + timedelta(microseconds=1)


def test_assistant_timestamp_uses_generation_time_when_it_is_later():
    user_timestamp = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)
    generated_at = user_timestamp + timedelta(seconds=3)

    assert assistant_message_timestamp(user_timestamp, now=generated_at) == generated_at


def test_assistant_timestamp_stays_after_a_future_dated_user_message():
    future_user_timestamp = datetime(2026, 10, 12, 10, 30, tzinfo=UTC) + timedelta(
        days=1
    )
    generated_at = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)

    assert assistant_message_timestamp(future_user_timestamp, now=generated_at) == (
        future_user_timestamp + timedelta(microseconds=1)
    )
