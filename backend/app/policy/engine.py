"""
The deterministic policy engine. OUR SYSTEM.

    evaluate(policy_input) -> PolicyDecision

Pure and total: no I/O, no LLM, no randomness, no database. The same
`PolicyInput` always yields an equal `PolicyDecision`. It always returns a
decision — it never raises for policy reasons; genuinely malformed input yields
DENY / RULE_INPUT_INVALID (fail closed).

Rule precedence is documented in `app.policy.rules`. The first rule that fires
decides the verdict.

Phase boundary: this module evaluates policy only. It moves no money, writes no
`decision` row, and calls no Razorpay or Anthropic API. Those are later phases.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.enums import ActionType, AgentStatus, Verdict
from app.counter_offer.engine import compute_floor
from app.policy import rules
from app.policy.decision import PolicyDecision
from app.policy.input import PolicyInput

_PAISE = Decimal("0.01")
_HUNDRED = Decimal("100")
_ZERO = Decimal("0")

# Tolerance when checking that a proposed_price and a requested_discount_pct
# describe the same deal: one paise of rounding on the derived percentage.
_PCT_CONSISTENCY_TOLERANCE = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _Ctx:
    """Values derived once from a well-formed PolicyInput, shared by rules 3-6."""

    quantity: int
    list_price: Decimal
    effective_unit_price: Decimal        # what the agent proposes to pay per unit
    effective_txn_amount: Decimal        # effective_unit_price * quantity
    requested_discount_pct: Decimal      # discount from list the proposal represents, >= 0


def _deny(pi: PolicyInput, rule_id: str, reason: str) -> PolicyDecision:
    return PolicyDecision(
        verdict=Verdict.DENY,
        rule_id=rule_id,
        reason=reason,
        policy_version=pi.merchant_policy_version,
    )


# --- input consistency guard (precedence 0) --------------------------------
def _guard_input(pi: PolicyInput) -> PolicyDecision | None:
    """
    Semantic checks that a frozen Pydantic model cannot express on its own.
    Returns a DENY decision on failure, or None to continue.
    """
    # Every current ActionType is a purchase-shaped action and needs a product.
    missing = [
        name
        for name, value in (
            ("product_price", pi.product_price),
            ("product_stock", pi.product_stock),
            ("product_max_discount_pct", pi.product_max_discount_pct),
            ("product_min_margin_price", pi.product_min_margin_price),
        )
        if value is None
    ]
    if missing:
        return _deny(
            pi,
            rules.RULE_INPUT_INVALID,
            f"Request for {pi.action_type.value} is missing required product "
            f"data: {', '.join(missing)}. Failing closed.",
        )

    assert pi.product_price is not None and pi.product_min_margin_price is not None
    if pi.product_min_margin_price > pi.product_price:
        return _deny(
            pi,
            rules.RULE_INPUT_INVALID,
            f"Product floor ₹{pi.product_min_margin_price} exceeds list price "
            f"₹{pi.product_price}; catalogue data is inconsistent.",
        )

    # A proposed price and an explicit discount must describe the same deal.
    if (
        pi.proposed_price is not None
        and pi.requested_discount_pct is not None
        and pi.product_price > 0
    ):
        derived = (pi.product_price - pi.proposed_price) / pi.product_price * _HUNDRED
        derived = max(_ZERO, _percent(derived))
        if abs(derived - pi.requested_discount_pct) > _PCT_CONSISTENCY_TOLERANCE:
            return _deny(
                pi,
                rules.RULE_INPUT_INVALID,
                f"Proposed price ₹{pi.proposed_price} implies a "
                f"{derived}% discount, which contradicts the requested "
                f"{pi.requested_discount_pct}%.",
            )
    return None


def _effective_unit_price(list_price: Decimal, pi: PolicyInput) -> Decimal:
    """What the agent proposes to pay per unit: an explicit proposed price, a
    list price with the requested discount applied, or the list price."""
    if pi.proposed_price is not None:
        return pi.proposed_price
    if pi.requested_discount_pct is not None:
        return _money(list_price * (_HUNDRED - pi.requested_discount_pct) / _HUNDRED)
    return list_price


def _quantity(pi: PolicyInput) -> int:
    return pi.requested_quantity if pi.requested_quantity is not None else 1


def _build_ctx(pi: PolicyInput) -> _Ctx:
    assert pi.product_price is not None
    list_price = pi.product_price
    quantity = _quantity(pi)
    effective_unit_price = _effective_unit_price(list_price, pi)

    if list_price > 0:
        discount_pct = max(
            _ZERO,
            _percent((list_price - effective_unit_price) / list_price * _HUNDRED),
        )
    else:
        discount_pct = _ZERO

    return _Ctx(
        quantity=quantity,
        list_price=list_price,
        effective_unit_price=effective_unit_price,
        effective_txn_amount=_money(effective_unit_price * quantity),
        requested_discount_pct=discount_pct,
    )


def effective_transaction_amount(pi: PolicyInput) -> Decimal | None:
    """
    The amount a permitted purchase would charge: effective unit price x
    quantity, quantised to paise. `None` when there is no product price to
    compute it from.

    Pure and deterministic — same helpers the engine uses in `_build_ctx`, so
    this can never drift from what the policy actually evaluated. The Action API
    stores this on the `Decision` row so the execution layer (Phase 8) charges a
    trusted amount, never one supplied by a client.
    """
    if pi.product_price is None:
        return None
    return _money(_effective_unit_price(pi.product_price, pi) * _quantity(pi))


# --- rules ---------------------------------------------------------------
def _rule_agent_active(pi: PolicyInput) -> PolicyDecision | None:
    if pi.agent_status is AgentStatus.ACTIVE:
        return None
    return _deny(
        pi,
        rules.RULE_AGENT_ACTIVE,
        f"Agent status is {pi.agent_status.value}; only ACTIVE agents may "
        f"perform commercial actions.",
    )


def _rule_action_permission(pi: PolicyInput) -> PolicyDecision | None:
    if pi.action_type in pi.agent_allowed_actions:
        return None
    permitted = ", ".join(a.value for a in pi.agent_allowed_actions) or "(none)"
    return _deny(
        pi,
        rules.RULE_ACTION_PERMISSION,
        f"Agent is not permitted to perform {pi.action_type.value}. "
        f"Permitted actions: {permitted}.",
    )


def _rule_transaction_cap(pi: PolicyInput, ctx: _Ctx) -> PolicyDecision | None:
    if ctx.effective_txn_amount <= pi.agent_max_transaction_amount:
        return None
    return PolicyDecision(
        verdict=Verdict.NEEDS_APPROVAL,
        rule_id=rules.RULE_TRANSACTION_CAP,
        reason=(
            f"Transaction amount ₹{ctx.effective_txn_amount} "
            f"({ctx.quantity} x ₹{ctx.effective_unit_price}) exceeds this "
            f"agent's per-transaction limit of "
            f"₹{pi.agent_max_transaction_amount}. Routed to human approval."
        ),
        policy_version=pi.merchant_policy_version,
    )


def _rule_stock_available(pi: PolicyInput, ctx: _Ctx) -> PolicyDecision | None:
    assert pi.product_stock is not None
    if ctx.quantity <= pi.product_stock:
        return None
    return _deny(
        pi,
        rules.RULE_STOCK_AVAILABLE,
        f"Requested quantity {ctx.quantity} exceeds available stock "
        f"{pi.product_stock}.",
    )


def _rule_price_floor(pi: PolicyInput, ctx: _Ctx) -> PolicyDecision | None:
    assert (
        pi.product_max_discount_pct is not None
        and pi.product_min_margin_price is not None
    )
    floor = compute_floor(
        ctx.list_price, pi.product_max_discount_pct, pi.product_min_margin_price
    )
    if ctx.effective_unit_price >= floor.price:
        return None  # terms are within policy

    rule_id = (
        rules.RULE_PRICE_FLOOR if floor.margin_is_binding else rules.RULE_DISCOUNT_POLICY
    )
    if floor.margin_is_binding:
        why = (
            f"the product's ₹{pi.product_min_margin_price} margin floor "
            f"(a {floor.discount_pct}% discount from list)"
        )
    else:
        why = (
            f"the {pi.product_max_discount_pct}% maximum discount "
            f"(₹{floor.price} from list ₹{ctx.list_price})"
        )
    return PolicyDecision(
        verdict=Verdict.COUNTER_OFFER,
        rule_id=rule_id,
        reason=(
            f"Requested unit price ₹{ctx.effective_unit_price} "
            f"({ctx.requested_discount_pct}% off) is below {why}. "
            f"Counter-offered at ₹{floor.price}."
        ),
        policy_version=pi.merchant_policy_version,
        counter_offer_price=floor.price,
        counter_offer_discount_pct=floor.discount_pct,
    )


# --- entry point -------------------------------------------------------
def evaluate(policy_input: PolicyInput) -> PolicyDecision:
    guard = _guard_input(policy_input)
    if guard is not None:
        return guard

    for agent_rule in (_rule_agent_active, _rule_action_permission):
        decision = agent_rule(policy_input)
        if decision is not None:
            return decision

    ctx = _build_ctx(policy_input)
    for commercial_rule in (
        _rule_transaction_cap,
        _rule_stock_available,
        _rule_price_floor,
    ):
        decision = commercial_rule(policy_input, ctx)
        if decision is not None:
            return decision

    return PolicyDecision(
        verdict=Verdict.ALLOW,
        rule_id=rules.RULE_OK,
        reason=(
            f"All policy rules satisfied. {ctx.quantity} x ₹"
            f"{ctx.effective_unit_price} = ₹{ctx.effective_txn_amount}, "
            f"{ctx.requested_discount_pct}% off list."
        ),
        policy_version=policy_input.merchant_policy_version,
    )


# Kept importable but unused here — a reminder that ACCEPT_COUNTER_OFFER is
# evaluated by exactly the same path as PURCHASE (defence in depth: even our own
# counter-offer acceptance is re-checked against every rule).
_RE_EVALUATED_ACTIONS = (ActionType.PURCHASE, ActionType.ACCEPT_COUNTER_OFFER)
