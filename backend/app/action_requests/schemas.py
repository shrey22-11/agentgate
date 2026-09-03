"""
HTTP contract for POST /actions. Request in, decision out — nothing else.

Money and percentage fields are `Decimal`. A JSON float is rejected (422):
binary floating point has no place in payments-adjacent input. Clients send
these as JSON strings (`"20.00"`) or integers (`20`).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import ActionType, Verdict


def _reject_float(value: Any) -> Any:
    # `bool` is an int subclass but never a sensible money value either.
    if isinstance(value, (float, bool)):
        raise ValueError("send money/percentage as a JSON string or integer, not a float")
    return value


class ActionRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    product_id: uuid.UUID
    action_type: ActionType = ActionType.PURCHASE
    quantity: int | None = Field(default=None, ge=1)
    requested_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    proposed_price: Decimal | None = Field(default=None, ge=0)

    _no_float_discount = field_validator("requested_discount_pct", mode="before")(_reject_float)
    _no_float_price = field_validator("proposed_price", mode="before")(_reject_float)


class CounterOfferOut(BaseModel):
    price: Decimal
    discount_pct: Decimal


class ActionDecisionResponse(BaseModel):
    action_request_id: uuid.UUID
    decision_id: uuid.UUID
    verdict: Verdict
    rule_id: str
    reason: str
    policy_version: str
    counter_offer: CounterOfferOut | None = None
