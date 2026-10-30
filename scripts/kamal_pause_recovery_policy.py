"""Pure, fail-closed policy for pre-Caddy Kamal cutover rescue.

This module never makes system calls and never changes container state.
The caller must separately validate root-owned immutable baseline evidence,
live Docker identities, and status immediately before any recovery action.
"""
from __future__ import annotations

from dataclasses import dataclass
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
