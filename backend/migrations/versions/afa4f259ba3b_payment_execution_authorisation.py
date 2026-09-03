"""payment execution authorisation

Revision ID: afa4f259ba3b
Revises: 01c4917ca111
Create Date: 2026-09-03 12:40:00.000000

Makes an approved NEEDS_APPROVAL decision executable while keeping every
unauthorised path impossible at the database level.

Before: `payment_attempt` had `CHECK (decision_verdict = 'ALLOW')` — an approved
NEEDS_APPROVAL decision could never get a payment row.

After:
  * `approval` gains `UNIQUE (id, outcome)` so it can be a composite-FK target.
  * `payment_attempt` gains nullable `approval_id`, `approval_outcome`,
    `razorpay_short_url`.
  * the ALLOW-only CHECK is replaced by `ck_payment_attempt_executable`:
      (verdict='ALLOW'  AND approval_id IS NULL AND approval_outcome IS NULL)
      OR
      (verdict='NEEDS_APPROVAL' AND approval_id IS NOT NULL AND approval_outcome='APPROVED')
  * `fk_payment_attempt_approved_approval` forces `(approval_id, approval_outcome)`
    to resolve to a real `approval(id, outcome)` row — so 'APPROVED' cannot be
    faked.
  * `decision` gains nullable `executable_amount` — the trusted charge amount,
    pinned at decision time.

Still impossible: DENY / COUNTER_OFFER (CHECK), NEEDS_APPROVAL with no approval
or a REJECTED approval (composite FK cannot resolve).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "afa4f259ba3b"
down_revision: Union[str, None] = "01c4917ca111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXECUTABLE = (
    "(decision_verdict = 'ALLOW' "
    "AND approval_id IS NULL AND approval_outcome IS NULL) "
    "OR (decision_verdict = 'NEEDS_APPROVAL' "
    "AND approval_id IS NOT NULL AND approval_outcome = 'APPROVED')"
)


def upgrade() -> None:
    op.add_column(
        "decision",
        sa.Column("executable_amount", sa.Numeric(precision=12, scale=2), nullable=True),
    )

    op.create_unique_constraint(
        "uq_approval_id_outcome", "approval", ["id", "outcome"]
    )

    op.add_column(
        "payment_attempt",
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "payment_attempt",
        sa.Column("approval_outcome", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "payment_attempt",
        sa.Column("razorpay_short_url", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_payment_attempt_approval_id", "payment_attempt", ["approval_id"]
    )

    op.drop_constraint(
        "ck_payment_attempt_verdict_is_allow", "payment_attempt", type_="check"
    )
    op.create_check_constraint(
        "ck_payment_attempt_executable", "payment_attempt", _EXECUTABLE
    )
    op.create_foreign_key(
        "fk_payment_attempt_approved_approval",
        "payment_attempt",
        "approval",
        ["approval_id", "approval_outcome"],
        ["id", "outcome"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_payment_attempt_approved_approval", "payment_attempt", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_payment_attempt_executable", "payment_attempt", type_="check"
    )
    op.create_check_constraint(
        "ck_payment_attempt_verdict_is_allow",
        "payment_attempt",
        "decision_verdict = 'ALLOW'",
    )
    op.drop_index("ix_payment_attempt_approval_id", table_name="payment_attempt")
    op.drop_column("payment_attempt", "razorpay_short_url")
    op.drop_column("payment_attempt", "approval_outcome")
    op.drop_column("payment_attempt", "approval_id")

    op.drop_constraint("uq_approval_id_outcome", "approval", type_="unique")

    op.drop_column("decision", "executable_amount")
