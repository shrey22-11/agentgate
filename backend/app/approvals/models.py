"""
Approval — a human decision on a NEEDS_APPROVAL verdict.

An approval can only move a request forward; it can never widen the original
policy constraints. Approving a NEEDS_APPROVAL decision authorises *that*
transaction as already evaluated — it does not, for example, let a discount
exceed `max_discount_pct`. That invariant is enforced in `app.approvals`
service code, not here.
"""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, str_enum, uuid_pk
from app.core.enums import ApprovalOutcome


class Approval(TimestampMixin, Base):
    __tablename__ = "approval"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_approval_decision"),
        # Target for payment_attempt's composite FK: lets a payment row prove,
        # at the DB level, that it references an APPROVED approval (mirrors the
        # decision(id, verdict) trick).
        UniqueConstraint("id", "outcome", name="uq_approval_id_outcome"),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()
    decision_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("decision.id"),
        nullable=False,
        index=True,
    )

    approver: Mapped[str] = mapped_column(String(120), nullable=False)
    outcome: Mapped[ApprovalOutcome] = str_enum(ApprovalOutcome, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
