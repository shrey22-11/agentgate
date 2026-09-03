"""
Payment execution and reconciliation. The only component that drives a Razorpay
payment object into existence.

Transaction shape (deliberately more than one commit — see docs/payment-execution.md):

    Txn 1  (short, holds a row lock on the decision)
      load decision FOR UPDATE, check eligibility, check no prior attempt,
      INSERT payment_attempt(status=CREATED), audit PAYMENT_EXECUTION_STARTED,
      COMMIT                                   <- lock released here

    (no DB transaction held during the Razorpay HTTP call)

    Txn 2  (short)
      on success: store ids, status=PENDING, audit PAYMENT_EXECUTION_CREATED
      on failure: status=FAILED,             audit PAYMENT_EXECUTION_FAILED
      COMMIT

A crash between Txn 1 and Txn 2 leaves a `CREATED` row with no Razorpay id;
`reconcile_payment_attempt` recovers it (adopt the object created with our
`reference_id`, or mark it FAILED). The amount charged always comes from
`decision.executable_amount`, never from a caller.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import append_audit_event, events
from app.core.enums import PaymentStatus, Verdict
from app.policy.models import Decision
from app.razorpay.client import (
    DisabledRazorpayClient,
    PaymentLinkResult,
    RazorpayClient,
    RazorpayDisabledError,
    RazorpayError,
)
from app.razorpay.eligibility import can_execute
from app.razorpay.models import PaymentAttempt
from app.razorpay.schemas import PaymentExecutionResponse

_CURRENCY = "INR"
_NON_TERMINAL = (PaymentStatus.CREATED, PaymentStatus.PENDING)


class ExecutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DecisionNotFound(ExecutionError):
    """HTTP 404."""


class ExecutionNotAllowed(ExecutionError):
    """Decision is not executable (verdict / approval state). HTTP 409."""


class ExecutionConflict(ExecutionError):
    """A prior attempt blocks a fresh one. HTTP 409."""


class ExecutionFailed(ExecutionError):
    """Razorpay object creation failed. HTTP 502."""


def _amount_paise(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _map_payment_link_status(raw: str) -> PaymentStatus:
    return {
        "paid": PaymentStatus.PAID,
        "expired": PaymentStatus.EXPIRED,
        "cancelled": PaymentStatus.FAILED,
    }.get(raw.lower(), PaymentStatus.PENDING)


def _response(pa: PaymentAttempt, amount: Decimal, *, already_existed: bool) -> PaymentExecutionResponse:
    return PaymentExecutionResponse(
        payment_attempt_id=pa.id,
        decision_id=pa.decision_id,
        status=pa.status,
        amount=amount,
        currency=_CURRENCY,
        razorpay_payment_link_id=pa.razorpay_payment_link_id,
        short_url=pa.razorpay_short_url,
        already_existed=already_existed,
    )


def _require_enabled(client: RazorpayClient) -> None:
    """Fail before any DB write if Razorpay is disabled — no half-built attempt."""
    if isinstance(client, DisabledRazorpayClient):
        raise RazorpayDisabledError("RAZORPAY_ENABLED is false")


async def _load_decision_for_update(session: AsyncSession, decision_id) -> Decision:
    decision = (
        await session.scalars(
            select(Decision).where(Decision.id == decision_id).with_for_update()
        )
    ).one_or_none()
    if decision is None:
        raise DecisionNotFound("DECISION_NOT_FOUND", f"No decision with id {decision_id}")
    return decision


async def _existing_attempt(session: AsyncSession, decision_id) -> PaymentAttempt | None:
    return (
        await session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.decision_id == decision_id)
        )
    ).one_or_none()


async def execute_payment(
    session: AsyncSession, client: RazorpayClient, decision_id
) -> PaymentExecutionResponse:
    _require_enabled(client)
    # --- Txn 1 -----------------------------------------------------------
    decision = await _load_decision_for_update(session, decision_id)
    eligibility = await can_execute(session, decision)
    if not eligibility.eligible:
        raise ExecutionNotAllowed(
            eligibility.reason_code,
            f"Decision {decision_id} is not executable ({eligibility.reason_code}).",
        )
    if decision.executable_amount is None:
        # ALLOW / approved NEEDS_APPROVAL always have this; None means bad data.
        raise ExecutionError(
            "NO_EXECUTABLE_AMOUNT",
            f"Decision {decision_id} has no executable_amount recorded.",
        )
    amount = decision.executable_amount

    existing = await _existing_attempt(session, decision_id)
    if existing is not None:
        if existing.status in (PaymentStatus.PENDING, PaymentStatus.PAID):
            await session.commit()
            return _response(existing, amount, already_existed=True)
        if existing.status is PaymentStatus.CREATED:
            raise ExecutionConflict(
                "EXECUTION_IN_PROGRESS",
                "A prior execution did not finish creating a Razorpay object; "
                "reconcile it before retrying.",
            )
        raise ExecutionConflict(
            "EXECUTION_TERMINAL_FAILED",
            f"This decision's payment attempt is terminal ({existing.status.value}).",
        )

    attempt = PaymentAttempt(
        decision_id=decision.id,
        decision_verdict=decision.verdict,
        idempotency_key=f"decision:{decision_id}",
        status=PaymentStatus.CREATED,
    )
    if decision.verdict is Verdict.NEEDS_APPROVAL:
        assert eligibility.approval is not None
        attempt.approval_id = eligibility.approval.id
        attempt.approval_outcome = eligibility.approval.outcome

    session.add(attempt)
    await session.flush()
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=decision.action_request_id,
        event_type=events.PAYMENT_EXECUTION_STARTED,
        payload={
            "payment_attempt_id": attempt.id,
            "decision_id": decision.id,
            "action_request_id": decision.action_request_id,
            "verdict": decision.verdict.value,
            "authorised_via": eligibility.reason_code,
            "amount": amount,
            "currency": _CURRENCY,
        },
    )
    await session.commit()  # Txn 1 complete; decision row lock released

    # --- Razorpay call (no DB transaction held) ------------------------
    try:
        result: PaymentLinkResult = await client.create_payment_link(
            amount_paise=_amount_paise(amount),
            currency=_CURRENCY,
            reference_id=str(attempt.id),
            description=f"AgentGate decision {decision_id}",
            notes={
                "decision_id": str(decision.id),
                "action_request_id": str(decision.action_request_id),
            },
        )
    except RazorpayError as exc:
        # --- Txn 2b (failure) -----------------------------------------
        await _mark_failed(session, attempt.id, decision.action_request_id, str(exc))
        raise ExecutionFailed("RAZORPAY_CREATE_FAILED", str(exc)) from exc

    # --- Txn 2 (success) --------------------------------------------
    fresh = await session.get(PaymentAttempt, attempt.id, with_for_update=True)
    assert fresh is not None
    fresh.razorpay_payment_link_id = result.id
    fresh.razorpay_short_url = result.short_url
    fresh.status = PaymentStatus.PENDING
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=decision.action_request_id,
        event_type=events.PAYMENT_EXECUTION_CREATED,
        payload={
            "payment_attempt_id": fresh.id,
            "decision_id": decision.id,
            "razorpay_payment_link_id": result.id,
            "short_url": result.short_url,
            "razorpay_status": result.status,
            "amount": amount,
            "currency": _CURRENCY,
        },
    )
    await session.commit()
    return _response(fresh, amount, already_existed=False)


async def _mark_failed(
    session: AsyncSession, attempt_id, action_request_id, error_message: str
) -> None:
    fresh = await session.get(PaymentAttempt, attempt_id, with_for_update=True)
    assert fresh is not None
    fresh.status = PaymentStatus.FAILED
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=action_request_id,
        event_type=events.PAYMENT_EXECUTION_FAILED,
        payload={
            "payment_attempt_id": fresh.id,
            "decision_id": fresh.decision_id,
            # error_message is our own RazorpayError text — no secrets, no body.
            "error": error_message[:200],
        },
    )
    await session.commit()


async def reconcile_payment_attempt(
    session: AsyncSession, client: RazorpayClient, decision_id
) -> PaymentExecutionResponse:
    _require_enabled(client)
    decision = await _load_decision_for_update(session, decision_id)
    if decision.executable_amount is None:
        raise ExecutionError("NO_EXECUTABLE_AMOUNT", "Decision has no executable_amount.")
    amount = decision.executable_amount

    attempt = await _existing_attempt(session, decision_id)
    if attempt is None:
        raise ExecutionConflict(
            "NO_PAYMENT_ATTEMPT", f"No payment attempt for decision {decision_id}."
        )
    if attempt.status not in _NON_TERMINAL:
        await session.commit()
        return _response(attempt, amount, already_existed=True)

    links = await client.fetch_payment_links_by_reference(str(attempt.id))
    match = links[0] if links else None

    if match is None:
        if attempt.status is PaymentStatus.CREATED:
            await _mark_failed(
                session, attempt.id, decision.action_request_id,
                "no Razorpay payment link found for this attempt on reconcile",
            )
            refreshed = await session.get(PaymentAttempt, attempt.id)
            return _response(refreshed, amount, already_existed=True)
        await session.commit()
        return _response(attempt, amount, already_existed=True)

    new_status = _map_payment_link_status(match.status)
    changed = (
        attempt.status != new_status
        or attempt.razorpay_payment_link_id != match.id
    )
    if not changed:
        await session.commit()
        return _response(attempt, amount, already_existed=True)

    was_orphan = attempt.razorpay_payment_link_id is None
    attempt.razorpay_payment_link_id = match.id
    attempt.razorpay_short_url = match.short_url
    attempt.status = new_status
    if was_orphan:
        await append_audit_event(
            session,
            ref_type="action_request",
            ref_id=decision.action_request_id,
            event_type=events.PAYMENT_EXECUTION_CREATED,
            payload={
                "payment_attempt_id": attempt.id,
                "decision_id": decision.id,
                "razorpay_payment_link_id": match.id,
                "short_url": match.short_url,
                "recovered": True,
            },
        )
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=decision.action_request_id,
        event_type=events.PAYMENT_STATUS_UPDATED,
        payload={
            "payment_attempt_id": attempt.id,
            "decision_id": decision.id,
            "new_status": new_status.value,
            "source": "reconcile",
            "razorpay_status": match.status,
        },
    )
    await session.commit()
    return _response(attempt, amount, already_existed=True)
