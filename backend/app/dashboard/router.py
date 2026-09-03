"""
GET /dashboard/summary — aggregate counts + recent decisions for the merchant
dashboard (Phase 11). Read-only, one round-trip.

Honest labelling (docs §9): these are counts of OUR SYSTEM's decisions on
SIMULATED catalogue/agent data. No business-impact figures.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.approvals.models import Approval
from app.audit.models import AuditEvent
from app.audit.verify import verify_audit_chain
from app.catalog.models import Product
from app.core.db import get_db
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class RecentDecision(BaseModel):
    decision_id: uuid.UUID
    action_request_id: uuid.UUID
    verdict: str
    rule_id: str
    reason: str
    policy_version: str
    created_at: dt.datetime
    agent_name: str | None
    product_name: str | None
    counter_offer_price: Decimal | None


class DashboardSummary(BaseModel):
    action_requests_total: int
    decisions_by_verdict: dict[str, int]
    action_requests_by_status: dict[str, int]
    approvals_pending: int
    approvals_resolved: dict[str, int]
    payments_by_status: dict[str, int]
    audit_events: int
    audit_chain_valid: bool
    recent_decisions: list[RecentDecision]


async def _counts(session: AsyncSession, column) -> dict[str, int]:
    rows = await session.execute(select(column, func.count()).group_by(column))
    return {str(getattr(k, "value", k)): n for k, n in rows.all()}


@router.get("/summary", response_model=DashboardSummary)
async def summary(session: AsyncSession = Depends(get_db)) -> DashboardSummary:
    action_requests_total = (
        await session.scalar(select(func.count()).select_from(ActionRequest))
    ) or 0
    decisions_by_verdict = await _counts(session, Decision.verdict)
    for verdict in ("ALLOW", "DENY", "NEEDS_APPROVAL", "COUNTER_OFFER"):
        decisions_by_verdict.setdefault(verdict, 0)

    ar_by_status = await _counts(session, ActionRequest.status)

    resolved_ids = select(Approval.decision_id)
    approvals_pending = (
        await session.scalar(
            select(func.count())
            .select_from(Decision)
            .where(Decision.verdict == "NEEDS_APPROVAL")
            .where(Decision.id.not_in(resolved_ids))
        )
    ) or 0
    approvals_resolved = await _counts(session, Approval.outcome)
    for outcome in ("APPROVED", "REJECTED"):
        approvals_resolved.setdefault(outcome, 0)

    payments_by_status = await _counts(session, PaymentAttempt.status)
    audit_events = (
        await session.scalar(select(func.count()).select_from(AuditEvent))
    ) or 0
    chain = await verify_audit_chain(session)

    recent_rows = (
        await session.execute(
            select(Decision, ActionRequest, Agent, Product)
            .join(ActionRequest, Decision.action_request_id == ActionRequest.id)
            .join(Agent, ActionRequest.agent_id == Agent.id, isouter=True)
            .join(Product, ActionRequest.product_id == Product.id, isouter=True)
            .order_by(Decision.created_at.desc())
            .limit(12)
        )
    ).all()
    recent = [
        RecentDecision(
            decision_id=d.id,
            action_request_id=d.action_request_id,
            verdict=d.verdict.value,
            rule_id=d.policy_rule_id,
            reason=d.reason,
            policy_version=d.policy_version,
            created_at=d.created_at,
            agent_name=agent.name if agent else None,
            product_name=product.name if product else None,
            counter_offer_price=d.counter_offer_price,
        )
        for d, _ar, agent, product in recent_rows
    ]

    return DashboardSummary(
        action_requests_total=action_requests_total,
        decisions_by_verdict=decisions_by_verdict,
        action_requests_by_status=ar_by_status,
        approvals_pending=approvals_pending,
        approvals_resolved=approvals_resolved,
        payments_by_status=payments_by_status,
        audit_events=audit_events,
        audit_chain_valid=chain.valid,
        recent_decisions=recent,
    )
