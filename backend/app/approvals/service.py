"""
Approval flow orchestration. A human gate, not a second policy engine.

Resolving a `NEEDS_APPROVAL` decision:

    load decision  (FOR UPDATE)           404 if missing
    verify verdict is NEEDS_APPROVAL       409 if not
    verify no Approval row exists yet      409 if already resolved
    INSERT approval (outcome, approver, reason)
    audit APPROVAL_RESOLVED
    commit once

The approval never touches `Decision.verdict / rule_id / reason /
policy_version` or the `ActionRequest`; the deterministic policy decision stays
historical truth. It creates no `PaymentAttempt`.

"Pending" is not a stored state: it is a `NEEDS_APPROVAL` `Decision` with no
`Approval` row. The `Approval` row is created only at resolution, and
`uq_approval_decision` makes a second resolution impossible at the database
level.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.approvals.models import Approval
from app.approvals.schemas import (
    ApprovalResolutionResponse,
    PendingApprovalItem,
)
from app.audit import append_audit_event, events
from app.catalog.models import Product
from app.core.enums import ApprovalOutcome, Verdict
from app.policy.models import Decision


class ApprovalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DecisionNotFound(ApprovalError):
    """No decision with that id. Maps to HTTP 404."""


class ApprovalConflict(ApprovalError):
    """Decision is not awaiting approval, or is already resolved. Maps to HTTP 409."""


async def list_pending_approvals(session: AsyncSession) -> list[PendingApprovalItem]:
    """NEEDS_APPROVAL decisions with no Approval row, oldest first."""
    resolved_ids = select(Approval.decision_id)
    rows = (
        await session.execute(
            select(Decision, ActionRequest, Agent, Product)
            .join(ActionRequest, Decision.action_request_id == ActionRequest.id)
            .join(Agent, ActionRequest.agent_id == Agent.id)
            .join(Product, ActionRequest.product_id == Product.id)
            .where(Decision.verdict == Verdict.NEEDS_APPROVAL)
            .where(Decision.id.not_in(resolved_ids))
            .order_by(Decision.created_at.asc())
        )
    ).all()

    return [
        PendingApprovalItem(
            decision_id=decision.id,
            action_request_id=action_request.id,
            policy_version=decision.policy_version,
            original_rule_id=decision.policy_rule_id,
            original_reason=decision.reason,
            decision_created_at=decision.created_at,
            agent_id=agent.id,
            agent_name=agent.name,
            product_id=product.id,
            product_name=product.name,
            product_price=product.price,
            action_type=action_request.action_type,
            quantity=action_request.requested_quantity,
            requested_discount_pct=action_request.requested_discount_pct,
            proposed_price=action_request.proposed_price,
        )
        for decision, action_request, agent, product in rows
    ]


async def resolve_approval(
    session: AsyncSession,
    *,
    decision_id,
    outcome: ApprovalOutcome,
    approver: str,
    reason: str | None,
) -> ApprovalResolutionResponse:
    # Row lock: concurrent resolutions for the same decision serialise here.
    decision = (
        await session.execute(
            select(Decision).where(Decision.id == decision_id).with_for_update()
        )
    ).scalar_one_or_none()
    if decision is None:
        raise DecisionNotFound("DECISION_NOT_FOUND", f"No decision with id {decision_id}")

    if decision.verdict is not Verdict.NEEDS_APPROVAL:
        raise ApprovalConflict(
            "DECISION_NOT_PENDING_APPROVAL",
            f"Decision {decision_id} has verdict {decision.verdict.value}; "
            f"only NEEDS_APPROVAL decisions can be resolved.",
        )

    existing = (
        await session.execute(
            select(Approval).where(Approval.decision_id == decision_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ApprovalConflict(
            "APPROVAL_ALREADY_RESOLVED",
            f"Decision {decision_id} was already {existing.outcome.value.lower()} "
            f"by {existing.approver}.",
        )

    approval = Approval(
        decision_id=decision.id,
        approver=approver,
        outcome=outcome,
        reason=reason,
    )
    session.add(approval)
    try:
        await session.flush()  # uq_approval_decision is the final backstop
    except IntegrityError as exc:
        # A racing transaction resolved it between our check and here.
        raise ApprovalConflict(
            "APPROVAL_ALREADY_RESOLVED",
            f"Decision {decision_id} was resolved concurrently.",
        ) from exc

    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=decision.action_request_id,
        event_type=events.APPROVAL_RESOLVED,
        payload={
            "approval_id": approval.id,
            "decision_id": decision.id,
            "action_request_id": decision.action_request_id,
            "outcome": outcome.value,
            "approver": approver,
            "reason": reason,
            "original_verdict": decision.verdict.value,
            "original_rule_id": decision.policy_rule_id,
            "policy_version": decision.policy_version,
        },
    )

    await session.commit()  # the one and only transaction boundary

    return ApprovalResolutionResponse(
        approval_id=approval.id,
        decision_id=decision.id,
        action_request_id=decision.action_request_id,
        outcome=approval.outcome,
        approver=approval.approver,
        reason=approval.reason,
        resolved_at=approval.created_at,
    )
