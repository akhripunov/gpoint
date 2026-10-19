import pytest

from app.jobs.outbox import retry_budget_for_job


@pytest.mark.parametrize(
    ("job_type", "payload", "expected"),
    [
        ("relationship_note_refresh", {}, 3),
        ("snapshot_refresh", {}, 3),
        ("conversation_summary", {}, 3),
        ("memory_extract", {}, 3),
        ("memory_promote", {}, 3),
        ("memory_embed", {}, 3),
        ("memory_graph_sync", {}, 3),
        ("inference", {"purpose": "memory_extract"}, 1),
        ("inference", {"purpose": "conversation_summary"}, 1),
        ("inference", {"purpose": "response"}, 2),
        ("inference", {}, 2),
        ("sales_draft_inference", {"purpose": "response"}, 5),
        ("generic", {}, 5),
        ("response_retry", {}, 5),
    ],
)
def test_retry_budget_for_job_matches_worker_failure_policy(job_type, payload, expected):
    assert retry_budget_for_job(job_type, payload) == expected
