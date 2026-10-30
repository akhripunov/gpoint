"""Pure tests for pre-Caddy Compose rescue decisions. Never mutate Docker."""
from dataclasses import fields, replace
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kamal_pause_recovery_policy", ROOT / "scripts/kamal_pause_recovery_policy.py"
)
policy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def evidence():
    guard = policy.PreCaddyEvidence(
        baseline_verified=True,
        exact_run_attempt_verified=True,
        caddy_active=True,
        caddy_sha_matches=True,
        postgres_identity_matches=True,
        kamal_resources_absent=True,
        handoff_not_started=True,
        gsales_stopped=True,
        sensitive_detector_unchanged=True,
    )
    containers = {}
    for n, name in enumerate(policy.RECOVERY_ORDER, start=1):
        cid = f"{n:064x}"
        image = "sha256:" + f"{n+10:064x}"
        containers[name] = policy.ContainerEvidence(
            container_id=cid,
            live_container_id=cid,
            image_id=image,
            live_image_id=image,
            compose_project="compose",
            compose_service=name,
            baseline_running=True,
            live_running=True,
        )
    return guard, containers


def test_noop_when_exact_legacy_services_are_running():
    guard, containers = evidence()
    assert policy.planned_legacy_rescue(guard, containers) == ()


def test_returns_only_exact_stopped_ids_in_order():
    guard, containers = evidence()
    for name in ("worker", "agata-api", "bot"):
        containers[name] = replace(containers[name], live_running=False)
    assert policy.planned_legacy_rescue(guard, containers) == (
        containers["agata-api"].container_id,
        containers["bot"].container_id,
        containers["worker"].container_id,
    )


@pytest.mark.parametrize("field", [f.name for f in fields(policy.PreCaddyEvidence)])
def test_every_missing_live_safety_gate_blocks(field):
    guard, containers = evidence()
    with pytest.raises(policy.PauseRecoveryBlocked, match="identity or handoff"):
        policy.planned_legacy_rescue(replace(guard, **{field: False}), containers)


@pytest.mark.parametrize("service", policy.RECOVERY_ORDER)
def test_every_missing_service_blocks(service):
    guard, containers = evidence()
    del containers[service]
    with pytest.raises(policy.PauseRecoveryBlocked, match="inventory"):
        policy.planned_legacy_rescue(guard, containers)


@pytest.mark.parametrize("service", policy.RECOVERY_ORDER)
def test_unexpected_service_blocks(service):
    guard, containers = evidence()
    containers[f"old-{service}"] = containers[service]
    with pytest.raises(policy.PauseRecoveryBlocked, match="inventory"):
        policy.planned_legacy_rescue(guard, containers)


@pytest.mark.parametrize("attr,value", [
    ("compose_project", "gsales"),
    ("compose_service", "gsales-api"),
    ("baseline_running", False),
    ("container_id", "wrong"),
    ("image_id", "a"*64),
    ("live_container_id", "f"*64),
    ("live_image_id", "sha256:"+"f"*64),
])
def test_each_legacy_identity_mismatch_blocks(attr, value):
    guard, containers = evidence()
    containers["api"] = replace(containers["api"], **{attr: value})
    with pytest.raises(policy.PauseRecoveryBlocked, match="identity"):
        policy.planned_legacy_rescue(guard, containers)


def test_duplicate_container_id_blocks_even_when_one_is_running():
    guard, containers = evidence()
    source = containers["api"]
    duplicate = containers["bot"]
    containers["bot"] = replace(
        duplicate, container_id=source.container_id,
        live_container_id=source.container_id,
    )
    with pytest.raises(policy.PauseRecoveryBlocked, match="duplicate"):
        policy.planned_legacy_rescue(guard, containers)


@pytest.mark.parametrize("bad_value", ["false", "true", 1, None])
def test_guard_facts_must_be_literal_booleans(bad_value):
    guard, containers = evidence()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.planned_legacy_rescue(replace(guard, baseline_verified=bad_value), containers)


@pytest.mark.parametrize("attribute,bad_value", [
    ("baseline_running", "true"),
    ("live_running", "false"),
    ("live_running", 0),
    ("live_running", None),
])
def test_container_states_must_be_literal_booleans(attribute, bad_value):
    guard, containers = evidence()
    containers["worker"] = replace(containers["worker"], **{attribute: bad_value})
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.planned_legacy_rescue(guard, containers)


def watchdog_evidence():
    return policy.WatchdogEvidence(
        exact_attempt_matches=True,
        independent_watchdog_armed=True,
        controller_alive=True,
        deadline_exceeded=False,
        caddy_active=True,
        kamal_resources_absent=True,
        caddy_backup_verified=False,
        post_caddy_guard_ready=False,
    )


@pytest.mark.parametrize("phase", list(policy.RecoveryPhase))
def test_live_controller_means_no_watchdog_intervention(phase):
    facts = watchdog_evidence()
    assert policy.decide_watchdog_owner(phase, facts) is policy.RescueDecision.NO_ACTION


@pytest.mark.parametrize("phase", list(policy.RecoveryPhase))
def test_unarmed_or_wrong_attempt_blocks_every_phase(phase):
    facts = watchdog_evidence()
    for bad in (
        replace(facts, independent_watchdog_armed=False, controller_alive=False),
        replace(facts, exact_attempt_matches=False, controller_alive=False),
    ):
        assert policy.decide_watchdog_owner(phase, bad) is policy.RescueDecision.BLOCKED


def test_dead_controller_before_caddy_handoff_uses_legacy_policy_only():
    facts = replace(watchdog_evidence(), controller_alive=False)
    assert policy.decide_watchdog_owner(
        policy.RecoveryPhase.PRE_CADDY, facts
    ) is policy.RescueDecision.INSPECT_AND_PLAN_LEGACY
    for changed in (
        replace(facts, caddy_active=False),
        replace(facts, kamal_resources_absent=False),
    ):
        assert policy.decide_watchdog_owner(
            policy.RecoveryPhase.PRE_CADDY, changed
        ) is policy.RescueDecision.BLOCKED


def test_dead_controller_after_handoff_never_falls_back_to_legacy():
    facts = replace(
        watchdog_evidence(),
        controller_alive=False,
        caddy_backup_verified=True,
        post_caddy_guard_ready=True,
    )
    for phase in (policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.POST_CADDY):
        assert policy.decide_watchdog_owner(phase, facts) is policy.RescueDecision.POST_CADDY_OWNER
        assert policy.decide_watchdog_owner(
            phase, replace(facts, caddy_active=False, kamal_resources_absent=False)
        ) is policy.RescueDecision.POST_CADDY_OWNER


@pytest.mark.parametrize("missing", ["caddy_backup_verified", "post_caddy_guard_ready"])
@pytest.mark.parametrize("phase", [
    policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.POST_CADDY,
])
def test_handoff_requires_both_recovery_evidence_items(phase, missing):
    facts = replace(
        watchdog_evidence(), controller_alive=False,
        caddy_backup_verified=True, post_caddy_guard_ready=True
    )
    assert policy.decide_watchdog_owner(
        phase, replace(facts, **{missing: False})
    ) is policy.RescueDecision.BLOCKED


@pytest.mark.parametrize("phase", list(policy.RecoveryPhase))
def test_deadline_can_trigger_even_if_controller_pid_still_exists(phase):
    facts = replace(watchdog_evidence(), deadline_exceeded=True)
    outcome = policy.decide_watchdog_owner(phase, facts)
    if phase is policy.RecoveryPhase.PRE_CADDY:
        assert outcome is policy.RescueDecision.INSPECT_AND_PLAN_LEGACY
    elif phase is policy.RecoveryPhase.VERIFIED:
        assert outcome is policy.RescueDecision.NO_ACTION
    else:
        assert outcome is policy.RescueDecision.BLOCKED


@pytest.mark.parametrize("phase", list(policy.RecoveryPhase))
def test_invalid_string_or_numeric_watchdog_facts_fail_closed(phase):
    facts = replace(watchdog_evidence(), controller_alive=False)
    for value in ("false", 1, None):
        assert policy.decide_watchdog_owner(
            phase, replace(facts, kamal_resources_absent=value)
        ) is policy.RescueDecision.BLOCKED


def test_unrecognized_recovery_phase_fails_closed():
    assert policy.decide_watchdog_owner(
        "pre_caddy", watchdog_evidence()
    ) is policy.RescueDecision.BLOCKED


def test_verified_phase_never_requests_legacy_recovery():
    facts = replace(
        watchdog_evidence(), controller_alive=False, deadline_exceeded=True,
        caddy_active=False, kamal_resources_absent=False
    )
    assert policy.decide_watchdog_owner(
        policy.RecoveryPhase.VERIFIED, facts
    ) is policy.RescueDecision.NO_ACTION


def test_no_recovery_in_post_caddy_or_kamal_activated_state():
    guard, containers = evidence()
    containers["worker"] = replace(containers["worker"], live_running=False)
    for wrong in (
        replace(guard, caddy_active=False),
        replace(guard, kamal_resources_absent=False),
        replace(guard, handoff_not_started=False),
    ):
        with pytest.raises(policy.PauseRecoveryBlocked):
            policy.planned_legacy_rescue(wrong, containers)


def handoff_proof():
    return policy.HandoffProof(
        exact_attempt=True,
        independent_guard_ready=True,
        legacy_consumers_stopped=False,
        original_caddy_active=True,
        caddy_retires_confirmed=False,
        caddy_backup_verified=False,
        post_caddy_recovery_ready=False,
        kamal_proxy_active=False,
        replacement_consumers_healthy=False,
        full_runtime_verify_passed=False,
    )


def safe_handoff_chain():
    """Proofs from distinct observed stages, not one stale snapshot."""
    early = replace(
        handoff_proof(),
        legacy_consumers_stopped=True,
        caddy_backup_verified=True,
        post_caddy_recovery_ready=True,
    )
    retired = replace(
        early, original_caddy_active=False, caddy_retires_confirmed=True
    )
    verified = replace(
        retired, kamal_proxy_active=True,
        replacement_consumers_healthy=True, full_runtime_verify_passed=True
    )
    return early, retired, verified


def test_handoff_requires_three_ordered_phase_proofs():
    early, retired, verified = safe_handoff_chain()
    assert policy.validate_phase_transition(
        policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.HANDOFF_PENDING, early
    ) is policy.RecoveryPhase.HANDOFF_PENDING
    assert policy.validate_phase_transition(
        policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.POST_CADDY, retired
    ) is policy.RecoveryPhase.POST_CADDY
    assert policy.validate_phase_transition(
        policy.RecoveryPhase.POST_CADDY, policy.RecoveryPhase.VERIFIED, verified
    ) is policy.RecoveryPhase.VERIFIED


@pytest.mark.parametrize("missing", [
    "legacy_consumers_stopped", "original_caddy_active",
    "caddy_backup_verified", "post_caddy_recovery_ready",
    "exact_attempt", "independent_guard_ready",
])
def test_no_pre_caddy_handoff_without_all_proofs(missing):
    early, _, _ = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.HANDOFF_PENDING,
            replace(early, **{missing: False})
        )


@pytest.mark.parametrize("missing", [
    "legacy_consumers_stopped", "caddy_retires_confirmed",
    "caddy_backup_verified", "post_caddy_recovery_ready",
    "exact_attempt", "independent_guard_ready",
])
def test_no_post_caddy_ownership_without_all_proofs(missing):
    _, retired, _ = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.POST_CADDY,
            replace(retired, **{missing: False})
        )


@pytest.mark.parametrize("missing", [
    "legacy_consumers_stopped", "caddy_retires_confirmed",
    "kamal_proxy_active", "replacement_consumers_healthy",
    "full_runtime_verify_passed", "exact_attempt", "independent_guard_ready",
])
def test_no_verified_state_without_all_proofs(missing):
    _, _, verified = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.POST_CADDY, policy.RecoveryPhase.VERIFIED,
            replace(verified, **{missing: False})
        )


@pytest.mark.parametrize("from_phase,to_phase", [
    (policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.VERIFIED),
    (policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.POST_CADDY),
    (policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.VERIFIED),
    (policy.RecoveryPhase.VERIFIED, policy.RecoveryPhase.PRE_CADDY),
    (policy.RecoveryPhase.VERIFIED, policy.RecoveryPhase.POST_CADDY),
    (policy.RecoveryPhase.POST_CADDY, policy.RecoveryPhase.HANDOFF_PENDING),
    (policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.PRE_CADDY),
])
def test_no_skipped_or_backward_handoff(from_phase, to_phase):
    _, _, verified = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(from_phase, to_phase, verified)


@pytest.mark.parametrize("bad", [None, 1, "true", "false"])
def test_untyped_or_missing_handoff_proof_rejected(bad):
    early, _, _ = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.HANDOFF_PENDING,
            replace(early, post_caddy_recovery_ready=bad)
        )


def test_caddy_drift_or_early_kamal_proxy_blocks_handoff():
    early, retired, verified = safe_handoff_chain()
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.PRE_CADDY, policy.RecoveryPhase.HANDOFF_PENDING,
            replace(early, kamal_proxy_active=True)
        )
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.HANDOFF_PENDING, policy.RecoveryPhase.POST_CADDY,
            replace(retired, original_caddy_active=True)
        )
    with pytest.raises(policy.PauseRecoveryBlocked):
        policy.validate_phase_transition(
            policy.RecoveryPhase.POST_CADDY, policy.RecoveryPhase.VERIFIED,
            replace(verified, original_caddy_active=True)
        )
