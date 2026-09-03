"""Tiny response for the webhook endpoint. Razorpay only cares about the 2xx."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.enums import PaymentStatus


class WebhookAck(BaseModel):
    status: str  # processed | duplicate_ignored | received_unmatched | received_unknown_event
    event_type: str | None = None
    payment_status: PaymentStatus | None = None
