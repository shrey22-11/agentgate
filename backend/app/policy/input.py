"""
`PolicyInput` — the in-memory snapshot the policy engine evaluates.

Deliberately plain data: no SQLAlchemy session, no ORM instances, no network.
The database layer (Phase 5) will load the agent / product / action-request
rows and assemble one of these; the engine never touches persistence.

Two layers of safety around this object:

1. *Structural* — this is a frozen Pydantic v2 model with `extra="forbid"` and
   per-field bounds (money >= 0, percentages 0..100, quantity >= 1). Malformed
   input cannot be constructed at all; the caller gets a `ValidationError`.
   Phase 5 wraps construction in a try/except and turns a failure into a DENY
   decision, so the system still fails closed.

2. *Semantic* — cross-field checks that depend on policy context (e.g. "a
   PURCHASE needs product fields", "proposed_price and requested_discount_pct
   must not contradict each other") live in the engine's input guard and yield
   a DENY / RULE_INPUT_INVALID decision rather than raising. Keeping them in the
   engine keeps `evaluate()` total: it always returns a `PolicyDecision`.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ActionType, AgentStatus


class PolicyInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- agent ---
    agent_id: uuid.UUID
    agent_status: AgentStatus
    # tuple, not list: the model is frozen and this keeps it hashable.
    agent_allowed_actions: tuple[ActionType, ...]
    agent_max_transaction_amount: Decimal = Field(ge=0)

    # --- request ---
    action_type: ActionType
    requested_quantity: int | None = Field(default=None, ge=1)
    requested_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    proposed_price: Decimal | None = Field(default=None, ge=0)

    # --- product (None only for a malformed request that never resolved one) ---
    product_id: uuid.UUID | None = None
    product_name: str | None = None
    product_price: Decimal | None = Field(default=None, ge=0)
    product_stock: int | None = Field(default=None, ge=0)
    product_max_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    product_min_margin_price: Decimal | None = Field(default=None, ge=0)

    # --- merchant ---
    merchant_policy_version: str = Field(min_length=1)
