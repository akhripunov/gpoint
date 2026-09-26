"""Canonical tenant/user/policy-bound disclosure receipt lookup."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import AIDisclosureReceipt, RegionalPolicyProfile


@dataclass(frozen=True, slots=True)
class CurrentDisclosure:
    policy: RegionalPolicyProfile
    receipt: AIDisclosureReceipt | None


async def current_disclosure(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    now: datetime | None = None,
    lock: bool = False,
) -> CurrentDisclosure | None:
    """Resolve the active policy and its tenant/user/policy-version receipt."""
    if not isinstance(tenant_id, UUID) or not isinstance(user_id, UUID):
        raise ValueError("invalid_disclosure_principal")
    instant = datetime.now(UTC) if now is None else now
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("invalid_disclosure_time")
    await set_tenant_context(session, str(tenant_id))
    query = (
        select(RegionalPolicyProfile)
        .where(
            RegionalPolicyProfile.tenant_id == tenant_id,
            RegionalPolicyProfile.status == "active",
            RegionalPolicyProfile.effective_from <= instant,
            (
                RegionalPolicyProfile.effective_to.is_(None)
                | (RegionalPolicyProfile.effective_to > instant)
            ),
            RegionalPolicyProfile.rules["ai_disclosure_required"].as_boolean().is_(True),
        )
        .order_by(RegionalPolicyProfile.effective_from.desc(), RegionalPolicyProfile.id.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update()
    policy = await session.scalar(query)
    if policy is None:
        return None
    receipt_query = (
        select(AIDisclosureReceipt)
        .where(
            AIDisclosureReceipt.tenant_id == tenant_id,
            AIDisclosureReceipt.user_id == user_id,
            AIDisclosureReceipt.policy_profile_id == policy.id,
            AIDisclosureReceipt.policy_version == policy.version,
        )
        .order_by(AIDisclosureReceipt.displayed_at.desc(), AIDisclosureReceipt.id.desc())
        .limit(1)
    )
    if lock:
        receipt_query = receipt_query.with_for_update()
    receipt = await session.scalar(receipt_query)
    return CurrentDisclosure(policy=policy, receipt=receipt)


async def require_current_disclosure(
    session: AsyncSession, *, tenant_id: UUID, user_id: UUID, lock: bool = False
) -> CurrentDisclosure:
    """Return current disclosure or a bounded denial signal for callers."""
    current = await current_disclosure(session, tenant_id=tenant_id, user_id=user_id, lock=lock)
    if current is None or current.receipt is None:
        raise ValueError("disclosure_not_current")
    return current
