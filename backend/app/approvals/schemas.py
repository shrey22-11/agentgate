"""
HTTP contract for the approval flow.

The approval layer is a human gate over a `NEEDS_APPROVAL` decision. It never
re-evaluates policy, so these schemas carry only who resolved it and why — no
prices, no discounts, no quantities to override.
"""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import ActionType, ApprovalOutcome


class ApprovalResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("approver")
    @classmethod
    def _approver_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("approver must not be blank")
        return stripped

    @field_validator("reason")
    @classmethod
    def _normalise_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class PendingApprovalItem(BaseModel):
    """One `NEEDS_APPROVAL` decision that has not been resolved yet."""

    decision_id: uuid.UUID
    action_request_id: uuid.UUID
    policy_version: str
    original_rule_id: str
    original_reason: str
    decision_created_at: dt.datetime

    agent_id: uuid.UUID
    agent_name: str

    product_id: uuid.UUID
    product_name: str
    product_price: Decimal

    action_type: ActionType
    quantity: int | None
    requested_discount_pct: Decimal | None
    proposed_price: Decimal | None


class ApprovalResolutionResponse(BaseModel):
    approval_id: uuid.UUID
    decision_id: uuid.UUID
    action_request_id: uuid.UUID
    outcome: ApprovalOutcome
    approver: str
    reason: str | None
    resolved_at: dt.datetime
