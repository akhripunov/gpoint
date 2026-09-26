from datetime import UTC, datetime, timedelta

import pytest

from app.memory.segments import segment_hierarchy, segment_interval


def test_segment_interval_orders_ascending_messages_and_preserves_boundaries() -> None:
    first = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    last = datetime(2026, 9, 26, 9, 10, tzinfo=UTC)

    assert segment_interval((first, last)) == (first, last)
    assert segment_interval((last, first)) == (first, last)
    assert segment_interval((first, first + timedelta(seconds=2), last)) == (first, last)
    hierarchy = segment_hierarchy("Boundary summary", first, last)
    assert hierarchy["segment"]["started_at"] == first.isoformat()
    assert hierarchy["segment"]["ended_at"] == last.isoformat()


def test_single_message_segment_is_a_valid_one_microsecond_interval() -> None:
    instant = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)

    assert segment_interval((instant,)) == (instant, instant + timedelta(microseconds=1))


@pytest.mark.parametrize("timestamps", [(), (datetime(2026, 9, 26),)])
def test_segment_interval_rejects_empty_or_naive_timestamps(timestamps) -> None:
    with pytest.raises(ValueError, match="segment_time_invalid"):
        segment_interval(timestamps)
