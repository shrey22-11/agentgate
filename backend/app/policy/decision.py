"""
The immutable result of a policy evaluation.

`PolicyDecision` is a plain frozen dataclass, not an ORM row and not a Pydantic
model: Phase 4 produces the *decision logic* only. Persisting a decision to
PostgreSQL (mapping this onto `app.policy.models.Decision`) belongs to Phase 5.

Equality is structural, which is what the determinism tests rely on: the same
`PolicyInput` evaluated twice must produce two `PolicyDecision`s that compare
equal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Verdict


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    rule_id: str
    reason: str
    policy_version: str

    # Set only when verdict is COUNTER_OFFER. `counter_offer_price` is the
    # deterministic floor from `app.counter_offer`; `counter_offer_discount_pct`
    # is the discount from list that price represents.
    counter_offer_price: Decimal | None = None
    counter_offer_discount_pct: Decimal | None = None

    def __post_init__(self) -> None:
        # Cheap internal consistency guard — protects against a future rule
        # being wired up wrong. Not validation of external data.
        if self.verdict is Verdict.COUNTER_OFFER:
            if self.counter_offer_price is None or self.counter_offer_discount_pct is None:
                raise ValueError("COUNTER_OFFER decision must carry price and discount")
        else:
            if self.counter_offer_price is not None or self.counter_offer_discount_pct is not None:
                raise ValueError(
                    f"{self.verdict.value} decision must not carry counter-offer fields"
                )
