from app.compliance.receipt import onboarding_is_complete


def test_onboarding_requires_acceptance_for_the_current_notice_version():
    assert not onboarding_is_complete(required=True, policy_version=3, accepted_version=None)
    assert onboarding_is_complete(required=True, policy_version=3, accepted_version=3)
    assert not onboarding_is_complete(required=True, policy_version=4, accepted_version=3)


def test_onboarding_is_not_required_when_policy_does_not_require_notice():
    assert onboarding_is_complete(required=False, policy_version=None, accepted_version=None)
