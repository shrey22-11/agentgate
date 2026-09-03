"""
PaymentAttempt — one row per REAL RAZORPAY test-mode payment object created for
an executable decision.

Executable means, and the database enforces:

  * `decision_verdict = 'ALLOW'` with no approval, OR
  * `decision_verdict = 'NEEDS_APPROVAL'` AND `approval_outcome = 'APPROVED'`
    where `(approval_id, approval_outcome)` actually resolves to an `approval`
    row via a composite foreign key.

So a DENY / COUNTER_OFFER decision, or a NEEDS_APPROVAL decision with no
approval or a REJECTED approval, cannot have a PaymentAttempt — there is no
application path and no raw-SQL path to create one.

  * `fk_payment_attempt_allow_decision` — `(decision_id, decision_verdict)`
    resolves to `decision(id, verdict)`: the decision exists with that verdict.
  * `ck_payment_attempt_executable` — the two-branch rule above.
  * `fk_payment_attempt_approved_approval` — `(approval_id, approval_outcome)`
    resolves to `approval(id, outcome)`: the approval exists and is APPROVED.
  * `uq_payment_attempt_decision` / `uq_payment_attempt_idempotency_key` — at
    most one Razorpay object per decision; a replayed execute or a duplicated
    webhook cannot create a second one.
"""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, str_enum, uuid_pk
from app.core.enums import ApprovalOutcome, PaymentStatus, Verdict

_EXECUTABLE = (
    "(decision_verdict = 'ALLOW' "
    "AND approval_id IS NULL AND approval_outcome IS NULL) "
    "OR (decision_verdict = 'NEEDS_APPROVAL' "
    "AND approval_id IS NOT NULL AND approval_outcome = 'APPROVED')"
)


class PaymentAttempt(TimestampMixin, Base):
    __tablename__ = "payment_attempt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["decision_id", "decision_verdict"],
            ["decision.id", "decision.verdict"],
            name="fk_payment_attempt_allow_decision",
        ),
        ForeignKeyConstraint(
            ["approval_id", "approval_outcome"],
            ["approval.id", "approval.outcome"],
            name="fk_payment_attempt_approved_approval",
        ),
        CheckConstraint(_EXECUTABLE, name="ck_payment_attempt_executable"),
        UniqueConstraint("decision_id", name="uq_payment_attempt_decision"),
        UniqueConstraint(
            "idempotency_key", name="uq_payment_attempt_idempotency_key"
        ),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()

    # No single-column FKs here — the composite ForeignKeyConstraints own the
    # references. Both are indexed for lookups.
    decision_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    decision_verdict: Mapped[Verdict] = str_enum(Verdict, nullable=False)

    # Set only when executing an approved NEEDS_APPROVAL decision.
    approval_id: Mapped[_uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    approval_outcome: Mapped[ApprovalOutcome | None] = str_enum(
        ApprovalOutcome, nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)

    # Payment Links are the Phase 8 primitive; order id is kept for a possible
    # future Orders path. Both nullable until the object is actually created.
    razorpay_payment_link_id: Mapped[str | None] = mapped_column(String(64))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64))
    razorpay_short_url: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[PaymentStatus] = str_enum(
        PaymentStatus, nullable=False, default=PaymentStatus.CREATED
    )
