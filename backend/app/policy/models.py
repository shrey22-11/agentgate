"""
Decision — the output of the deterministic policy engine.

Exactly one Decision per ActionRequest. The verdict is one of four values and is
computed only by `app.policy` code — never by an LLM. `policy_rule_id` names the
single rule that determined the outcome (e.g. "RULE_DISCOUNT_EXCEEDED"), and
`reason` is a human-readable explanation of that rule firing. `policy_version`
is copied from the merchant at decision time so the decision stays explainable
against its original ruleset.

`counter_offer_price` is set only for verdict COUNTER_OFFER and is always a
`app.counter_offer` computation from `max_discount_pct` / `min_margin_price`.
"""
from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin, str_enum, uuid_pk
from app.core.enums import Verdict

Money = Numeric(12, 2)
Percent = Numeric(5, 2)


class Decision(TimestampMixin, Base):
    __tablename__ = "decision"
    __table_args__ = (
        # One decision per request.
        UniqueConstraint("action_request_id", name="uq_decision_action_request"),
        # Target for payment_attempt's composite FK: lets another table
        # reference "(this decision) AND (its verdict)" atomically, so a
        # payment row can be constrained to ALLOW decisions only at the DB
        # level, not just in application code.
        UniqueConstraint("id", "verdict", name="uq_decision_id_verdict"),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()
    action_request_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("action_request.id"),
        nullable=False,
        index=True,
    )

    verdict: Mapped[Verdict] = str_enum(Verdict, nullable=False)
    policy_rule_id: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # Both set only for verdict COUNTER_OFFER, copied verbatim from the
    # PolicyDecision so this row is a faithful record of the engine's output.
    # discount_pct is pinned here (not derived later) because the product's
    # list price may change after the decision was made.
    counter_offer_price: Mapped[Decimal | None] = mapped_column(Money)
    counter_offer_discount_pct: Mapped[Decimal | None] = mapped_column(Percent)

    # The amount a permitted purchase charges (effective unit price x quantity),
    # pinned at decision time. Set for ALLOW and NEEDS_APPROVAL, NULL for DENY
    # and COUNTER_OFFER. The execution layer (Phase 8) charges exactly this —
    # never an amount supplied by a client.
    executable_amount: Mapped[Decimal | None] = mapped_column(Money)

    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)

    action_request: Mapped["ActionRequest"] = relationship(  # noqa: F821
        back_populates="decision"
    )
