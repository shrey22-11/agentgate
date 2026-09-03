"""
Deterministic counter-offer / price-floor calculation. OUR SYSTEM.

This module computes the single commercial boundary the policy engine needs:
the lowest price at which a purchase may proceed for a given product. It is
pure Decimal arithmetic with no I/O, no randomness, and — most importantly — no
LLM. The architecture-freeze doc is explicit that the fastest wrong move under
deadline pressure is "ask the model what a fair counter-offer would be"; that is
structurally impossible here because nothing in this file can call a model.

Boundary formula
----------------
    discounted_at_cap = list_price * (100 - max_discount_pct) / 100
    floor_price       = max(discounted_at_cap, min_margin_price)

`floor_price` is always in the closed interval [min_margin_price, list_price]:
both arguments of the max are <= list_price (a non-negative discount, and the
product's own margin floor which the DB constrains to <= price), and the result
is >= min_margin_price by construction.

Rounding
--------
All money is quantized to paise (0.01) with ROUND_HALF_UP — one consistent rule
for the whole codebase. Percentages are quantized to 0.01 the same way. Every
seeded demo scenario is exact at paise, so rounding never changes a demo number;
the rule exists for the general case.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_PAISE = Decimal("0.01")
_HUNDRED = Decimal("100")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CounterOffer:
    """The deterministic floor for one product."""

    price: Decimal            # lowest permitted unit price, in [min_margin_price, list_price]
    discount_pct: Decimal     # discount from list that `price` represents, 0..100
    margin_is_binding: bool   # True if min_margin_price set the floor, not the discount cap


def compute_floor(
    list_price: Decimal,
    max_discount_pct: Decimal,
    min_margin_price: Decimal,
) -> CounterOffer:
    """
    Return the price floor for a product.

    Raises ValueError on inputs that make no commercial sense (negative money,
    a discount percentage outside 0..100). The policy engine only calls this
    after its own input-consistency guard has passed, so a raise here means a
    programming error upstream, not untrusted data — it must not be swallowed.
    """
    if list_price < 0 or min_margin_price < 0:
        raise ValueError("prices must be non-negative")
    if not (Decimal(0) <= max_discount_pct <= _HUNDRED):
        raise ValueError("max_discount_pct must be within 0..100")
    if min_margin_price > list_price:
        raise ValueError("min_margin_price cannot exceed list_price")

    discounted_at_cap = _money(list_price * (_HUNDRED - max_discount_pct) / _HUNDRED)
    margin_floor = _money(min_margin_price)

    if margin_floor > discounted_at_cap:
        floor_price = margin_floor
        margin_is_binding = True
    else:
        floor_price = discounted_at_cap
        margin_is_binding = False

    if list_price > 0:
        discount_pct = _percent(
            (list_price - floor_price) / list_price * _HUNDRED
        )
    else:
        discount_pct = Decimal("0.00")

    return CounterOffer(
        price=floor_price,
        discount_pct=discount_pct,
        margin_is_binding=margin_is_binding,
    )
