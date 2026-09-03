"""
Orchestration for POST /actions. Deterministic — no LLM, no Razorpay.

    HTTP request (validated)
        -> load Agent + Product from the DB (authoritative)
        -> persist ActionRequest (status RECEIVED)
        -> audit ACTION_REQUEST_RECEIVED
        -> build PolicyInput (authoritative DB data + request data)
        -> evaluate()               [app.policy, unchanged]
        -> persist Decision, mark ActionRequest DECIDED
        -> audit POLICY_EVALUATED
        -> if verdict is NEEDS_APPROVAL: audit APPROVAL_REQUESTED
        -> commit once
        -> ActionDecisionResponse

A NEEDS_APPROVAL decision enters the human approval queue (Phase 7) simply by
existing with no `Approval` row yet; the APPROVAL_REQUESTED event here gives
that queue entry an auditable origin, written in the same transaction so it is
atomic with the decision it refers to.

The whole thing runs in the caller's single transaction (`get_db`). This
module performs exactly one `commit()`, as its final step; `append_audit_event`
never commits. Any exception before that commit leaves nothing persisted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.action_requests.schemas import (
    ActionDecisionResponse,
    ActionRequestCreate,
    CounterOfferOut,
)
from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.audit import append_audit_event, events
from app.catalog.models import Product
from app.core.enums import ActionRequestStatus, ActionType, Verdict
from app.policy import PolicyInput, effective_transaction_amount, evaluate
from app.policy.decision import PolicyDecision
from app.policy.models import Decision


class ResourceNotFound(Exception):
    """A client-supplied id does not resolve to a row. Maps to HTTP 404."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AiParseContext:
    """
    Supplied by the natural-language parser (Phase 9) so the ONE policy path
    (`evaluate_action`) can fill the LLM-path fields and emit ACTION_PARSED,
    without a parallel implementation of policy evaluation.
    """

    raw_input: str
    confidence: Decimal
    parsed_payload: dict
    audit_payload: dict


def _build_policy_input(
    agent: Agent, product: Product, body: ActionRequestCreate
) -> PolicyInput:
    """
    Authoritative agent/product fields from the DB + the client's request
    fields. The policy engine only ever sees this plain object, never an ORM
    instance — Phase 4's DB-independence is preserved.
    """
    return PolicyInput(
        agent_id=agent.id,
        agent_status=agent.status,
        agent_allowed_actions=tuple(ActionType(a) for a in agent.allowed_actions),
        agent_max_transaction_amount=agent.max_transaction_amount,
        action_type=body.action_type,
        requested_quantity=body.quantity,
        requested_discount_pct=body.requested_discount_pct,
        proposed_price=body.proposed_price,
        product_id=product.id,
        product_name=product.name,
        product_price=product.price,
        product_stock=product.stock,
        product_max_discount_pct=product.max_discount_pct,
        product_min_margin_price=product.min_margin_price,
        merchant_policy_version=product.merchant.policy_version,
    )


def _persist_decision(
    action_request_id: uuid.UUID,
    decision: PolicyDecision,
    executable_amount: Decimal | None,
) -> Decision:
    return Decision(
        action_request_id=action_request_id,
        verdict=decision.verdict,
        policy_rule_id=decision.rule_id,
        reason=decision.reason,
        counter_offer_price=decision.counter_offer_price,
        counter_offer_discount_pct=decision.counter_offer_discount_pct,
        # The trusted charge amount for an executable decision; NULL otherwise
        # so DENY / COUNTER_OFFER rows carry no spendable figure.
        executable_amount=(
            executable_amount
            if decision.verdict in (Verdict.ALLOW, Verdict.NEEDS_APPROVAL)
            else None
        ),
        policy_version=decision.policy_version,
    )


async def evaluate_action(
    session: AsyncSession,
    body: ActionRequestCreate,
    *,
    ai_context: AiParseContext | None = None,
) -> ActionDecisionResponse:
    agent = (
        await session.scalars(select(Agent).where(Agent.id == body.agent_id))
    ).one_or_none()
    if agent is None:
        raise ResourceNotFound("AGENT_NOT_FOUND", f"No agent with id {body.agent_id}")

    product = (
        await session.scalars(
            select(Product)
            .options(selectinload(Product.merchant))
            .where(Product.id == body.product_id)
        )
    ).one_or_none()
    if product is None:
        raise ResourceNotFound(
            "PRODUCT_NOT_FOUND", f"No product with id {body.product_id}"
        )

    # `source="http"` is the structured path; `ai_context` is set on the
    # natural-language path and carries the preserved raw input, the
    # deterministically-computed confidence, and the validated interpretation.
    default_parsed_payload = {
        "source": "http",
        "action_type": body.action_type.value,
        "quantity": body.quantity,
        "requested_discount_pct": _as_str(body.requested_discount_pct),
        "proposed_price": _as_str(body.proposed_price),
    }
    action_request = ActionRequest(
        agent_id=agent.id,
        product_id=product.id,
        action_type=body.action_type,
        requested_quantity=body.quantity,
        requested_discount_pct=body.requested_discount_pct,
        proposed_price=body.proposed_price,
        raw_input=ai_context.raw_input if ai_context else None,
        parsed_payload=ai_context.parsed_payload if ai_context else default_parsed_payload,
        confidence=ai_context.confidence if ai_context else None,
        status=ActionRequestStatus.RECEIVED,
    )
    session.add(action_request)
    await session.flush()  # assign action_request.id

    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request.id,
        event_type=events.ACTION_REQUEST_RECEIVED,
        payload={
            "action_request_id": action_request.id,
            "agent_id": agent.id,
            "product_id": product.id,
            "action_type": body.action_type.value,
            "quantity": body.quantity,
            "requested_discount_pct": body.requested_discount_pct,
            "proposed_price": body.proposed_price,
            "source": "ai" if ai_context else "http",
        },
    )

    if ai_context is not None:
        action_request.status = ActionRequestStatus.PARSED
        await append_audit_event(
            session,
            ref_type="action_request",
            ref_id=action_request.id,
            event_type=events.ACTION_PARSED,
            payload=ai_context.audit_payload,
        )
        # Structured output passed schema + business checks + catalogue
        # resolution before we got here.
        action_request.status = ActionRequestStatus.VALIDATED

    policy_input = _build_policy_input(agent, product, body)
    decision = evaluate(policy_input)

    decision_row = _persist_decision(
        action_request.id, decision, effective_transaction_amount(policy_input)
    )
    session.add(decision_row)
    action_request.status = ActionRequestStatus.DECIDED
    await session.flush()  # assign decision_row.id

    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request.id,
        event_type=events.POLICY_EVALUATED,
        payload={
            "decision_id": decision_row.id,
            "action_request_id": action_request.id,
            "verdict": decision.verdict.value,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
            "policy_version": decision.policy_version,
            "counter_offer_price": decision.counter_offer_price,
            "counter_offer_discount_pct": decision.counter_offer_discount_pct,
        },
    )

    if decision.verdict is Verdict.NEEDS_APPROVAL:
        await append_audit_event(
            session,
            ref_type="action_request",
            ref_id=action_request.id,
            event_type=events.APPROVAL_REQUESTED,
            payload={
                "decision_id": decision_row.id,
                "action_request_id": action_request.id,
                "agent_id": agent.id,
                "product_id": product.id,
                "original_rule_id": decision.rule_id,
                "original_reason": decision.reason,
                "policy_version": decision.policy_version,
            },
        )

    await session.commit()  # the one and only transaction boundary

    counter_offer = None
    if decision.verdict is Verdict.COUNTER_OFFER:
        counter_offer = CounterOfferOut(
            price=decision.counter_offer_price,
            discount_pct=decision.counter_offer_discount_pct,
        )
    return ActionDecisionResponse(
        action_request_id=action_request.id,
        decision_id=decision_row.id,
        verdict=decision.verdict,
        rule_id=decision.rule_id,
        reason=decision.reason,
        policy_version=decision.policy_version,
        counter_offer=counter_offer,
    )


def _as_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
