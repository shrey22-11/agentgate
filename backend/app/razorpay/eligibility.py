"""
The one authoritative answer to "may this decision be executed?".

Nothing else in the codebase re-implements this condition. Both the execution
service and its tests call `can_execute`.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals.models import Approval
from app.core.enums import ApprovalOutcome, Verdict
from app.policy.models import Decision

# reason codes
ALLOW_VERDICT = "ALLOW_VERDICT"
NEEDS_APPROVAL_APPROVED = "NEEDS_APPROVAL_APPROVED"
NEEDS_APPROVAL_NOT_APPROVED = "NEEDS_APPROVAL_NOT_APPROVED"
NEEDS_APPROVAL_REJECTED = "NEEDS_APPROVAL_REJECTED"
VERDICT_NOT_EXECUTABLE = "VERDICT_NOT_EXECUTABLE"


@dataclass(frozen=True)
class ExecutionEligibility:
    eligible: bool
    reason_code: str
    # The approval that authorises execution — only set when eligible via the
    # NEEDS_APPROVAL path.
    approval: Approval | None = None


async def can_execute(session: AsyncSession, decision: Decision) -> ExecutionEligibility:
    if decision.verdict is Verdict.ALLOW:
        return ExecutionEligibility(True, ALLOW_VERDICT)

    if decision.verdict is Verdict.NEEDS_APPROVAL:
        approval = (
            await session.scalars(
                select(Approval).where(Approval.decision_id == decision.id)
            )
        ).one_or_none()
        if approval is None:
            return ExecutionEligibility(False, NEEDS_APPROVAL_NOT_APPROVED)
        if approval.outcome is ApprovalOutcome.APPROVED:
            return ExecutionEligibility(True, NEEDS_APPROVAL_APPROVED, approval)
        return ExecutionEligibility(False, NEEDS_APPROVAL_REJECTED)

    # DENY, COUNTER_OFFER
    return ExecutionEligibility(False, VERDICT_NOT_EXECUTABLE)
