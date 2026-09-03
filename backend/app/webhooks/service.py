"""
Razorpay webhook processing.

Order of operations:

  1. verify HMAC-SHA256 over the *raw* bytes (never a reparsed body) with the
     webhook secret — invalid -> nothing is persisted, 400.
  2. parse the (now trusted) JSON.
  3. dedupe on `X-Razorpay-Event-Id` (fallback: sha256 of the raw body) via the
     unique `webhook_event.event_id` — a replay is a silent 200, no re-processing.
  4. match to a local PaymentAttempt (by payment-link id, or by decision id in
     the link notes), record the WebhookEvent, apply at most one status
     transition, append audit events.
  5. one commit. If anything raises, nothing persists and Razorpay will retry.

Unknown event types are recorded and acknowledged (200) — never a 500.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import append_audit_event, events
from app.core.enums import PaymentStatus
from app.razorpay.client import RazorpayClient
from app.razorpay.models import PaymentAttempt
from app.webhooks.models import WebhookEvent
from app.webhooks.schemas import WebhookAck

_NON_TERMINAL = (PaymentStatus.CREATED, PaymentStatus.PENDING)

# event type -> the status it drives the PaymentAttempt to (None = record only)
_EVENT_TO_STATUS: dict[str, PaymentStatus] = {
    "payment_link.paid": PaymentStatus.PAID,
    "payment_link.expired": PaymentStatus.EXPIRED,
    "payment_link.cancelled": PaymentStatus.FAILED,
    "payment.captured": PaymentStatus.PAID,
    "payment.failed": PaymentStatus.FAILED,
}


class WebhookSignatureInvalid(Exception):
    """HMAC did not verify. HTTP 400, nothing persisted."""


class WebhookMalformed(Exception):
    """Signature verified but the body is not JSON. HTTP 400, nothing persisted."""


def _extract_refs(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (razorpay_payment_link_id, decision_id) from a webhook payload."""
    body = payload.get("payload", {})
    link = body.get("payment_link", {}).get("entity", {})
    link_id = link.get("id")
    notes = link.get("notes") or body.get("payment", {}).get("entity", {}).get("notes") or {}
    decision_id = notes.get("decision_id") if isinstance(notes, dict) else None
    return link_id, decision_id


async def _match_attempt(
    session: AsyncSession, link_id: str | None, decision_id: str | None
) -> PaymentAttempt | None:
    if link_id:
        found = (
            await session.scalars(
                select(PaymentAttempt).where(
                    PaymentAttempt.razorpay_payment_link_id == link_id
                )
            )
        ).one_or_none()
        if found is not None:
            return found
    if decision_id:
        return (
            await session.scalars(
                select(PaymentAttempt).where(
                    PaymentAttempt.decision_id == decision_id
                )
            )
        ).one_or_none()
    return None


async def process_webhook(
    session: AsyncSession,
    client: RazorpayClient,
    *,
    raw_body: bytes,
    signature: str | None,
    event_id_header: str | None,
) -> WebhookAck:
    if not signature or not client.verify_webhook_signature(
        raw_body=raw_body, signature=signature
    ):
        raise WebhookSignatureInvalid()

    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError as exc:
        raise WebhookMalformed() from exc

    event_type = str(payload.get("event", ""))
    event_id = event_id_header or f"sha256:{hashlib.sha256(raw_body).hexdigest()}"

    session.add(
        WebhookEvent(
            event_id=event_id,
            event_type=event_type or "(none)",
            payload=payload,
            signature_valid=True,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return WebhookAck(status="duplicate_ignored", event_type=event_type)

    webhook_row = (
        await session.scalars(
            select(WebhookEvent).where(WebhookEvent.event_id == event_id)
        )
    ).one()

    link_id, decision_id = _extract_refs(payload)
    attempt = await _match_attempt(session, link_id, decision_id)
    now = _dt.datetime.now(_dt.timezone.utc)

    if attempt is None:
        webhook_row.processed_at = now
        await append_audit_event(
            session,
            ref_type="webhook_event",
            ref_id=webhook_row.id,
            event_type=events.WEBHOOK_RECEIVED,
            payload={
                "webhook_event_id": webhook_row.id,
                "razorpay_event_id": event_id,
                "event_type": event_type,
                "matched": False,
            },
        )
        await session.commit()
        return WebhookAck(status="received_unmatched", event_type=event_type)

    webhook_row.payment_attempt_id = attempt.id
    webhook_row.processed_at = now
    await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=await _action_request_id(session, attempt),
        event_type=events.WEBHOOK_RECEIVED,
        payload={
            "webhook_event_id": webhook_row.id,
            "razorpay_event_id": event_id,
            "event_type": event_type,
            "payment_attempt_id": attempt.id,
            "matched": True,
        },
    )

    new_status = _EVENT_TO_STATUS.get(event_type)
    if (
        new_status is not None
        and attempt.status in _NON_TERMINAL
        and attempt.status != new_status
    ):
        attempt.status = new_status
        ar_id = await _action_request_id(session, attempt)
        await append_audit_event(
            session,
            ref_type="action_request",
            ref_id=ar_id,
            event_type=events.PAYMENT_STATUS_UPDATED,
            payload={
                "payment_attempt_id": attempt.id,
                "decision_id": attempt.decision_id,
                "new_status": new_status.value,
                "source": "webhook",
                "razorpay_event_id": event_id,
            },
        )
        if new_status is PaymentStatus.PAID:
            await append_audit_event(
                session,
                ref_type="action_request",
                ref_id=ar_id,
                event_type=events.PAYMENT_EXECUTION_SUCCEEDED,
                payload={
                    "payment_attempt_id": attempt.id,
                    "decision_id": attempt.decision_id,
                    "razorpay_event_id": event_id,
                },
            )
        result_status = "processed"
    elif new_status is None:
        result_status = "received_unknown_event"
    else:
        result_status = "processed"  # matched, but no transition (terminal / same)

    await session.commit()
    return WebhookAck(
        status=result_status, event_type=event_type, payment_status=attempt.status
    )


async def _action_request_id(session: AsyncSession, attempt: PaymentAttempt):
    """The action_request behind a payment attempt, for audit ref_id."""
    from app.policy.models import Decision

    decision = await session.get(Decision, attempt.decision_id)
    assert decision is not None
    return decision.action_request_id
