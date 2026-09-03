"""
Phase 4 — deterministic counter-offer / price-floor math.

No DB, no LLM: this is pure Decimal arithmetic and is tested as such.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.counter_offer.engine import CounterOffer, compute_floor

D = Decimal


def test_velocity_pro_floor_is_9000_at_10_percent() -> None:
    """Mirrors app/seed.py 'Velocity Pro Marathon Racer': ₹10,000 / 10% / ₹8,800."""
    co = compute_floor(D("10000.00"), D("10.00"), D("8800.00"))
    assert co == CounterOffer(
        price=D("9000.00"), discount_pct=D("10.00"), margin_is_binding=False
    )


def test_margin_floor_binds_when_above_discount_cap() -> None:
    # 50% off ₹1,000 would be ₹500, but the ₹700 margin floor is higher.
    co = compute_floor(D("1000.00"), D("50.00"), D("700.00"))
    assert co.price == D("700.00")
    assert co.discount_pct == D("30.00")
    assert co.margin_is_binding is True


def test_zero_discount_cap_floors_at_list_price() -> None:
    co = compute_floor(D("2500.00"), D("0"), D("1000.00"))
    assert co.price == D("2500.00")
    assert co.discount_pct == D("0.00")
    assert co.margin_is_binding is False


def test_full_discount_cap_floors_at_margin() -> None:
    co = compute_floor(D("2500.00"), D("100"), D("1000.00"))
    assert co.price == D("1000.00")
    assert co.margin_is_binding is True


def test_paise_level_rounding_is_half_up_to_two_places() -> None:
    # 999.99 * 92.5% = 924.99075 -> 924.99
    co = compute_floor(D("999.99"), D("7.50"), D("100.00"))
    assert co.price == D("924.99")
    assert co.price.as_tuple().exponent == -2
    assert co.discount_pct.as_tuple().exponent == -2


@pytest.mark.parametrize(
    "list_price, cap, margin",
    [
        (D("10000.00"), D("10.00"), D("8800.00")),
        (D("1000.00"), D("50.00"), D("700.00")),
        (D("4500.00"), D("15.00"), D("3600.00")),
        (D("45000.00"), D("8.00"), D("41000.00")),
        (D("3200.00"), D("20.00"), D("2400.00")),
        (D("999.99"), D("7.50"), D("0.00")),
    ],
)
def test_floor_never_below_margin_never_above_list(list_price, cap, margin) -> None:
    co = compute_floor(list_price, cap, margin)
    assert margin <= co.price <= list_price
    assert D("0") <= co.discount_pct <= D("100")


@pytest.mark.parametrize(
    "list_price, cap, margin",
    [
        (D("-1.00"), D("10"), D("0")),
        (D("100.00"), D("10"), D("-1.00")),
        (D("100.00"), D("150"), D("0")),
        (D("100.00"), D("-5"), D("0")),
        (D("100.00"), D("10"), D("200.00")),  # margin above list
    ],
)
def test_compute_floor_rejects_nonsense_inputs(list_price, cap, margin) -> None:
    with pytest.raises(ValueError):
        compute_floor(list_price, cap, margin)


def test_result_is_deterministic() -> None:
    args = (D("10000.00"), D("10.00"), D("8800.00"))
    results = {compute_floor(*args) for _ in range(5)}
    assert len(results) == 1
