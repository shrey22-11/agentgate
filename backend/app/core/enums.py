"""
Cross-cutting enums.

These are referenced by more than one feature module (e.g. `Verdict` by both
the policy engine and the audit module; `ActionType` by both agents and
action_requests), so they live here rather than in any one feature package to
keep imports acyclic.

All of these are persisted as plain VARCHAR + CHECK constraint, not native
PostgreSQL ENUM types (see `app.core.db.str_enum`). Native enums make Alembic
migrations painful (every value change is an `ALTER TYPE`), and we gain nothing
from them here.
"""
from __future__ import annotations

import enum


class AgentType(str, enum.Enum):
    """What kind of counterparty an agent record represents."""

    AI_BUYER = "AI_BUYER"      # our own LLM-driven buyer agent (still untrusted)
    EXTERNAL = "EXTERNAL"      # a third-party agent we neither built nor control


class AgentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DISABLED = "DISABLED"


class ActionType(str, enum.Enum):
    """
    The commercial actions an agent can *request* through AgentGate.

    Read-only catalog operations (search / get / compare) are plain tool calls
    that never create an ActionRequest, so they are deliberately absent here.
    Every value in this enum is a money-adjacent action that must be gated.
    """

    PURCHASE = "PURCHASE"                          # buy at list price or a requested discount
    ACCEPT_COUNTER_OFFER = "ACCEPT_COUNTER_OFFER"  # accept a prior COUNTER_OFFER verdict


class ActionRequestStatus(str, enum.Enum):
    """Lifecycle of a single ActionRequest row."""

    RECEIVED = "RECEIVED"            # raw input stored, nothing parsed yet
    PARSED = "PARSED"               # LLM produced structured output
    VALIDATED = "VALIDATED"        # structured output passed schema + business checks
    INVALID = "INVALID"           # parse/schema/business failure -> policy treats as DENY
    DECIDED = "DECIDED"          # a Decision row exists for this request
    EXECUTED = "EXECUTED"      # an allowed action led to a payment object
    FAILED = "FAILED"        # execution error after an ALLOW


class Verdict(str, enum.Enum):
    """The four — and only four — outcomes of policy evaluation."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    COUNTER_OFFER = "COUNTER_OFFER"


class ApprovalOutcome(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PaymentStatus(str, enum.Enum):
    """
    Local lifecycle of a PaymentAttempt (Phase 8).

        CREATED -> PENDING -> PAID
        CREATED -> FAILED
        PENDING -> FAILED / EXPIRED

    Terminal: PAID, FAILED, EXPIRED.
    """

    CREATED = "CREATED"      # local row created; no Razorpay object yet
    PENDING = "PENDING"      # Razorpay payment link created; awaiting payment
    PAID = "PAID"           # payment captured (webhook / reconciliation)
    FAILED = "FAILED"      # Razorpay creation failed, or payment failed/cancelled
    EXPIRED = "EXPIRED"   # payment link expired unpaid
