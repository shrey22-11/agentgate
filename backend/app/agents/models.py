"""
Agent identity and permissions — SIMULATED population.

An `agent` row is the merchant's record of a counterparty it may transact with.
Even our own AI buyer agent gets a row here and is treated as untrusted: the
`status`, `allowed_actions` and `max_transaction_amount` columns are hard inputs
to the policy engine, not hints.

This is deliberately NOT an authentication system (see the frozen "what we will
not build" list). There are no credentials here — just identity and commercial
limits.
"""
from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from sqlalchemy import ARRAY, CheckConstraint, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, str_enum, uuid_pk
from app.core.enums import AgentStatus, AgentType

Money = Numeric(12, 2)


class Agent(TimestampMixin, Base):
    __tablename__ = "agent"
    __table_args__ = (
        CheckConstraint(
            "max_transaction_amount >= 0", name="ck_agent_max_txn_nonneg"
        ),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[AgentType] = str_enum(AgentType, nullable=False)
    status: Mapped[AgentStatus] = str_enum(
        AgentStatus, nullable=False, default=AgentStatus.ACTIVE
    )

    # Largest single transaction this agent may have auto-approved. Above this
    # the policy engine returns NEEDS_APPROVAL — it does not deny (Rule 4).
    max_transaction_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False
    )

    # Whitelist of ActionType values (stored as their string values). An action
    # not in this list is denied on RULE_ACTION_NOT_ALLOWED. Postgres text[].
    allowed_actions: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), nullable=False, default=list
    )
