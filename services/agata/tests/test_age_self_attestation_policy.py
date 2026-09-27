"""Self-attestation remains distinct from external identity verification."""

from datetime import UTC, datetime

import pytest

from app.compliance.policy import PolicyService


@pytest.mark.parametrize(
    ("allowed", "status", "asserted_at", "expires_at", "expected"),
    [
        (True, "self_attested", datetime(2026, 9, 27, tzinfo=UTC), None, True),
        (False, "self_attested", datetime(2026, 9, 27, tzinfo=UTC), None, False),
        (True, "self_attested", datetime(2026, 9, 27, tzinfo=UTC), datetime(2026, 9, 27, tzinfo=UTC), False),
        (True, "rejected", None, None, False),
        (True, "verified", datetime(2026, 9, 27, tzinfo=UTC), None, False),
    ],
)
def test_self_attestation_is_policy_gated_and_never_masquerades_as_verified(
    allowed, status, asserted_at, expires_at, expected
):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    rules = {
        "adult_only_features": ["romantic", "sexual"],
        "ai_disclosure_required": False,
        "self_attestation_allowed": allowed,
    }
    decision = PolicyService().feature_decision(
        rules=rules,
        policy_version=1,
        feature="romantic",
        age_band="adult",
        age_status=status,
        verified_at=None,
        asserted_at=asserted_at,
        expires_at=expires_at,
        disclosure_current=True,
        now=now,
    )
    assert decision.allowed is expected
