from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgeAssurance, RegionalPolicyProfile

SAFETY_USE_PURPOSES = frozenset({"safe_response", "present_resources", "escalate"})
VALID_CRISIS_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
VALID_AGE_BANDS = frozenset({"unknown", "minor", "adult"})
VALID_AGE_STATUSES = frozenset({"pending", "verified", "expired", "rejected"})
VALID_POLICY_STATUSES = frozenset({"draft", "active", "retired"})
DEFAULT_DISCLOSURE_REPEAT_HOURS = 24
DEFAULT_BREAK_INTERVAL_MINUTES = 180


@dataclass(frozen=True)
class Decision:
    allowed: bool
    outcome: str
    reason_codes: tuple[str, ...]
    policy_version: int


class PolicyService:
    """Pure deterministic interpreter: every public boundary validates untrusted input."""

    async def age_assurance_eligible(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, now: datetime
    ) -> bool:
        """Authorize adult access without conflating self-attestation with verification."""
        if not isinstance(tenant_id, UUID) or not isinstance(user_id, UUID) or not self._aware(now):
            return False
        age = await session.scalar(
            select(AgeAssurance)
            .where(
                AgeAssurance.tenant_id == tenant_id,
                AgeAssurance.user_id == user_id,
                AgeAssurance.age_band == "adult",
                AgeAssurance.expires_at.is_(None) | (AgeAssurance.expires_at > now),
            )
            .order_by(AgeAssurance.verified_at.desc(), AgeAssurance.id.desc())
            .limit(1)
            .with_for_update()
        )
        if age is None:
            return False
        if age.status == "verified":
            return True
        if age.status != "self_attested" or age.method != "self_attested":
            return False
        policy = await session.scalar(
            select(RegionalPolicyProfile)
            .where(
                RegionalPolicyProfile.tenant_id == tenant_id,
                RegionalPolicyProfile.status == "active",
                RegionalPolicyProfile.effective_from <= now,
                RegionalPolicyProfile.effective_to.is_(None)
                | (RegionalPolicyProfile.effective_to > now),
                RegionalPolicyProfile.rules["self_attestation_allowed"].as_boolean().is_(True),
            )
            .order_by(RegionalPolicyProfile.effective_from.desc(), RegionalPolicyProfile.id.desc())
            .limit(1)
            .with_for_update()
        )
        return policy is not None and age.jurisdiction == policy.jurisdiction

    def feature_decision(
        self,
        *,
        rules: object,
        policy_version: object,
        feature: object,
        age_band: object,
        age_status: object,
        verified_at: object,
        expires_at: object,
        disclosure_current: object,
        now: object,
    ) -> Decision:
        version = self._version_or_none(policy_version)
        if version is None:
            return self._invalid("invalid_policy_version", 0)
        if not self._valid_rules(rules):
            return self._invalid("policy_configuration_invalid", version)
        if not self._aware(now) or not self._optional_aware(verified_at, expires_at):
            return self._invalid("invalid_time_input", version)
        if not isinstance(feature, str):
            return self._invalid("invalid_feature", version)
        if not self._valid_optional_enum(
            age_band, VALID_AGE_BANDS
        ) or not self._valid_optional_enum(age_status, VALID_AGE_STATUSES):
            return self._invalid("policy_configuration_invalid", version)
        if not isinstance(disclosure_current, bool):
            return self._invalid("policy_configuration_invalid", version)
        assert type(rules) is dict and isinstance(now, datetime)
        restricted = {"romantic", "sexual"} | set(rules.get("adult_only_features", []))
        age_is_current = (
            isinstance(verified_at, datetime)
            and age_band == "adult"
            and age_status == "verified"
            and verified_at <= now
            and (expires_at is None or (isinstance(expires_at, datetime) and expires_at > now))
        )
        if feature in restricted and not age_is_current:
            return Decision(False, "deny", ("adult_age_assurance_required",), version)
        if rules.get("ai_disclosure_required", False) and not disclosure_current:
            return Decision(False, "deny", ("ai_disclosure_required",), version)
        return Decision(True, "allow", ("policy_allow",), version)

    def select_active_policy(self, *, profiles: object, now: object) -> Decision:
        if not self._aware(now):
            return self._invalid("invalid_time_input", 0)
        if type(profiles) not in (list, tuple):
            return self._invalid("policy_configuration_invalid", 0)
        assert isinstance(profiles, (list, tuple)) and isinstance(now, datetime)
        applicable: list[dict[object, object]] = []
        for profile in profiles:
            if type(profile) is not dict or not self._valid_profile(profile):
                return self._invalid("policy_configuration_invalid", 0)
            effective_from = profile["effective_from"]
            effective_to = profile["effective_to"]
            assert isinstance(effective_from, datetime)
            assert effective_to is None or isinstance(effective_to, datetime)
            if (
                profile["status"] == "active"
                and effective_from <= now
                and (effective_to is None or effective_to > now)
            ):
                applicable.append(profile)
        if len(applicable) != 1:
            return self._invalid("policy_configuration_invalid", 0)
        version = applicable[0]["version"]
        assert isinstance(version, int)
        return Decision(True, "allow", ("active_policy_selected",), version)

    def disclosure_required(
        self, *, rules: object, policy_version: object, last_displayed_at: object, now: object
    ) -> Decision:
        version = self._version_or_none(policy_version)
        if version is None:
            return self._invalid("invalid_policy_version", 0)
        if not self._valid_rules(rules):
            return self._invalid("policy_configuration_invalid", version)
        if not self._aware(now) or not self._optional_aware(last_displayed_at):
            return self._invalid("invalid_time_input", version)
        assert type(rules) is dict and isinstance(now, datetime)
        if not rules.get("ai_disclosure_required", False):
            return Decision(False, "allow", ("disclosure_not_required",), version)
        interval_hours = rules.get("disclosure_repeat_hours", DEFAULT_DISCLOSURE_REPEAT_HOURS)
        assert isinstance(interval_hours, int)
        if last_displayed_at is None:
            return Decision(True, "remind", ("onboarding_disclosure_required",), version)
        assert isinstance(last_displayed_at, datetime)
        if now >= last_displayed_at + timedelta(hours=interval_hours):
            return Decision(True, "remind", ("periodic_disclosure_required",), version)
        return Decision(False, "allow", ("disclosure_current",), version)

    def break_reminder(
        self,
        *,
        rules: object,
        policy_version: object,
        started_at: object,
        last_reminder_at: object,
        now: object,
    ) -> Decision:
        version = self._version_or_none(policy_version)
        if version is None:
            return self._invalid("invalid_policy_version", 0)
        if not self._valid_rules(rules):
            return self._invalid("policy_configuration_invalid", version)
        if (
            not self._aware(now)
            or not self._aware(started_at)
            or not self._optional_aware(last_reminder_at)
        ):
            return self._invalid("invalid_time_input", version)
        assert (
            type(rules) is dict and isinstance(now, datetime) and isinstance(started_at, datetime)
        )
        interval_minutes = rules.get("break_interval_minutes", DEFAULT_BREAK_INTERVAL_MINUTES)
        assert isinstance(interval_minutes, int)
        interval = timedelta(minutes=interval_minutes)
        if now - started_at < interval:
            return Decision(False, "allow", ("break_interval_not_reached",), version)
        if isinstance(last_reminder_at, datetime) and now - last_reminder_at < interval:
            return Decision(False, "allow", ("break_reminder_already_current",), version)
        return Decision(True, "remind", ("continuous_session_break_due",), version)

    def crisis_decision(
        self, *, policy_version: object, severity: object, resources: object
    ) -> Decision:
        version = self._version_or_none(policy_version)
        if version is None:
            return self._invalid("invalid_policy_version", 0)
        if not isinstance(severity, str) or severity not in VALID_CRISIS_SEVERITIES:
            return self._invalid("invalid_severity", version)
        if type(resources) not in (list, tuple):
            return self._invalid("invalid_resources", version)
        assert isinstance(resources, (list, tuple))
        if not all(type(resource) is dict for resource in resources):
            return self._invalid("invalid_resources", version)
        if resources:
            outcome = "escalate" if severity in {"high", "critical"} else "allow"
            return Decision(True, outcome, ("present_versioned_local_resources",), version)
        return Decision(
            True, "allow", ("safe_resource_fallback", "no_third_party_contact"), version
        )

    def safety_use_decision(self, *, policy_version: object, purpose: object) -> Decision:
        version = self._version_or_none(policy_version)
        if version is None:
            return self._invalid("invalid_policy_version", 0)
        if not isinstance(purpose, str) or purpose not in SAFETY_USE_PURPOSES:
            return self._invalid("invalid_purpose", version)
        return Decision(True, "allow", ("safety_use_permitted",), version)

    @staticmethod
    def _aware(value: object) -> bool:
        return (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )

    @classmethod
    def _optional_aware(cls, *values: object) -> bool:
        return all(value is None or cls._aware(value) for value in values)

    @staticmethod
    def _version_or_none(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    @staticmethod
    def _valid_optional_enum(value: object, allowed: frozenset[str]) -> bool:
        return value is None or (isinstance(value, str) and value in allowed)

    @classmethod
    def _valid_rules(cls, rules: object) -> bool:
        if type(rules) is not dict:
            return False
        allowed_keys = {
            "adult_only_features",
            "ai_disclosure_required",
            "disclosure_repeat_hours",
            "break_interval_minutes",
        }
        for key in rules:
            if not isinstance(key, str) or key not in allowed_keys:
                return False
        features = rules.get("adult_only_features", [])
        if type(features) is not list or not all(isinstance(value, str) for value in features):
            return False
        for key in ("disclosure_repeat_hours", "break_interval_minutes"):
            if key in rules and (
                not isinstance(rules[key], int) or isinstance(rules[key], bool) or rules[key] <= 0
            ):
                return False
        return "ai_disclosure_required" not in rules or isinstance(
            rules["ai_disclosure_required"], bool
        )

    @classmethod
    def _valid_profile(cls, profile: dict[object, object]) -> bool:
        required = {"status", "effective_from", "effective_to", "rules", "version"}
        if len(profile) != len(required):
            return False
        if any(not isinstance(key, str) or key not in required for key in profile):
            return False
        if any(key not in profile for key in required):
            return False
        status = profile["status"]
        effective_from = profile["effective_from"]
        effective_to = profile["effective_to"]
        if not isinstance(effective_from, datetime):
            return False
        return (
            isinstance(status, str)
            and status in VALID_POLICY_STATUSES
            and cls._aware(effective_from)
            and cls._optional_aware(effective_to)
            and (
                effective_to is None
                or (isinstance(effective_to, datetime) and effective_to > effective_from)
            )
            and cls._valid_rules(profile["rules"])
            and cls._version_or_none(profile["version"]) is not None
        )

    @staticmethod
    def _invalid(reason_code: str, policy_version: int) -> Decision:
        return Decision(False, "deny", (reason_code,), policy_version)


def training_consent_default() -> bool:
    """No consent evidence means no training use."""
    return False


def utc_now() -> datetime:
    return datetime.now(UTC)
