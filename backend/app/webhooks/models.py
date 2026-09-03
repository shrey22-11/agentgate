"""
WebhookEvent — one row per Razorpay webhook delivery we accept.

Deduplication is the unique constraint on `event_id` (Razorpay's
`x-razorpay-event-id`). A duplicate delivery hits that constraint and is
acknowledged without re-processing, so a re-sent `payment.captured` cannot
transition a PaymentAttempt twice.

`signature_valid` records the result of HMAC-SHA256 verification over the raw
request body (Phase 8). A row with `signature_valid = false` is stored for the
audit trail but never acted on.
"""
from __future__ import annotations

import datetime as _dt
import uuid as _uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, uuid_pk


class WebhookEvent(TimestampMixin, Base):
    __tablename__ = "webhook_event"

    id: Mapped[_uuid.UUID] = uuid_pk()

    # Razorpay's per-event id. Unique -> replays are no-ops.
    event_id: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)

    payment_attempt_id: Mapped[_uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payment_attempt.id"),
        nullable=True,
        index=True,
    )
    # Null until processing completes; set once state changes have been applied.
    processed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
