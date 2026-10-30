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
