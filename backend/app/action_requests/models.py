"""
ActionRequest — the structured, validated commercial request that enters the
policy engine.

Flow position (see architecture-freeze Section 12):

    natural language -> AI parse -> structured output -> Pydantic validation
    -> ActionRequest (this row) -> policy engine -> Decision

`raw_input` is kept verbatim for the audit trail. It is NEVER read by the policy
engine — only the typed columns and `parsed_payload` are. If parsing or
validation fails, a row is still written with status INVALID and low/zero
`confidence`, and the policy engine treats that as DENY (fail closed).
"""
from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin, str_enum, uuid_pk
from app.core.enums import ActionRequestStatus, ActionType

Money = Numeric(12, 2)
Percent = Numeric(5, 2)


class ActionRequest(TimestampMixin, Base):
    __tablename__ = "action_request"
    __table_args__ = (
        CheckConstraint(
            "requested_discount_pct IS NULL OR "
            "(requested_discount_pct >= 0 AND requested_discount_pct <= 100)",
            name="ck_action_request_discount_range",
        ),
        CheckConstraint(
            "requested_quantity IS NULL OR requested_quantity > 0",
            name="ck_action_request_qty_positive",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_action_request_confidence_range",
        ),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()

    agent_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.id"), nullable=False, index=True
    )
    # Nullable on purpose: an adversarial or malformed request may never resolve
    # to a real product, and we still persist the request + its DENY decision.
    product_id: Mapped[_uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("product.id"), nullable=True, index=True
    )

    action_type: Mapped[ActionType] = str_enum(ActionType, nullable=False)

    # What the agent asked for. All optional — a plain PURCHASE at list price
    # sets none of them.
    requested_discount_pct: Mapped[Decimal | None] = mapped_column(Percent)
    requested_quantity: Mapped[int | None] = mapped_column()
    proposed_price: Mapped[Decimal | None] = mapped_column(Money)

    # Audit-only. Never an input to any decision.
    raw_input: Mapped[str | None] = mapped_column(Text)
    # The LLM's structured output after Pydantic validation (or the raw dict on
    # a validation failure, for debugging). JSONB so it is queryable.
    parsed_payload: Mapped[dict | None] = mapped_column(JSONB)
    # Parser self-reported confidence, 0..1. Below the module threshold this is
    # forced to an INVALID status regardless of the rest of the payload.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))

    status: Mapped[ActionRequestStatus] = str_enum(
        ActionRequestStatus, nullable=False, default=ActionRequestStatus.RECEIVED
    )

    decision: Mapped["Decision | None"] = relationship(  # noqa: F821
        back_populates="action_request", uselist=False
    )
