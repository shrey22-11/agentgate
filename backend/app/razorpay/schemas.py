"""HTTP responses for the execution + reconciliation endpoints."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import PaymentStatus


class PaymentExecutionResponse(BaseModel):
    payment_attempt_id: uuid.UUID
    decision_id: uuid.UUID
    status: PaymentStatus
    amount: Decimal
    currency: str
    razorpay_payment_link_id: str | None
    short_url: str | None
    # true when this response is an existing attempt returned unchanged rather
    # than a newly created one (idempotent replay).
    already_existed: bool = False
