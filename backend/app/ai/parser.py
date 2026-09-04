"""
The defensive natural-language parsing pipeline.

    raw text (untrusted)
      -> load Agent from DB (404 if unknown)
      -> client.parse_intent()  [Gemini, JSON-schema-constrained structured output]
      -> ParsedIntent (already Pydantic-validated by the SDK)
      -> deterministic field coercion + re-validation
      -> deterministic catalogue resolution (name -> product, never an id)
      -> confidence gate
      -> ActionRequestCreate (the EXISTING schema, re-validated)
      -> evaluate_action(..., ai_context=...)   [the ONE policy path]
      -> NLActionResponse

Any failure at any stage — provider down, invalid output, unknown/ambiguous
product, bad numbers, low confidence — fails closed: a persisted ActionRequest
with status INVALID and a persisted DENY / RULE_INPUT_INVALID decision, fully
audited. The LLM never produces a verdict, a price floor, or a payment.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError

from app.action_requests.schemas import ActionDecisionResponse, ActionRequestCreate
from app.action_requests.service import (
    AiParseContext,
    ResourceNotFound,
    evaluate_action,
)
from app.action_requests.models import ActionRequest
from app.ai.client import AIParserClient, AIUnavailableError
from app.ai.schemas import NLActionResponse, ParsedIntent
from app.agents.models import Agent
from app.audit import append_audit_event, events
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.enums import ActionRequestStatus, ActionType, Verdict
from app.policy import rules

_FALLBACK_POLICY_VERSION = "v1"


class _ParseFailure(Exception):
    """Internal: a stage failed closed. Carries the audit stage + detail."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


# --- deterministic helpers ------------------------------------------------
def _to_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise _ParseFailure("field_coercion", f"{field} {raw!r} is not a number") from exc
    if not value.is_finite():
        raise _ParseFailure("field_coercion", f"{field} {raw!r} is not finite")
    return value


async def _resolve_product(
    session: AsyncSession, reference: str
) -> tuple[Product, Decimal, str]:
    """
    Resolve a free-text product name against the catalogue. Returns
    (product, confidence, method). Fails closed on no match or ambiguity — the
    model never supplies an id.
    """
    ref = reference.strip()
    if not ref:
        raise _ParseFailure("product_resolution", "empty product reference")

    exact = (
        await session.scalars(
            select(Product).where(func.lower(Product.name) == ref.lower())
        )
    ).all()
    if len(exact) == 1:
        return exact[0], Decimal("1.0"), "exact"
    if len(exact) > 1:  # unlikely (names aren't unique-constrained) but be safe
        raise _ParseFailure(
            "product_resolution", f"{len(exact)} products share the name {ref!r}"
        )

    partial = (
        await session.scalars(
            select(Product).where(
                func.lower(Product.name).contains(ref.lower(), autoescape=True)
            )
        )
    ).all()
    if len(partial) == 1:
        return partial[0], Decimal("0.7"), "substring"
    if len(partial) == 0:
        raise _ParseFailure(
            "product_resolution", f"no catalogue product matches {ref!r}"
        )
    names = ", ".join(sorted(p.name for p in partial))
    raise _ParseFailure(
        "product_resolution",
        f"{ref!r} is ambiguous — {len(partial)} candidates: {names}",
    )


async def _merchant_policy_version(session: AsyncSession) -> str:
    merchant = (await session.scalars(select(Merchant).limit(1))).one_or_none()
    return merchant.policy_version if merchant else _FALLBACK_POLICY_VERSION


# --- fail-closed persistence -------------------------------------------
async def _persist_failed_parse(
    session: AsyncSession,
    agent: Agent,
    *,
    raw_input: str,
    stage: str,
    detail: str,
    override_detected: bool = False,
    parsed_extra: dict | None = None,
) -> NLActionResponse:
    from app.policy.models import Decision  # local import: avoid cycle at module load

    action_request = ActionRequest(
        agent_id=agent.id,
        product_id=None,
        action_type=ActionType.PURCHASE,
        raw_input=raw_input,
        parsed_payload={
            "source": "ai",
            "success": False,
            "stage": stage,
            "detail": detail,
            "override_instructions_detected": override_detected,
            **(parsed_extra or {}),
        },
        confidence=Decimal("0"),
        status=ActionRequestStatus.INVALID,
    )
    session.add(action_request)
    await session.flush()

    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request.id,
        event_type=events.ACTION_REQUEST_RECEIVED,
        payload={
            "action_request_id": action_request.id,
            "agent_id": agent.id,
            "product_id": None,
            "source": "ai",
        },
    )
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request.id,
        event_type=events.ACTION_PARSED,
        payload={
            "action_request_id": action_request.id,
            "success": False,
            "stage": stage,
            "detail": detail,
            "confidence": Decimal("0"),
            "override_instructions_detected": override_detected,
        },
    )

    policy_version = await _merchant_policy_version(session)
    reason = (
        "Natural-language request could not be turned into a valid action "
        f"({stage}): {detail}"
    )
    decision = Decision(
        action_request_id=action_request.id,
        verdict=Verdict.DENY,
        policy_rule_id=rules.RULE_INPUT_INVALID,
        reason=reason,
        counter_offer_price=None,
        counter_offer_discount_pct=None,
        executable_amount=None,
        policy_version=policy_version,
    )
    session.add(decision)
    await session.flush()

    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request.id,
        event_type=events.POLICY_EVALUATED,
        payload={
            "decision_id": decision.id,
            "action_request_id": action_request.id,
            "verdict": Verdict.DENY.value,
            "rule_id": rules.RULE_INPUT_INVALID,
            "reason": reason,
            "policy_version": policy_version,
            "source": "ai_parse_failed",
        },
    )
    await session.commit()

    return NLActionResponse(
        decision=ActionDecisionResponse(
            action_request_id=action_request.id,
            decision_id=decision.id,
            verdict=Verdict.DENY,
            rule_id=rules.RULE_INPUT_INVALID,
            reason=reason,
            policy_version=policy_version,
            counter_offer=None,
        ),
        confidence=Decimal("0"),
        resolved_product=None,
        parse_notes=None,
        override_instructions_detected=override_detected,
    )


# --- entry point ------------------------------------------------------
async def parse_natural_language_action(
    session: AsyncSession,
    client: AIParserClient,
    *,
    agent_id: uuid.UUID,
    raw_input: str,
) -> NLActionResponse:
    settings = get_settings()

    agent = (
        await session.scalars(select(Agent).where(Agent.id == agent_id))
    ).one_or_none()
    if agent is None:
        # Same as POST /actions: 404, nothing persisted (agent_id is a NOT NULL FK).
        raise ResourceNotFound("AGENT_NOT_FOUND", f"No agent with id {agent_id}")

    # --- AI call (AIDisabledError propagates to the router as 503) ---------
    try:
        intent: ParsedIntent = await client.parse_intent(raw_input=raw_input)
    except AIUnavailableError as exc:
        return await _persist_failed_parse(
            session, agent, raw_input=raw_input, stage="ai_call", detail=str(exc)
        )

    override = bool(intent.contains_override_instructions)

    try:
        product, confidence, method, action_create = await _interpret(
            session, agent_id, intent
        )
    except _ParseFailure as failure:
        return await _persist_failed_parse(
            session,
            agent,
            raw_input=raw_input,
            stage=failure.stage,
            detail=failure.detail,
            override_detected=override,
            parsed_extra={"product_reference": intent.product_reference},
        )

    if confidence < Decimal(str(settings.ai_parse_confidence_threshold)):
        return await _persist_failed_parse(
            session,
            agent,
            raw_input=raw_input,
            stage="confidence",
            detail=f"resolution confidence {confidence} below threshold "
            f"{settings.ai_parse_confidence_threshold}",
            override_detected=override,
            parsed_extra={"product_reference": intent.product_reference},
        )

    parsed_payload = {
        "source": "ai",
        "success": True,
        "model": settings.ai_model,
        "product_reference": intent.product_reference,
        "resolved_product_id": str(product.id),
        "resolved_product_name": product.name,
        "resolution_method": method,
        "action_type": action_create.action_type.value,
        "quantity": action_create.quantity,
        "requested_discount_pct": _as_str(action_create.requested_discount_pct),
        "proposed_price": _as_str(action_create.proposed_price),
        "contains_override_instructions": override,
        "notes": intent.notes,
    }
    ai_context = AiParseContext(
        raw_input=raw_input,
        confidence=confidence,
        parsed_payload=parsed_payload,
        audit_payload={
            "success": True,
            "confidence": confidence,
            "model": settings.ai_model,
            "resolved_product_id": str(product.id),
            "resolved_product_name": product.name,
            "resolution_method": method,
            "requested_discount_pct": _as_str(action_create.requested_discount_pct),
            "override_instructions_detected": override,
        },
    )

    decision_response = await evaluate_action(session, action_create, ai_context=ai_context)
    return NLActionResponse(
        decision=decision_response,
        confidence=confidence,
        resolved_product=product.name,
        parse_notes=intent.notes,
        override_instructions_detected=override,
    )


async def _interpret(
    session: AsyncSession, agent_id: uuid.UUID, intent: ParsedIntent
) -> tuple[Product, Decimal, str, ActionRequestCreate]:
    if not intent.is_purchase_request:
        raise _ParseFailure("intent", "the message is not a request to buy a product")
    if not (intent.product_reference and intent.product_reference.strip()):
        raise _ParseFailure("intent", "no product was named")

    discount = (
        _to_decimal(intent.requested_discount_pct, "requested_discount_pct")
        if intent.requested_discount_pct is not None
        else None
    )
    if discount is not None and not (Decimal(0) <= discount <= Decimal(100)):
        raise _ParseFailure("field_coercion", f"discount {discount} outside 0..100")

    price = (
        _to_decimal(intent.proposed_price, "proposed_price")
        if intent.proposed_price is not None
        else None
    )
    if price is not None and price < 0:
        raise _ParseFailure("field_coercion", "proposed_price is negative")

    quantity = intent.quantity
    if quantity is not None and quantity < 1:
        raise _ParseFailure("field_coercion", f"quantity {quantity} is below 1")

    product, confidence, method = await _resolve_product(
        session, intent.product_reference
    )

    try:
        action_create = ActionRequestCreate(
            agent_id=agent_id,
            product_id=product.id,
            action_type=ActionType(intent.action_type),
            quantity=quantity,
            requested_discount_pct=discount,
            proposed_price=price,
        )
    except ValidationError as exc:
        raise _ParseFailure("revalidation", f"structured request rejected: {exc}") from exc

    return product, confidence, method, action_create


def _as_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
