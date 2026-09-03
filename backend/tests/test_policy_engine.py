"""
Phase 4 — the deterministic policy engine.

Boundary-heavy, in-memory, no DB and no LLM. Every test builds a `PolicyInput`
with `_mk(...)` (a valid Trailblaze-style request) and overrides only the field
under test, so a failure points straight at one rule.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.counter_offer.engine as counter_offer_module
import app.policy.engine as engine_module
from app.core.enums import ActionType, AgentStatus, Verdict
from app.policy import evaluate, rules
from app.policy.decision import PolicyDecision
from app.policy.input import PolicyInput

D = Decimal


def _mk(**over) -> PolicyInput:
    base = dict(
        agent_id=uuid4(),
        agent_status=AgentStatus.ACTIVE,
        agent_allowed_actions=(ActionType.PURCHASE, ActionType.ACCEPT_COUNTER_OFFER),
        agent_max_transaction_amount=D("25000.00"),
        action_type=ActionType.PURCHASE,
        product_id=uuid4(),
        product_name="Trailblaze Daily Trainer (SIMULATED)",
        product_price=D("4500.00"),
        product_stock=40,
        product_max_discount_pct=D("15.00"),
        product_min_margin_price=D("3600.00"),
        merchant_policy_version="v1",
    )
    base.update(over)
    return PolicyInput(**base)


def _velocity(**over) -> PolicyInput:
    """Seed 'Velocity Pro Marathon Racer': ₹10,000 / 10% cap / ₹8,800 floor."""
    velocity = dict(
        product_name="Velocity Pro Marathon Racer (SIMULATED)",
        product_price=D("10000.00"),
        product_stock=12,
        product_max_discount_pct=D("10.00"),
        product_min_margin_price=D("8800.00"),
    )
    velocity.update(over)
    return _mk(**velocity)


# --- happy path -------------------------------------------------------------
def test_active_agent_plain_purchase_is_allowed() -> None:
    d = evaluate(_mk())
    assert d.verdict is Verdict.ALLOW
    assert d.rule_id == rules.RULE_OK
    assert d.policy_version == "v1"
    assert d.counter_offer_price is None


def test_purchase_with_quantity_and_small_discount_is_allowed() -> None:
    d = evaluate(_mk(requested_quantity=2, requested_discount_pct=D("5")))
    assert d.verdict is Verdict.ALLOW


# --- Rule 1: agent active -------------------------------------------------
@pytest.mark.parametrize("status", [AgentStatus.SUSPENDED, AgentStatus.DISABLED])
def test_inactive_agent_is_denied(status) -> None:
    d = evaluate(_mk(agent_status=status))
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_AGENT_ACTIVE


def test_inactive_agent_precedes_discount_evaluation() -> None:
    # Inactive AND asking for an excessive discount -> agent rule wins, no counter-offer.
    d = evaluate(_velocity(agent_status=AgentStatus.SUSPENDED, requested_discount_pct=D("20")))
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_AGENT_ACTIVE


# --- Rule 2: action permission -----------------------------------------
def test_action_not_in_allowlist_is_denied() -> None:
    d = evaluate(_mk(agent_allowed_actions=(ActionType.ACCEPT_COUNTER_OFFER,)))
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_ACTION_PERMISSION


def test_empty_allowlist_denies_even_when_terms_are_fine() -> None:
    d = evaluate(_mk(agent_allowed_actions=(), requested_discount_pct=D("5")))
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_ACTION_PERMISSION


def test_unauthorized_action_precedes_transaction_cap() -> None:
    d = evaluate(
        _velocity(
            agent_allowed_actions=(),
            agent_max_transaction_amount=D("1.00"),  # would also blow the cap
        )
    )
    assert d.rule_id == rules.RULE_ACTION_PERMISSION


# --- Rule 3: transaction cap -----------------------------------------
def test_amount_below_cap_is_allowed() -> None:
    d = evaluate(_velocity(agent_max_transaction_amount=D("10000.01")))
    assert d.verdict is Verdict.ALLOW


def test_amount_exactly_equal_to_cap_is_allowed() -> None:
    d = evaluate(_velocity(agent_max_transaction_amount=D("10000.00")))
    assert d.verdict is Verdict.ALLOW
    assert d.rule_id == rules.RULE_OK


def test_amount_above_cap_needs_approval() -> None:
    # Seed 'Home Marathon Treadmill T9': ₹45,000 vs the ₹25,000 reference cap.
    d = evaluate(
        _mk(
            product_name="Home Marathon Treadmill T9 (SIMULATED)",
            product_price=D("45000.00"),
            product_stock=3,
            product_max_discount_pct=D("8.00"),
            product_min_margin_price=D("41000.00"),
            agent_max_transaction_amount=D("25000.00"),
        )
    )
    assert d.verdict is Verdict.NEEDS_APPROVAL
    assert d.rule_id == rules.RULE_TRANSACTION_CAP
    assert d.counter_offer_price is None


def test_transaction_cap_precedes_discount_even_after_discount_applied() -> None:
    # 60% off ₹45,000 = ₹18,000, still over a ₹10,000 cap; discount is also excessive.
    d = evaluate(
        _mk(
            product_price=D("45000.00"),
            product_stock=3,
            product_max_discount_pct=D("8.00"),
            product_min_margin_price=D("41000.00"),
            requested_discount_pct=D("60"),
            agent_max_transaction_amount=D("10000.00"),
        )
    )
    assert d.verdict is Verdict.NEEDS_APPROVAL
    assert d.rule_id == rules.RULE_TRANSACTION_CAP


def test_transaction_cap_precedes_stock() -> None:
    d = evaluate(
        _mk(
            product_price=D("45000.00"),
            product_stock=3,
            requested_quantity=5,  # also over stock
            agent_max_transaction_amount=D("10000.00"),
        )
    )
    assert d.rule_id == rules.RULE_TRANSACTION_CAP


# --- Rule 4: stock -----------------------------------------------------
def test_quantity_below_stock_is_allowed() -> None:
    d = evaluate(_velocity(requested_quantity=2, agent_max_transaction_amount=D("100000")))
    assert d.verdict is Verdict.ALLOW


def test_quantity_exactly_equal_to_stock_is_allowed() -> None:
    d = evaluate(
        _velocity(
            product_stock=3, requested_quantity=3, agent_max_transaction_amount=D("100000")
        )
    )
    assert d.verdict is Verdict.ALLOW


def test_quantity_above_stock_is_denied() -> None:
    d = evaluate(
        _velocity(
            product_stock=3, requested_quantity=5, agent_max_transaction_amount=D("100000")
        )
    )
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_STOCK_AVAILABLE


def test_out_of_stock_never_produces_counter_offer() -> None:
    d = evaluate(
        _velocity(
            product_stock=3,
            requested_quantity=5,
            requested_discount_pct=D("20"),  # excessive discount too
            agent_max_transaction_amount=D("100000"),
        )
    )
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_STOCK_AVAILABLE
    assert d.counter_offer_price is None


# --- Rule 5/6: discount policy + price floor -------------------------
def test_discount_below_cap_is_allowed() -> None:
    d = evaluate(_velocity(requested_discount_pct=D("5")))
    assert d.verdict is Verdict.ALLOW


def test_discount_exactly_equal_to_cap_is_allowed() -> None:
    d = evaluate(_velocity(requested_discount_pct=D("10")))
    assert d.verdict is Verdict.ALLOW
    assert d.rule_id == rules.RULE_OK


def test_velocity_pro_20_percent_request_counter_offers_9000() -> None:
    d = evaluate(_velocity(requested_discount_pct=D("20")))
    assert d.verdict is Verdict.COUNTER_OFFER
    assert d.rule_id == rules.RULE_DISCOUNT_POLICY
    assert d.counter_offer_price == D("9000.00")
    assert d.counter_offer_discount_pct == D("10.00")
    assert d.counter_offer_price.as_tuple().exponent == -2


def test_excessive_discount_via_proposed_price_counter_offers() -> None:
    d = evaluate(_velocity(proposed_price=D("8000.00")))
    assert d.verdict is Verdict.COUNTER_OFFER
    assert d.counter_offer_price == D("9000.00")


def test_proposed_price_within_policy_is_allowed() -> None:
    d = evaluate(_velocity(proposed_price=D("9500.00")))
    assert d.verdict is Verdict.ALLOW


def test_counter_offer_never_below_margin_and_never_above_list() -> None:
    # Margin floor (₹700) is above the 50%-off price (₹500): a 60% request must
    # counter to ₹700 exactly, tagged as the price-floor rule.
    pi = _mk(
        product_price=D("1000.00"),
        product_stock=10,
        product_max_discount_pct=D("50.00"),
        product_min_margin_price=D("700.00"),
        requested_discount_pct=D("60"),
    )
    d = evaluate(pi)
    assert d.verdict is Verdict.COUNTER_OFFER
    assert d.rule_id == rules.RULE_PRICE_FLOOR
    assert d.counter_offer_price == D("700.00")
    assert pi.product_min_margin_price <= d.counter_offer_price <= pi.product_price


def test_within_cap_but_below_margin_floor_still_counter_offers() -> None:
    # 45% < 50% cap, but ₹550 < ₹700 margin floor.
    d = evaluate(
        _mk(
            product_price=D("1000.00"),
            product_stock=10,
            product_max_discount_pct=D("50.00"),
            product_min_margin_price=D("700.00"),
            requested_discount_pct=D("45"),
        )
    )
    assert d.verdict is Verdict.COUNTER_OFFER
    assert d.rule_id == rules.RULE_PRICE_FLOOR
    assert d.counter_offer_price == D("700.00")


def test_paise_level_counter_offer_is_quantized() -> None:
    d = evaluate(
        _mk(
            product_price=D("999.99"),
            product_stock=10,
            product_max_discount_pct=D("7.50"),
            product_min_margin_price=D("100.00"),
            requested_discount_pct=D("50"),
        )
    )
    assert d.verdict is Verdict.COUNTER_OFFER
    assert d.counter_offer_price == D("924.99")
    assert d.counter_offer_price.as_tuple().exponent == -2


# --- invalid / inconsistent input fails closed ----------------------
def test_missing_product_data_denies_as_input_invalid() -> None:
    d = evaluate(
        _mk(
            product_price=None,
            product_stock=None,
            product_max_discount_pct=None,
            product_min_margin_price=None,
        )
    )
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_INPUT_INVALID


def test_contradictory_proposed_price_and_discount_denies() -> None:
    # proposed ₹8,000 implies 20% off ₹10,000, contradicts the stated 5%.
    d = evaluate(_velocity(proposed_price=D("8000.00"), requested_discount_pct=D("5")))
    assert d.verdict is Verdict.DENY
    assert d.rule_id == rules.RULE_INPUT_INVALID


def test_consistent_proposed_price_and_discount_is_accepted() -> None:
    d = evaluate(_velocity(proposed_price=D("9500.00"), requested_discount_pct=D("5")))
    assert d.verdict is Verdict.ALLOW


@pytest.mark.parametrize(
    "bad",
    [
        {"product_price": D("-1.00")},
        {"requested_discount_pct": D("150")},
        {"requested_discount_pct": D("-5")},
        {"requested_quantity": 0},
        {"agent_max_transaction_amount": D("-1")},
        {"merchant_policy_version": ""},
        {"unexpected_field": 1},
    ],
)
def test_policy_input_rejects_structurally_invalid_data(bad) -> None:
    with pytest.raises(ValidationError):
        _mk(**bad)


# --- determinism -----------------------------------------------------
def test_same_input_yields_identical_decision() -> None:
    pi = _velocity(requested_discount_pct=D("20"))
    results = [evaluate(pi) for _ in range(5)]
    assert all(r == results[0] for r in results)
    assert results[0] == PolicyDecision(
        verdict=Verdict.COUNTER_OFFER,
        rule_id=rules.RULE_DISCOUNT_POLICY,
        reason=results[0].reason,  # reason text is deterministic; compared below
        policy_version="v1",
        counter_offer_price=D("9000.00"),
        counter_offer_discount_pct=D("10.00"),
    )
    assert "9000.00" in results[0].reason


def test_precedence_is_total_order_across_all_rules() -> None:
    # inactive + unauthorized + over cap + over stock + excessive discount,
    # all at once -> the agent-active rule (highest precedence) wins.
    d = evaluate(
        _velocity(
            agent_status=AgentStatus.DISABLED,
            agent_allowed_actions=(),
            agent_max_transaction_amount=D("1.00"),
            product_stock=0,
            requested_quantity=99,
            requested_discount_pct=D("90"),
        )
    )
    assert d.rule_id == rules.RULE_AGENT_ACTIVE


# --- Decimal / no-float discipline ---------------------------------
def test_policy_and_counter_offer_source_never_uses_float() -> None:
    for mod in (engine_module, counter_offer_module):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "float(" not in src, f"{mod.__name__} must not call float()"
        assert "from decimal import" in src


def test_counter_offer_fields_are_decimal_with_two_places() -> None:
    d = evaluate(_velocity(requested_discount_pct=D("20")))
    assert isinstance(d.counter_offer_price, Decimal)
    assert isinstance(d.counter_offer_discount_pct, Decimal)
    assert d.counter_offer_price == D("9000.00")
    assert d.counter_offer_price.as_tuple().exponent == -2
