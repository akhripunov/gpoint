"""Pure, fail-closed policy for pre-Caddy Kamal cutover rescue.

This module never makes system calls and never changes container state.
The caller must separately validate root-owned immutable baseline evidence,
live Docker identities, and status immediately before any recovery action.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping

RECOVERY_ORDER = ("agata-api", "api", "agata-memory-worker", "bot", "worker")
REQUIRED = frozenset(RECOVERY_ORDER)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class PauseRecoveryBlocked(ValueError):
    """No legacy restart can be authorized from these facts."""


@dataclass(frozen=True)
class ContainerEvidence:
    container_id: str
    live_container_id: str
    image_id: str
    live_image_id: str
    compose_project: str
    compose_service: str
    baseline_running: bool
    live_running: bool


@dataclass(frozen=True)
class PreCaddyEvidence:
    baseline_verified: bool
    exact_run_attempt_verified: bool
    caddy_active: bool
    caddy_sha_matches: bool
    postgres_identity_matches: bool
    kamal_resources_absent: bool
    handoff_not_started: bool
    gsales_stopped: bool
    sensitive_detector_unchanged: bool


def planned_legacy_rescue(
    guards: PreCaddyEvidence,
    containers: Mapping[str, ContainerEvidence],
) -> tuple[str, ...]:
    """Return ONLY exact baseline IDs eligible for a pre-Caddy rescue.

    The tuple is an inspection/planning result, not authorization to bypass
    the production runbook or a substitute for independent watchdog testing.
    """
    if not all(flag is True for flag in (
        guards.baseline_verified,
        guards.exact_run_attempt_verified,
        guards.caddy_active,
        guards.caddy_sha_matches,
        guards.postgres_identity_matches,
        guards.kamal_resources_absent,
        guards.handoff_not_started,
        guards.gsales_stopped,
        guards.sensitive_detector_unchanged,
    )):
        raise PauseRecoveryBlocked("pre-Caddy identity or handoff gate failed")
    if set(containers) != REQUIRED:
        raise PauseRecoveryBlocked("legacy service inventory is incomplete or ambiguous")

    selected: dict[str, str] = {}
    seen_ids: set[str] = set()
    for service in RECOVERY_ORDER:
        item = containers[service]
        if (item.compose_project != "compose"
                or item.compose_service != service
                or item.baseline_running is not True
                or type(item.live_running) is not bool
                or not _HEX64.fullmatch(item.container_id)
                or not _IMAGE_ID.fullmatch(item.image_id)
                or item.live_container_id != item.container_id
                or item.live_image_id != item.image_id):
            raise PauseRecoveryBlocked("legacy service identity mismatches baseline")
        if item.container_id in seen_ids:
            raise PauseRecoveryBlocked("duplicate legacy container identity")
        seen_ids.add(item.container_id)
        if not item.live_running:
            selected[service] = item.container_id
    return tuple(selected[service] for service in RECOVERY_ORDER if service in selected)


class RecoveryPhase(str, Enum):
    """Durably recorded ownership of the rollback responsibility."""

    PRE_CADDY = "pre_caddy"
    HANDOFF_PENDING = "handoff_pending"
    POST_CADDY = "post_caddy"
    VERIFIED = "verified"


class RescueDecision(str, Enum):
    NO_ACTION = "no_action"
    INSPECT_AND_PLAN_LEGACY = "inspect_and_plan_legacy"
    POST_CADDY_OWNER = "post_caddy_owner"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WatchdogEvidence:
    """Evidence gathered independently, never accepted from the SSH caller alone."""

    exact_attempt_matches: bool
    independent_watchdog_armed: bool
    controller_alive: bool
    deadline_exceeded: bool
    caddy_active: bool
    kamal_resources_absent: bool
    caddy_backup_verified: bool
    post_caddy_guard_ready: bool


def decide_watchdog_owner(
    phase: RecoveryPhase, facts: WatchdogEvidence
) -> RescueDecision:
    """Pure routing decision. No health claim or Docker action follows from it.

    A post-Caddy handoff must not fall back to pre-Caddy Compose recovery,
    even if Caddy is active again. Recovery code must re-read exact identities.
    """
    if not isinstance(phase, RecoveryPhase):
        return RescueDecision.BLOCKED
    if not all(type(getattr(facts, key)) is bool for key in (
        "exact_attempt_matches", "independent_watchdog_armed",
        "controller_alive", "deadline_exceeded", "caddy_active",
        "kamal_resources_absent", "caddy_backup_verified",
        "post_caddy_guard_ready",
    )):
        return RescueDecision.BLOCKED
    if not facts.exact_attempt_matches or not facts.independent_watchdog_armed:
        return RescueDecision.BLOCKED
    if phase is RecoveryPhase.VERIFIED:
        return RescueDecision.NO_ACTION
    if facts.controller_alive and not facts.deadline_exceeded:
        return RescueDecision.NO_ACTION
    if phase is RecoveryPhase.PRE_CADDY:
        if facts.caddy_active and facts.kamal_resources_absent:
            return RescueDecision.INSPECT_AND_PLAN_LEGACY
        return RescueDecision.BLOCKED
    if phase is RecoveryPhase.HANDOFF_PENDING:
        # The same-run Caddy backup and new recovery owner must be ready
        # BEFORE either old Caddy or the old consumers may be handed over.
        if not (facts.caddy_backup_verified and facts.post_caddy_guard_ready):
            return RescueDecision.BLOCKED
        return RescueDecision.POST_CADDY_OWNER
    if phase is RecoveryPhase.POST_CADDY:
        if facts.caddy_backup_verified and facts.post_caddy_guard_ready:
            return RescueDecision.POST_CADDY_OWNER
        return RescueDecision.BLOCKED
    return RescueDecision.BLOCKED
