"""
Phase 3 — the schema constraints that do real work in the demo.

The architecture-freeze doc calls these out specifically: a payment attempt can
only exist against an ALLOW decision; idempotency keys are unique; the margin
floor can't be seeded above list price. These are enforced in the database, not
just in application code, so they hold even if a future bug tries to bypass the
service layer.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.catalog.models import Merchant, Product
from app.core.enums import (
    ActionRequestStatus,
    ActionType,
    AgentStatus,
    AgentType,
    PaymentStatus,
    Verdict,
)
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt


async def _chain(session, *, verdict: Verdict) -> Decision:
    """merchant -> product -> agent -> action_request -> decision(<verdict>)."""
    merchant = Merchant(name="Constraint Co. (SIMULATED)")
    session.add(merchant)
    await session.flush()

    product = Product(
        merchant_id=merchant.id,
        name="Constraint Item (SIMULATED)",
        category="misc",
        price=Decimal("1000.00"),
        stock=10,
        max_discount_pct=Decimal("10.00"),
        min_margin_price=Decimal("900.00"),
    )
    agent = Agent(
        name="Constraint Agent (SIMULATED)",
        type=AgentType.AI_BUYER,
        status=AgentStatus.ACTIVE,
        max_transaction_amount=Decimal("50000.00"),
        allowed_actions=[ActionType.PURCHASE.value],
    )
    session.add_all([product, agent])
    await session.flush()

    ar = ActionRequest(
        agent_id=agent.id,
        product_id=product.id,
        action_type=ActionType.PURCHASE,
        status=ActionRequestStatus.DECIDED,
    )
    session.add(ar)
    await session.flush()

    decision = Decision(
        action_request_id=ar.id,
        verdict=verdict,
        policy_rule_id="RULE_TEST",
        reason="fixture",
        policy_version="v1",
    )
    session.add(decision)
    await session.flush()
    return decision


async def test_payment_attempt_allowed_against_allow_decision(db_session) -> None:
    decision = await _chain(db_session, verdict=Verdict.ALLOW)
    db_session.add(
        PaymentAttempt(
            decision_id=decision.id,
            decision_verdict=Verdict.ALLOW,
            idempotency_key=f"idem-{uuid.uuid4()}",
            status=PaymentStatus.CREATED,
        )
    )
    await db_session.flush()  # must not raise


async def test_payment_attempt_check_blocks_non_allow_verdict(db_session) -> None:
    """The CHECK: decision_verdict must literally be 'ALLOW'."""
    decision = await _chain(db_session, verdict=Verdict.DENY)
    db_session.add(
        PaymentAttempt(
            decision_id=decision.id,
            decision_verdict=Verdict.DENY,
            idempotency_key=f"idem-{uuid.uuid4()}",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_payment_attempt_fk_blocks_verdict_spoofing(db_session) -> None:
    """
    Claiming decision_verdict='ALLOW' while the referenced decision is actually
    DENY must fail the composite (decision_id, decision_verdict) foreign key.
    """
    decision = await _chain(db_session, verdict=Verdict.DENY)
    db_session.add(
        PaymentAttempt(
            decision_id=decision.id,
            decision_verdict=Verdict.ALLOW,  # lie
            idempotency_key=f"idem-{uuid.uuid4()}",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_idempotency_key_is_unique(db_session) -> None:
    d1 = await _chain(db_session, verdict=Verdict.ALLOW)
    d2 = await _chain(db_session, verdict=Verdict.ALLOW)
    key = f"idem-{uuid.uuid4()}"
    db_session.add(
        PaymentAttempt(decision_id=d1.id, decision_verdict=Verdict.ALLOW, idempotency_key=key)
    )
    await db_session.flush()
    db_session.add(
        PaymentAttempt(decision_id=d2.id, decision_verdict=Verdict.ALLOW, idempotency_key=key)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_payment_attempt_per_decision(db_session) -> None:
    decision = await _chain(db_session, verdict=Verdict.ALLOW)
    db_session.add(
        PaymentAttempt(
            decision_id=decision.id,
            decision_verdict=Verdict.ALLOW,
            idempotency_key=f"idem-{uuid.uuid4()}",
        )
    )
    await db_session.flush()
    db_session.add(
        PaymentAttempt(
            decision_id=decision.id,
            decision_verdict=Verdict.ALLOW,
            idempotency_key=f"idem-{uuid.uuid4()}",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_margin_floor_cannot_exceed_price(db_session) -> None:
    merchant = Merchant(name="Bad Margin Co. (SIMULATED)")
    db_session.add(merchant)
    await db_session.flush()
    db_session.add(
        Product(
            merchant_id=merchant.id,
            name="Upside Down (SIMULATED)",
            category="misc",
            price=Decimal("100.00"),
            stock=1,
            max_discount_pct=Decimal("10.00"),
            min_margin_price=Decimal("150.00"),  # > price
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
